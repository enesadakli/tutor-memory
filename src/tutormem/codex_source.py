from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import Config
from .errors import ParseError
from .models import Turn
from .storage import read_json, write_json, write_text

_LOGGER = logging.getLogger(__name__)
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False


@dataclass(frozen=True, slots=True)
class CodexRollout:
    session_id: str
    cwd: Path
    turns: tuple[Turn, ...]
    updated_at: str


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp must be ISO-8601")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def read_rollout(path: Path) -> CodexRollout | None:
    """Read learner and final tutor messages, ignoring injected context and bad lines."""
    metadata: tuple[str, Path, str] | None = None
    turns: list[Turn] = []
    updated_at: str | None = None
    with path.open("rb") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    continue
                payload = record["payload"]
                if record.get("type") == "session_meta":
                    source = payload.get("source")
                    if isinstance(source, dict) and "subagent" in source:
                        return None
                    if metadata is None:
                        session_id = payload["id"]
                        if not isinstance(session_id, str):
                            continue
                        if str(UUID(session_id)) != session_id.lower():
                            continue
                        cwd = payload["cwd"]
                        if not isinstance(cwd, str) or not cwd:
                            continue
                        metadata = (
                            session_id,
                            Path(cwd).expanduser().resolve(),
                            _timestamp(payload["timestamp"]),
                        )
                    continue
                if record.get("type") != "response_item" or payload.get("type") != "message":
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                text = "\n".join(
                    item["text"]
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
                if not text.strip():
                    continue
                role = payload.get("role")
                if role == "user":
                    kinds = payload.get("internal_chat_message_metadata_passthrough", {})
                    if isinstance(kinds, dict) and "content_item_kinds" in kinds:
                        if kinds["content_item_kinds"] != ["user.text"]:
                            continue
                    elif text.lstrip().startswith(("# AGENTS.md instructions", "<")):
                        continue
                    speaker = "learner"
                elif role == "assistant" and payload.get("phase", "final_answer") == "final_answer":
                    speaker = "tutor"
                else:
                    continue
                stamp = _timestamp(record["timestamp"])
                if turns and turns[-1].speaker == speaker:
                    turns[-1] = Turn(speaker, turns[-1].text + "\n\n" + text)
                else:
                    turns.append(Turn(speaker, text))
                updated_at = stamp
            except (json.JSONDecodeError, UnicodeError, KeyError, ValueError, TypeError):
                continue
    if metadata is None:
        raise ParseError(f"{path.name}: missing or invalid session_meta")
    if not any(turn.speaker == "learner" for turn in turns):
        return None
    session_id, cwd, _ = metadata
    assert updated_at is not None
    return CodexRollout(
        session_id, cwd, tuple(Turn(turn.speaker, turn.text.strip()) for turn in turns), updated_at
    )


def rollout_course(rollout: CodexRollout, config: Config) -> str | None:
    """Return the course under the first matching root, or None outside all roots."""
    for value in config.codex.roots:
        root = Path(value).expanduser().resolve()
        if rollout.cwd.is_relative_to(root):
            parts = rollout.cwd.relative_to(root).parts
            return parts[0] if parts else ""
    return None


def _write_capture(path: Path, rollout: CodexRollout, inbox: Path, course: str) -> bool:
    transcript = (
        json.dumps(
            {"format": "tutor-memory-turns/1", "turns": [turn.to_dict() for turn in rollout.turns]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    base = f"codex-{rollout.session_id}"
    sidecar_path = inbox / f"{base}.json"
    if sidecar_path.exists():
        try:
            existing = read_json(sidecar_path)
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and existing.get("transcript_sha256") == digest:
            return False
    stamp = path.stat().st_mtime
    transcript_path = inbox / f"{base}.turns.json"
    write_text(transcript_path, transcript)
    write_json(
        sidecar_path,
        {
            "source": "codex-cli",
            "chat_id": rollout.session_id,
            "title": course,
            "rollout": path.name,
            "turns": sum(turn.speaker == "learner" for turn in rollout.turns),
            "updated_at": rollout.updated_at,
            "transcript_sha256": digest,
        },
    )
    for target in (transcript_path, sidecar_path):
        os.utime(target, (stamp, stamp))
    return True


def capture_rollout(path: Path, config: Config, inbox: Path, *, course: str | None = None) -> bool:
    """Capture one rollout immediately, allowing back-fill outside configured roots."""
    path = path.expanduser()
    try:
        rollout = read_rollout(path)
    except ParseError:
        return False
    if rollout is None:
        return False
    title = course if course is not None else (rollout_course(rollout, config) or "")
    return _write_capture(path, rollout, inbox.expanduser(), title)


def capture_codex(
    config: Config,
    inbox: Path,
    *,
    now_epoch: float,
    idle_minutes: int,
) -> list[str]:
    """Capture idle study rollouts; invalid metadata is silent outside automatic mode."""
    written: list[str] = []
    oldest = now_epoch - config.codex.lookback_days * 86400
    cutoff = now_epoch - idle_minutes * 60
    directory = Path(config.codex.sessions_dir).expanduser()
    for path in sorted(directory.rglob("rollout-*.jsonl")):
        stamp = path.stat().st_mtime
        if stamp < oldest or stamp > cutoff:
            continue
        try:
            rollout = read_rollout(path)
        except ParseError as exc:
            _LOGGER.warning("%s", exc)
            continue
        if rollout is None:
            continue
        course = rollout_course(rollout, config)
        if course is not None and _write_capture(path, rollout, inbox.expanduser(), course):
            written.append(rollout.session_id)
    return written
