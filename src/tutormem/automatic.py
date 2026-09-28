from __future__ import annotations

import dataclasses
import fcntl
import getpass
import hashlib
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

from .config import Config, store_resolved_tools
from .errors import (
    AutomaticModeError,
    DuplicateContentError,
    ExtractorError,
    ParseError,
    PendingReviewError,
    SchemaError,
    StaleArtifactError,
    TutormemError,
)
from .extract import Extractor, ProgressExtractor, ProgressResult, ProgressRun, make_extractor
from .ingest import _MANUAL_HEADING_RE, _parse_json, _parse_manual, ingest
from .models import (
    DecisionFile,
    ExtractResult,
    Hypothesis,
    Instruction,
    ProfileState,
    Revocation,
    Session,
    Turn,
    VerifyResult,
    turns_sha256,
)
from .progress import apply_session, load_progress, render_progress, write_progress
from .promote import replay
from .render import render_brief, render_profile
from .review import ClaudeReviewer, apply_decisions, write_packet
from .security import sanitize_brief_text
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

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[Any]]
Notifier = Callable[[str], None]
Syncer = Callable[[Workspace, Config], str]


class Reviewer(Protocol):
    def propose(self, packet: str, result: VerifyResult) -> DecisionFile:
        """Return proposed decisions for a verified session."""
        raise NotImplementedError


class CourseProgressExtractor(Protocol):
    def extract(self, session_turns: Sequence[Turn], course_names: Sequence[str]) -> ProgressResult:
        """Return the course and continuation point for one session."""
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
    turns: tuple[Turn, ...]


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
    progress = load_progress(ws)
    progress_md = render_progress(progress, config.progress) if progress is not None else None
    write_profile(ws, state)
    write_text(ws.out_dir / "profile.md", render_profile(state, threshold=config.threshold))
    write_text(
        ws.out_dir / "brief.md",
        render_brief(state, courses, base_md=base, progress_md=progress_md),
    )


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
    (runner or _default_command_runner)(["/usr/bin/osascript", "-e", script])


def _notification_text(lines: Sequence[str]) -> str:
    shown = list(lines[:3])
    if len(lines) > 3:
        shown.append("…")
    return "\n".join(shown)


def _log(ws: Workspace, message: str, *, timestamp: datetime | None = None) -> None:
    stamp = (timestamp or datetime.now().astimezone()).astimezone().isoformat(timespec="seconds")
    _append(ws.auto_log_path, f"{stamp} {message}\n")


def _notification_errors(ws: Workspace) -> dict[str, str]:
    if not ws.auto_notified_path.exists():
        return {}
    try:
        value = json.loads(ws.auto_notified_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)
    }


def _record_failure_notification(
    ws: Workspace,
    session_id: str,
    error: BaseException,
    *,
    enabled: bool,
    notify: Notifier,
) -> None:
    if not enabled:
        return
    error_class = type(error).__name__
    errors = _notification_errors(ws)
    if errors.get(session_id) != error_class:
        notify(f"Automatic processing failed: {session_id}: {error_class}")
    if errors.get(session_id) != error_class:
        errors[session_id] = error_class
        write_json(ws.auto_notified_path, errors)


def _clear_failure_notification(ws: Workspace, session_id: str) -> None:
    errors = _notification_errors(ws)
    if session_id not in errors:
        return
    del errors[session_id]
    write_json(ws.auto_notified_path, errors)


