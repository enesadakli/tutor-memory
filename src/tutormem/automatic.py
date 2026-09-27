from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .config import Config
from .errors import (
    AutomaticModeError,
    DuplicateContentError,
    ParseError,
    PendingReviewError,
    SchemaError,
    StaleArtifactError,
    TutormemError,
)
from .extract import Extractor, make_extractor
from .ingest import _parse_manual, ingest
from .models import (
    DecisionFile,
    Hypothesis,
    Instruction,
    ProfileState,
    Revocation,
    Session,
    VerifyResult,
    turns_sha256,
)
from .promote import replay
from .render import render_brief, render_profile
from .review import ClaudeReviewer, apply_decisions, write_packet
from .storage import (
    Workspace,
    list_sessions,
    load_artifact,
    write_json,
    write_profile,
    write_text,
)
from .sync import sync as sync_brief
from .verify import verify

_LOCK_MAX_AGE_S = 60 * 60
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[Any]]
Notifier = Callable[[str], None]
Syncer = Callable[[Workspace, Config], str]


class Reviewer(Protocol):
    def propose(self, packet: str, result: VerifyResult) -> DecisionFile:
        """Return proposed decisions for a verified session."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class AutoResult:
    processed: tuple[str, ...]
    failed: tuple[str, ...]
    changed: bool
    locked: bool = False


@dataclass(frozen=True, slots=True)
class _InboxItem:
    transcript: Path
    sidecar: dict[str, Any]
    session_id: str
    course: str
    date: str
    sha256: str


def _empty_profile() -> ProfileState:
    return ProfileState(1, (), (), (), ())


def _open_items(state: ProfileState) -> tuple[Instruction | Hypothesis, ...]:
    instructions = tuple(item for item in state.instructions if item.status == "active")
    hypotheses = tuple(item for item in state.hypotheses if item.status in ("open", "promoted"))
    return instructions + hypotheses


def _approvals(ws: Workspace) -> dict[str, Any]:
    approvals: dict[str, Any] = {}
    for session in list_sessions(ws):
        try:
            result = load_artifact(ws.verify_path(session.session_id), VerifyResult, session)
            decisions = load_artifact(ws.decisions_path(session.session_id), DecisionFile, session)
            if result is None or decisions is None:
                approvals[session.session_id] = None
            else:
                approvals[session.session_id] = apply_decisions(result, decisions)
        except (PendingReviewError, StaleArtifactError):
            approvals[session.session_id] = None
    return approvals


def _replayed(ws: Workspace, config: Config) -> ProfileState:
    return replay(list_sessions(ws), _approvals(ws), config)


def _render(ws: Workspace, state: ProfileState, config: Config) -> None:
    courses = ws.courses_path.read_text(encoding="utf-8") if ws.courses_path.exists() else None
    base = ws.base_path.read_text(encoding="utf-8") if ws.base_path.exists() else None
    write_profile(ws, state)
    write_text(ws.out_dir / "profile.md", render_profile(state, threshold=config.threshold))
    write_text(ws.out_dir / "brief.md", render_brief(state, courses, base_md=base))


def profile_diff(before: ProfileState, after: ProfileState, *, threshold: int = 3) -> list[str]:
    """Describe learner-profile changes in deterministic creation order."""
    lines: list[str] = []
    before_instructions = {item.id: item for item in before.instructions}
    before_hypotheses = {item.id: item for item in before.hypotheses}

    for item in after.instructions:
        previous = before_instructions.get(item.id)
        if item.status == "active" and previous is None:
            lines.append(f"New instruction: {item.claim} ({item.id})")

    for item in after.hypotheses:
        previous = before_hypotheses.get(item.id)
        if item.status == "promoted" and (previous is None or previous.status != "promoted"):
            lines.append(f"Promoted: {item.claim} ({item.id})")
        elif item.status == "open" and previous is None:
            lines.append(f"New hypothesis: {item.claim} ({item.id})")
        elif (
            previous is not None
            and item.status in ("open", "promoted")
            and len(item.sessions) > len(previous.sessions)
        ):
            lines.append(f"Hypothesis {len(item.sessions)}/{threshold}: {item.claim} ({item.id})")

    for item in after.hypotheses:
        previous = before_hypotheses.get(item.id)
        if item.status == "dropped" and previous is not None and previous.status != "dropped":
            lines.append(f"Dropped: {item.claim} ({item.id})")

    for item in after.instructions:
        previous = before_instructions.get(item.id)
        if item.status == "revoked" and previous is not None and previous.status != "revoked":
            lines.append(f"Revoked: {item.claim} ({item.id})")
    for item in after.hypotheses:
        previous = before_hypotheses.get(item.id)
        if item.status == "revoked" and previous is not None and previous.status != "revoked":
            lines.append(f"Revoked: {item.claim} ({item.id})")
    return lines


def _append(path: Path, block: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n\n") else "\n"
    write_text(path, existing + separator + block)


def write_changelog(
    ws: Workspace,
    config: Config,
    session_ids: Sequence[str],
    lines: Sequence[str],
    *,
    timestamp: datetime | None = None,
    undo_ids: Sequence[str] = (),
) -> str:
    """Append one automatic-mode change block and return it."""
    stamp = (timestamp or datetime.now().astimezone()).astimezone().isoformat(timespec="seconds")
    body = [f"## {stamp}", "", "Sessions: " + (", ".join(session_ids) or "(none)")]
    body.extend(["", *[f"- {line}" for line in lines]])
    if not lines:
        body.extend(["", "- No profile changes."])
    for target_id in dict.fromkeys(undo_ids):
        body.extend(["", f"Undo: tutormem revoke {target_id}"])
    block = "\n".join(body) + "\n"
    _append(ws.changelog_path, block)
    if config.auto.changelog_copy:
        copy_path = Path(config.auto.changelog_copy).expanduser()
        if copy_path != ws.changelog_path:
            _append(copy_path, block)
    return block


def _default_command_runner(args: list[str]) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def notify_macos(
    text: str,
    *,
    runner: CommandRunner | None = None,
    platform: str | None = None,
) -> None:
    """Display a macOS notification, doing nothing on other platforms."""
    if (platform or sys.platform) != "darwin":
        return
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{escaped}" with title "tutor-memory"'
    (runner or _default_command_runner)(["osascript", "-e", script])


def _notification_text(lines: Sequence[str]) -> str:
    shown = list(lines[:3])
    if len(lines) > 3:
        shown.append("…")
    return "\n".join(shown)


def _log(ws: Workspace, message: str, *, timestamp: datetime | None = None) -> None:
    stamp = (timestamp or datetime.now().astimezone()).astimezone().isoformat(timespec="seconds")
    _append(ws.auto_log_path, f"{stamp} {message}\n")


class _AutoLock:
    def __init__(self, path: Path, now_epoch: float) -> None:
        self.path = path
        self.now_epoch = now_epoch
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    age = self.now_epoch - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age <= _LOCK_MAX_AGE_S:
                    return False
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.acquired = False


def _sidecar_date(value: Any) -> str:
    if not isinstance(value, str):
        raise SchemaError("sidecar updated_at must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchemaError("sidecar updated_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    else:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def _inbox_item(path: Path, config: Config) -> _InboxItem:
    sidecar_path = path.with_suffix(".json")
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read sidecar {sidecar_path.name}: {exc}") from exc
    if not isinstance(sidecar, dict):
        raise SchemaError(f"sidecar {sidecar_path.name}: expected object")
    chat_id = sidecar.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise SchemaError(f"sidecar {sidecar_path.name}: invalid chat_id")
    session_id = "gem-" + chat_id.lower()
    title = sidecar.get("title")
    if title is not None and not isinstance(title, str):
        raise SchemaError(f"sidecar {sidecar_path.name}: invalid title")
    try:
        turns = _parse_manual(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ParseError(f"cannot read transcript {path.name}: {exc}") from exc
    return _InboxItem(
        transcript=path,
        sidecar=sidecar,
        session_id=session_id,
        course=(
            title.strip()
            if isinstance(title, str) and title.strip()
            else config.auto.default_course
        ),
        date=_sidecar_date(sidecar.get("updated_at")),
        sha256=turns_sha256(turns),
    )


def _is_complete(ws: Workspace, session: Session, approve: str) -> bool:
    try:
        if approve == "none":
            proposed = load_artifact(
                ws.proposed_decisions_path(session.session_id), DecisionFile, session
            )
            return proposed is not None
        result = load_artifact(ws.verify_path(session.session_id), VerifyResult, session)
        decisions = load_artifact(ws.decisions_path(session.session_id), DecisionFile, session)
        if result is None or decisions is None:
            return False
        apply_decisions(result, decisions)
        return True
    except (PendingReviewError, SchemaError, StaleArtifactError):
        return False


def _undo_ids(state: ProfileState, lines: Sequence[str]) -> list[str]:
    eligible = {item.id for item in state.instructions if item.status == "active"} | {
        item.id for item in state.hypotheses if item.status in ("open", "promoted")
    }
    return [target for target in eligible if any(line.endswith(f"({target})") for line in lines)]


def run_auto(
    ws: Workspace,
    config: Config,
    *,
    inbox: Path | None = None,
    idle_minutes: int | None = None,
    no_sync: bool = False,
    dry_run: bool = False,
    extractor: Extractor | None = None,
    reviewer: Reviewer | None = None,
    notifier: Notifier | None = None,
    syncer: Syncer | None = None,
    now: datetime | None = None,
) -> AutoResult:
    """Process finished capture-extension chats without a human approval gate."""
    current_time = now or datetime.now().astimezone()
    now_epoch = current_time.timestamp()
    idle = config.auto.idle_minutes if idle_minutes is None else idle_minutes
    if idle < 0:
        raise AutomaticModeError("idle minutes must be non-negative")
    inbox_path = (inbox or Path(config.auto.inbox)).expanduser()
    notify = notifier or notify_macos
    lock = _AutoLock(ws.auto_lock_path, now_epoch)
    if not lock.acquire():
        print("another run in progress")
        return AutoResult((), (), False, locked=True)

    processed: list[str] = []
    failed: list[str] = []
    changed = False
    try:
        before = _replayed(ws, config) if list_sessions(ws) else _empty_profile()
        state = before
        if not inbox_path.exists():
            paths: list[Path] = []
        elif not inbox_path.is_dir():
            raise AutomaticModeError(f"inbox is not a directory: {inbox_path}")
        else:
            paths = sorted(inbox_path.glob("gemini-*.md"))

        base = ws.base_path.read_text(encoding="utf-8") if ws.base_path.exists() else None
        effective_extractor = extractor
        effective_reviewer = reviewer or ClaudeReviewer(
            config.review.claude_model, config.review.timeout_s
        )

        for path in paths:
            sidecar_path = path.with_suffix(".json")
            if not sidecar_path.exists():
                continue
            try:
                cutoff = now_epoch - idle * 60
                if path.stat().st_mtime > cutoff or sidecar_path.stat().st_mtime > cutoff:
                    continue
                item = _inbox_item(path, config)
            except ParseError as exc:
                if "no learner turns" in str(exc):
                    continue
                failed.append(path.stem)
                if not dry_run:
                    _log(ws, f"{path.name}: {exc}", timestamp=current_time)
                if config.auto.notify and not dry_run:
                    notify(f"Automatic processing failed: {path.name}: {exc}")
                continue
            except (OSError, TutormemError) as exc:
                failed.append(path.stem)
                if not dry_run:
                    _log(ws, f"{path.name}: {exc}", timestamp=current_time)
                if config.auto.notify and not dry_run:
                    notify(f"Automatic processing failed: {path.name}: {exc}")
                continue

            existing = next(
                (session for session in list_sessions(ws) if session.session_id == item.session_id),
                None,
            )
            if existing is not None and existing.content_sha256 == item.sha256:
                if _is_complete(ws, existing, config.auto.approve):
                    continue
                action = "retry"
            else:
                action = "changed" if existing is not None else "new"

            if dry_run:
                print(f"would process {item.session_id} ({action})")
                processed.append(item.session_id)
                continue

            try:
                if action != "retry":
                    try:
                        session = ingest(
                            item.transcript,
                            ws,
                            course=item.course,
                            speakers="manual",
                            session_id=item.session_id,
                            date=item.date,
                            replace=action == "changed",
                        )
                    except DuplicateContentError as exc:
                        _log(ws, f"{item.session_id}: skipped duplicate content: {exc}")
                        continue
                    changed = True
                    state = _replayed(ws, config)
                else:
                    assert existing is not None
                    session = existing

                session_extractor = effective_extractor
                if session_extractor is None:
                    session_extractor = make_extractor(config.extract.extractor, config, base=base)
                extracted = session_extractor.extract(session, _open_items(state))
                write_json(ws.observations_path(session.session_id), extracted)
                verified = verify(session, extracted.observations)
                write_json(ws.verify_path(session.session_id), verified)
                packet = write_packet(session, verified, state)
                write_text(ws.review_path(session.session_id), packet)
                proposed = effective_reviewer.propose(packet, verified)
                write_json(ws.proposed_decisions_path(session.session_id), proposed)
                if config.auto.approve == "claude":
                    apply_decisions(verified, proposed)
                    write_json(ws.decisions_path(session.session_id), proposed)
                    state = _replayed(ws, config)
                processed.append(session.session_id)
                changed = True
            except (OSError, TutormemError) as exc:
                failed.append(item.session_id)
                _log(ws, f"{item.session_id}: {exc}", timestamp=current_time)
                if config.auto.notify:
                    notify(f"Automatic processing failed: {item.session_id}: {exc}")

        if dry_run:
            return AutoResult(tuple(processed), tuple(failed), False)

        if changed:
            after = _replayed(ws, config)
            _render(ws, after, config)
            if config.auto.sync and not no_sync:
                (syncer or sync_brief)(ws, config)
            lines = profile_diff(before, after, threshold=config.threshold)
            write_changelog(
                ws,
                config,
                processed,
                lines,
                timestamp=current_time,
                undo_ids=_undo_ids(after, lines),
            )
            if config.auto.notify and lines:
                notify(_notification_text(lines))
        return AutoResult(tuple(processed), tuple(failed), changed)
    except (OSError, ValueError) as exc:
        raise AutomaticModeError(f"automatic run failed: {exc}") from exc
    finally:
        lock.release()


def revoke(
    ws: Workspace,
    config: Config,
    target_id: str,
    *,
    reason: str = "",
    no_sync: bool = False,
    notifier: Notifier | None = None,
    syncer: Syncer | None = None,
    now: datetime | None = None,
) -> ProfileState:
    """Revoke one active profile item and rebuild all derived output."""
    before = _replayed(ws, config)
    instruction = next((item for item in before.instructions if item.id == target_id), None)
    hypothesis = next((item for item in before.hypotheses if item.id == target_id), None)
    if instruction is not None:
        valid = instruction.status == "active"
    elif hypothesis is not None:
        valid = hypothesis.status in ("open", "promoted")
    else:
        valid = False
    if not valid:
        raise AutomaticModeError(f"cannot revoke unknown, dropped, or revoked item: {target_id}")

    records = [record for record in before.sessions if record.status == "applied"]
    if not records:
        raise AutomaticModeError("cannot revoke without an applied session")
    latest = max(records, key=lambda record: record.index)
    session = next(item for item in list_sessions(ws) if item.session_id == latest.session_id)
    decisions = load_artifact(ws.decisions_path(session.session_id), DecisionFile, session)
    if decisions is None:
        raise AutomaticModeError(f"decision file not found for session: {session.session_id}")
    updated = DecisionFile(
        decisions.session_id,
        decisions.content_sha256,
        decisions.decisions,
        decisions.revocations + (Revocation(target_id, "human", reason),),
    )

    approvals = _approvals(ws)
    result = load_artifact(ws.verify_path(session.session_id), VerifyResult, session)
    if result is None:
        raise AutomaticModeError(f"verification not found for session: {session.session_id}")
    approvals[session.session_id] = apply_decisions(result, updated)
    after = replay(list_sessions(ws), approvals, config)
    write_json(ws.decisions_path(session.session_id), updated)
    _render(ws, after, config)
    if not no_sync:
        (syncer or sync_brief)(ws, config)
    lines = profile_diff(before, after, threshold=config.threshold)
    write_changelog(
        ws,
        config,
        (session.session_id,),
        lines,
        timestamp=now,
    )
    if config.auto.notify and lines:
        (notifier or notify_macos)(_notification_text(lines))
    return after


def _agent_runner(args: list[str]) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def install_agent(
    ws: Workspace,
    *,
    interval_minutes: int = 15,
    load: bool = False,
    home: Path | None = None,
    executable: str | None = None,
    uid: int | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    """Write and optionally load the per-user launchd agent."""
    if interval_minutes < 1:
        raise AutomaticModeError("interval minutes must be at least 1")
    user_home = (home or Path.home()).expanduser()
    resolved_executable = executable or shutil.which("tutormem") or sys.argv[0]
    resolved_executable = str(Path(resolved_executable).expanduser().resolve())
    path = user_home / "Library" / "LaunchAgents" / "com.tutormem.auto.plist"
    payload = {
        "Label": "com.tutormem.auto",
        "ProgramArguments": [resolved_executable, "--workspace", str(ws.root.resolve()), "auto"],
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str((ws.root / "auto.stdout.log").resolve()),
        "StandardErrorPath": str((ws.root / "auto.stderr.log").resolve()),
        "EnvironmentVariables": {
            "PATH": ":".join(
                [
                    str(user_home / ".local" / "bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                ]
            )
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, plistlib.dumps(payload, sort_keys=False).decode("utf-8"))
    user_id = os.getuid() if uid is None else uid
    command_runner = runner or _agent_runner
    load_command = ["launchctl", "bootstrap", f"gui/{user_id}", str(path)]
    print(path)
    print(" ".join(load_command))
    if load:
        command_runner(["launchctl", "bootout", f"gui/{user_id}", str(path)])
        completed = command_runner(load_command)
        if completed.returncode != 0:
            raise AutomaticModeError("launchctl bootstrap failed")
    return path


def uninstall_agent(
    *,
    home: Path | None = None,
    uid: int | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    """Unload and remove the per-user launchd agent."""
    user_home = (home or Path.home()).expanduser()
    path = user_home / "Library" / "LaunchAgents" / "com.tutormem.auto.plist"
    user_id = os.getuid() if uid is None else uid
    (runner or _agent_runner)(["launchctl", "bootout", f"gui/{user_id}", str(path)])
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path
