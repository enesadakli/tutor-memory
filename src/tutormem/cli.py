from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .errors import PendingReviewError, ReplayError, StaleArtifactError, TutormemError
from .extract import make_extractor
from .ingest import ingest
from .models import (
    DecisionFile,
    ExtractResult,
    Hypothesis,
    Instruction,
    ProfileState,
    VerifyResult,
)
from .promote import replay as replay_profile
from .render import render_brief, render_profile
from .review import ClaudeReviewer, apply_decisions, write_packet
from .storage import (
    Workspace,
    list_sessions,
    load_artifact,
    load_session,
    read_json,
    write_json,
    write_profile,
    write_text,
)
from .sync import sync as sync_brief
from .verify import verify


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tutormem")
    parser.add_argument("--workspace")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("path", type=Path)
    ingest_parser.add_argument("--course", required=True)
    ingest_parser.add_argument("--speakers", choices=("gemini", "manual"), default="gemini")
    ingest_parser.add_argument("--session-id")
    ingest_parser.add_argument("--date")
    ingest_parser.add_argument("--replace", action="store_true")

    extract_parser = commands.add_parser("extract")
    extract_parser.add_argument("session_id")
    extract_parser.add_argument("--extractor", choices=("agy", "file"))
    extract_parser.add_argument("--from", dest="from_path", type=Path)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("session_id")

    review_parser = commands.add_parser("review")
    review_parser.add_argument("session_id")
    review_parser.add_argument("--mode", choices=("packet", "claude"))

    approve_parser = commands.add_parser("approve")
    approve_parser.add_argument("session_id")

    commands.add_parser("replay")
    commands.add_parser("render")
    commands.add_parser("status")

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--extractor", choices=("agy", "file"))

    sync_parser = commands.add_parser("sync")
    sync_parser.add_argument("--dry-run", action="store_true")
    return parser


def _empty_profile() -> ProfileState:
    return ProfileState(
        schema_version=1, instructions=(), hypotheses=(), stuck_points=(), sessions=()
    )


def _load_profile(ws: Workspace, *, required: bool = False) -> ProfileState:
    if not ws.profile_path.exists():
        if required:
            raise ReplayError("profile state not found; run replay first")
        return _empty_profile()
    return ProfileState.from_dict(read_json(ws.profile_path))


def _open_items(state: ProfileState) -> tuple[Instruction | Hypothesis, ...]:
    instructions = tuple(item for item in state.instructions if item.status == "active")
    hypotheses = tuple(item for item in state.hypotheses if item.status in ("open", "promoted"))
    return instructions + hypotheses


def _extract_one(
    ws: Workspace,
    config: Config,
    session_id: str,
    extractor_name: str | None,
    from_path: Path | None,
) -> ExtractResult:
    session = load_session(ws, session_id)
    name = extractor_name or config.extract.extractor
    extractor = make_extractor(name, config, path=from_path)  # type: ignore[arg-type]
    result = extractor.extract(session, _open_items(_load_profile(ws)))
    write_json(ws.observations_path(session_id), result)
    return result


def _verify_one(ws: Workspace, session_id: str) -> VerifyResult:
    session = load_session(ws, session_id)
    extracted = load_artifact(ws.observations_path(session_id), ExtractResult, session)
    if extracted is None:
        raise ReplayError(f"observations not found for session: {session_id}")
    result = verify(session, extracted.observations)
    write_json(ws.verify_path(session_id), result)
    return result


def _review_one(ws: Workspace, config: Config, session_id: str, mode: str | None) -> None:
    session = load_session(ws, session_id)
    result = load_artifact(ws.verify_path(session_id), VerifyResult, session)
    if result is None:
        raise ReplayError(f"verification not found for session: {session_id}")
    packet = write_packet(session, result, _load_profile(ws))
    write_text(ws.review_path(session_id), packet)
    review_mode = mode or config.review.mode
    if review_mode == "claude":
        reviewer = ClaudeReviewer(config.review.claude_model, config.review.timeout_s)
        write_json(ws.proposed_decisions_path(session_id), reviewer.propose(packet, result))


def _approvals(ws: Workspace) -> dict[str, Any]:
    approvals: dict[str, Any] = {}
    for session in list_sessions(ws):
        try:
            result = load_artifact(ws.verify_path(session.session_id), VerifyResult, session)
            decisions = load_artifact(ws.decisions_path(session.session_id), DecisionFile, session)
            if result is None or decisions is None:
                approvals[session.session_id] = None
                continue
            approvals[session.session_id] = apply_decisions(result, decisions)
        except (PendingReviewError, StaleArtifactError):
            approvals[session.session_id] = None
    return approvals