class _AutoLock:
    def __init__(self, path: Path, now_epoch: float) -> None:
        self.path = path
        del now_epoch
        self.descriptor: int | None = None
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode())
        except OSError:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        self.acquired = True
        return True

    def release(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
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


def _sidecar_path(path: Path) -> Path:
    if path.name.endswith(".turns.json"):
        return path.with_name(path.name.removesuffix(".turns.json") + ".json")
    return path.with_suffix(".json")


def _path_session_id(path: Path) -> str:
    name = path.name.removesuffix(".turns.json").removesuffix(".md")
    return "gem-" + name.removeprefix("gemini-").lower()


def _inbox_item(path: Path, config: Config) -> _InboxItem:
    is_json = path.name.endswith(".turns.json")
    base_name = path.name.removesuffix(".turns.json") if is_json else path.stem
    sidecar_path = _sidecar_path(path)
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
    expected_name = f"gemini-{chat_id}"
    if base_name.lower() != expected_name.lower():
        raise SchemaError(f"sidecar {sidecar_path.name}: chat_id does not match filename")
    try:
        transcript_text = path.read_text(encoding="utf-8")
        if is_json:
            turns = _parse_json(transcript_text)
        else:
            turns = _parse_manual(transcript_text)
            expected_turns = sidecar.get("turns")
            if (
                type(expected_turns) is not int
                or expected_turns < 1
                or len(turns) != expected_turns * 2
                or any(
                    turn.speaker != ("learner" if index % 2 == 0 else "tutor")
                    for index, turn in enumerate(turns)
                )
            ):
                raise ParseError("legacy manual transcript has an unsafe role sentinel")
            if any(_MANUAL_HEADING_RE.search(turn.text) for turn in turns):
                raise ParseError("legacy manual transcript contains an unsafe role sentinel")
    except OSError as exc:
        raise ParseError(f"cannot read transcript {path.name}: {exc}") from exc
    expected_sha = sidecar.get("transcript_sha256")
    actual_sha = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()
    if is_json and (not isinstance(expected_sha, str) or expected_sha != actual_sha):
        raise SchemaError(f"sidecar {sidecar_path.name}: transcript_sha256 mismatch")
    if expected_sha is not None and expected_sha != actual_sha:
        raise SchemaError(f"sidecar {sidecar_path.name}: transcript_sha256 mismatch")
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
        turns=turns,
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


def _auto_approved(result: VerifyResult, proposed: DecisionFile) -> DecisionFile:
    observations = {item.observation.id: item.observation for item in result.verified}
    decisions = []
    for decision in proposed.decisions:
        observation = observations.get(decision.observation_id)
        claim = decision.claim
        if decision.action == "accept" and observation is not None:
            claim = sanitize_brief_text(claim if claim is not None else observation.claim, 200)
        decisions.append(dataclasses.replace(decision, claim=claim))
    return dataclasses.replace(proposed, decisions=tuple(decisions), revocations=())


def run_auto(
    ws: Workspace,
    config: Config,
    *,
    inbox: Path | None = None,
    idle_minutes: int | None = None,
    no_sync: bool = False,
    dry_run: bool = False,
    extractor: Extractor | None = None,
    progress_extractor: CourseProgressExtractor | None = None,
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
            json_paths = sorted(inbox_path.glob("gemini-*.turns.json"))
            json_bases = {path.name.removesuffix(".turns.json").lower() for path in json_paths}
            legacy_paths = [
                path
                for path in sorted(inbox_path.glob("gemini-*.md"))
                if path.stem.lower() not in json_bases
            ]
            paths = json_paths + legacy_paths

        base = (
            ws.base_path.read_text(encoding="utf-8")
            if config.extract.send_base and ws.base_path.exists()
            else None
        )
        effective_extractor = extractor
        effective_progress_extractor = progress_extractor
        effective_reviewer = reviewer or ClaudeReviewer(
            config.review.claude_model,
            config.review.timeout_s,
            executable=config.tools.claude,
        )
        progress_state = load_progress(ws) if config.progress.enabled else None
        progress_lines: list[str] = []

        for path in paths:
            sidecar_path = _sidecar_path(path)
            if not sidecar_path.exists():
                continue
            try:
                cutoff = now_epoch - idle * 60
                if path.stat().st_mtime > cutoff or sidecar_path.stat().st_mtime > cutoff:
                    continue
                item = _inbox_item(path, config)
            except ParseError as exc:
                if "learner turns" in str(exc):
                    continue
                failure_id = _path_session_id(path)
                failed.append(failure_id)
                if not dry_run:
                    _log(ws, f"{path.name}: {exc}", timestamp=current_time)
                    _record_failure_notification(
                        ws,
                        failure_id,
                        exc,
                        enabled=config.auto.notify,
                        notify=notify,
                    )
                continue
            except (OSError, TutormemError) as exc:
                failure_id = _path_session_id(path)
                failed.append(failure_id)
                if not dry_run:
                    _log(ws, f"{path.name}: {exc}", timestamp=current_time)
                    _record_failure_notification(
                        ws,
                        failure_id,
                        exc,
                        enabled=config.auto.notify,
                        notify=notify,
                    )
                continue

            existing = next(
                (session for session in list_sessions(ws) if session.session_id == item.session_id),
                None,
            )
            if existing is not None and existing.content_sha256 == item.sha256:
                if _is_complete(ws, existing, config.auto.approve):
                    if not dry_run:
                        _clear_failure_notification(ws, existing.session_id)
                    continue
                action = "retry"
            else:
                action = "changed" if existing is not None else "new"

            if dry_run:
                print(f"would process {item.session_id} ({action})")
                processed.append(item.session_id)
                continue

            progress_result: ProgressResult | None = None
            if progress_state is not None:
                if action == "retry":
                    assert existing is not None
                    try:
                        cached_progress = load_artifact(
                            ws.progress_result_path(existing.session_id), ProgressRun, existing
                        )
                    except (SchemaError, StaleArtifactError) as exc:
                        _log(
                            ws,
                            f"{item.session_id}: progress cache ignored: {exc}",
                            timestamp=current_time,
                        )
                    else:
                        if cached_progress is not None:
                            progress_result = cached_progress.result()
                else:
                    try:
                        if effective_progress_extractor is None:
                            assert config.progress.model is not None
                            effective_progress_extractor = ProgressExtractor(
                                config.progress.model,
                                config.extract.timeout_s,
                                language=config.progress.language,
                                executable=config.tools.agy,
                            )
                        progress_result = effective_progress_extractor.extract(
                            item.turns,
                            tuple(course.name for course in progress_state.courses),
                        )
                    except ExtractorError as exc:
                        _log(
                            ws,
                            f"{item.session_id}: progress extraction failed: {exc}",
                            timestamp=current_time,
                        )

            if progress_result is not None:
                progress_result = ProgressResult(
                    progress_result.course,
                    sanitize_brief_text(progress_result.covered, 200),
                    sanitize_brief_text(progress_result.next, 160),
                )

            try:
                if action != "retry":
                    course = (
                        progress_result.course
                        if progress_result is not None and progress_result.course is not None
                        else item.course
                    )
                    try:
                        session = ingest(
                            item.transcript,
                            ws,
                            course=course,
                            speakers="json"
                            if item.transcript.name.endswith(".turns.json")
                            else "manual",
                            session_id=item.session_id,
                            date=item.date,
                            replace=action == "changed",
                        )
                    except DuplicateContentError as exc:
                        _log(ws, f"{item.session_id}: skipped duplicate content: {exc}")
                        continue
                    changed = True
                    state = _replayed(ws, config)
                    if progress_result is not None:
                        write_json(
                            ws.progress_result_path(session.session_id),
                            ProgressRun.from_result(
                                session.session_id,
                                session.content_sha256,
                                progress_result,
                            ),
                        )
                else:
                    assert existing is not None
                    session = existing

                try:
                    extracted = load_artifact(
                        ws.observations_path(session.session_id), ExtractResult, session
                    )
                    verified = load_artifact(
                        ws.verify_path(session.session_id), VerifyResult, session
                    )
                except StaleArtifactError:
                    extracted = None
                    verified = None
                if extracted is None or verified is None:
                    session_extractor = effective_extractor
                    if session_extractor is None:
                        session_extractor = make_extractor(
                            config.extract.extractor, config, base=base
                        )
                    extracted = session_extractor.extract(session, _open_items(state))
                    write_json(ws.observations_path(session.session_id), extracted)
                    verified = verify(session, extracted.observations)
                    write_json(ws.verify_path(session.session_id), verified)
                packet = write_packet(session, verified, state)
                write_text(ws.review_path(session.session_id), packet)
                proposed = effective_reviewer.propose(packet, verified)
                write_json(ws.proposed_decisions_path(session.session_id), proposed)
                if config.auto.approve == "claude":
                    approved = _auto_approved(verified, proposed)
                    apply_decisions(verified, approved)
                    write_json(ws.decisions_path(session.session_id), approved)
                    for revocation in proposed.revocations:
                        progress_lines.append(
                            "ignored revocation proposal: "
                            f"{revocation.target_id} "
                            f"({sanitize_brief_text(revocation.reason, 160) or 'no reason'})"
                        )
                    state = _replayed(ws, config)
                    if progress_state is not None and progress_result is not None:
                        updated_progress = apply_session(
                            progress_state,
                            session.course,
                            session.session_id,
                            session.index,
                            session.date,
                            progress_result.covered,
                            progress_result.next,
                        )
                        if updated_progress != progress_state:
                            progress_state = updated_progress
                            write_progress(ws, progress_state)
                            progress_lines.append(
                                f"Progress: {session.course} — {progress_result.covered} "
                                f"→ next: {progress_result.next}"
                            )
                processed.append(session.session_id)
                changed = True
                _clear_failure_notification(ws, session.session_id)
            except (OSError, TutormemError) as exc:
                failed.append(item.session_id)
                _log(ws, f"{item.session_id}: {exc}", timestamp=current_time)
                _record_failure_notification(
                    ws,
                    item.session_id,
                    exc,
                    enabled=config.auto.notify,
                    notify=notify,
                )

        if dry_run:
            return AutoResult(tuple(processed), tuple(failed), False)

        if changed:
            after = _replayed(ws, config)
            _render(ws, after, config)
            if config.auto.sync and not no_sync:
                (syncer or sync_brief)(ws, config)
            lines = profile_diff(before, after, threshold=config.threshold) + progress_lines
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
    ws.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store_resolved_tools(ws)
    for log_name in ("auto.stdout.log", "auto.stderr.log"):
        descriptor = os.open(ws.root / log_name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(descriptor)
    path = user_home / "Library" / "LaunchAgents" / "com.tutormem.auto.plist"
    payload = {
        "Label": "com.tutormem.auto",
        "ProgramArguments": [resolved_executable, "--workspace", str(ws.root.resolve()), "auto"],
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str((ws.root / "auto.stdout.log").resolve()),
        "StandardErrorPath": str((ws.root / "auto.stderr.log").resolve()),
        "EnvironmentVariables": {
            "USER": getpass.getuser(),
            "LOGNAME": getpass.getuser(),
            "HOME": str(user_home),
            "PATH": ":".join(
                [
                    str(user_home / ".local" / "bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                ]
            ),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, plistlib.dumps(payload, sort_keys=False).decode("utf-8"))
    user_id = os.getuid() if uid is None else uid
    command_runner = runner or _agent_runner
    load_command = ["/bin/launchctl", "bootstrap", f"gui/{user_id}", str(path)]
    print(path)
    print(" ".join(load_command))
    if load:
        command_runner(["/bin/launchctl", "bootout", f"gui/{user_id}", str(path)])
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
    (runner or _agent_runner)(["/bin/launchctl", "bootout", f"gui/{user_id}", str(path)])
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path