def _replay(ws: Workspace, config: Config) -> ProfileState:
    state = replay_profile(list_sessions(ws), _approvals(ws), config)
    write_profile(ws, state)
    return state


def _render(ws: Workspace) -> None:
    state = _load_profile(ws, required=True)
    courses = ws.courses_path.read_text(encoding="utf-8") if ws.courses_path.exists() else None
    write_text(ws.out_dir / "profile.md", render_profile(state))
    write_text(ws.out_dir / "brief.md", render_brief(state, courses))


def _artifact_state(path: Path, cls: type[Any], session: Any) -> tuple[Any | None, bool]:
    try:
        return load_artifact(path, cls, session), False
    except StaleArtifactError:
        return None, True


def _status(ws: Workspace) -> None:
    profile = _load_profile(ws)
    records = {record.session_id: record for record in profile.sessions}
    for session in list_sessions(ws):
        stage = "ingested"
        stale = False
        pending = False
        extracted, item_stale = _artifact_state(
            ws.observations_path(session.session_id), ExtractResult, session
        )
        stale |= item_stale
        verified = None
        decisions = None
        if extracted is not None:
            stage = "extracted"
            verified, item_stale = _artifact_state(
                ws.verify_path(session.session_id), VerifyResult, session
            )
            stale |= item_stale
        if verified is not None:
            stage = "verified"
            decisions, item_stale = _artifact_state(
                ws.decisions_path(session.session_id), DecisionFile, session
            )
            stale |= item_stale
            pending = decisions is None
        if decisions is not None:
            stage = "reviewed"
        record = records.get(session.session_id)
        if record is not None:
            if record.status == "applied":
                stage = "applied"
                pending = False
            else:
                pending = True
        flags = ""
        if pending:
            flags += " [pending]"
        if stale:
            flags += " [stale]"
        print(f"{session.index:>3}  {session.session_id}  {stage}{flags}")


def _run(ws: Workspace, config: Config, extractor_name: str | None) -> None:
    for session in list_sessions(ws):
        try:
            extracted = load_artifact(
                ws.observations_path(session.session_id), ExtractResult, session
            )
        except StaleArtifactError:
            extracted = None
        if extracted is None:
            extracted = _extract_one(ws, config, session.session_id, extractor_name, None)
        try:
            result = load_artifact(ws.verify_path(session.session_id), VerifyResult, session)
        except StaleArtifactError:
            result = None
        if result is None:
            result = verify(session, extracted.observations)
            write_json(ws.verify_path(session.session_id), result)
        try:
            decisions = load_artifact(ws.decisions_path(session.session_id), DecisionFile, session)
        except StaleArtifactError:
            decisions = None
        if decisions is None:
            packet = write_packet(session, result, _load_profile(ws))
            write_text(ws.review_path(session.session_id), packet)
    state = _replay(ws, config)
    _render(ws)
    pending = [record.session_id for record in state.sessions if record.status == "pending"]
    if pending:
        print("pending: " + ", ".join(pending))


def _dispatch(args: argparse.Namespace) -> None:
    ws = Workspace.resolve(args.workspace)
    config = Config.load(ws)
    if args.command == "ingest":
        ingest(
            args.path,
            ws,
            course=args.course,
            speakers=args.speakers,
            session_id=args.session_id,
            date=args.date,
            replace=args.replace,
        )
    elif args.command == "extract":
        _extract_one(ws, config, args.session_id, args.extractor, args.from_path)
    elif args.command == "verify":
        _verify_one(ws, args.session_id)
    elif args.command == "review":
        _review_one(ws, config, args.session_id, args.mode)
    elif args.command == "approve":
        session = load_session(ws, args.session_id)
        decisions = load_artifact(
            ws.proposed_decisions_path(args.session_id), DecisionFile, session
        )
        if decisions is None:
            raise PendingReviewError(f"proposed decisions not found for session: {args.session_id}")
        write_json(ws.decisions_path(args.session_id), decisions)
    elif args.command == "replay":
        _replay(ws, config)
    elif args.command == "render":
        _render(ws)
    elif args.command == "status":
        _status(ws)
    elif args.command == "run":
        _run(ws, config, args.extractor)
    elif args.command == "sync":
        print(sync_brief(ws, config, dry_run=args.dry_run))


def main(argv: list[str] | None = None) -> int:
    """Run the tutor-memory command-line interface."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _dispatch(args)
    except TutormemError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
