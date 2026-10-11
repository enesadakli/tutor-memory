from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tutormem.codex_source import capture_codex, capture_rollout, read_rollout
from tutormem.config import CodexConfig, Config
from tutormem.errors import ParseError
from tutormem.models import Turn
from tutormem.storage import read_json

SID = "12345678-1234-1234-1234-123456789abc"
STAMP = "2026-10-10T12:00:00Z"
NOW = 1791720000.0


def _message(role: str, text: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": "response_item",
        "timestamp": STAMP,
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            **fields,
        },
    }


def _user(text: str, kinds: list[str] | None = None) -> dict[str, Any]:
    return _message(
        "user",
        text,
        internal_chat_message_metadata_passthrough={
            "content_item_kinds": ["user.text"] if kinds is None else kinds
        },
    )


def _rollout(
    path: Path,
    cwd: Path,
    messages: list[Any] | None = None,
    *,
    age_minutes: int = 30,
    **metadata_fields: Any,
) -> Path:
    records = [
        {
            "type": "session_meta",
            "payload": {"id": SID, "timestamp": STAMP, "cwd": str(cwd), **metadata_fields},
        },
        *(
            messages
            if messages is not None
            else [_user("Adım adım anlat."), _message("assistant", "Tamam.")]
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    stamp = NOW - age_minutes * 60
    os.utime(path, (stamp, stamp))
    return path


def _config(tmp_path: Path, roots: tuple[str, ...] | None = None) -> Config:
    return Config(
        codex=CodexConfig(
            sessions_dir=str(tmp_path / "sessions"),
            roots=roots if roots is not None else (str(tmp_path / "Dersler"),),
        )
    )


def _capture(config: Config, inbox: Path) -> list[str]:
    return capture_codex(config, inbox, now_epoch=NOW, idle_minutes=20)


def test_extraction_filters_context_and_merges_consecutive_messages(tmp_path: Path) -> None:
    multi = _user(" Bir ")
    multi["payload"]["content"].append({"type": "input_text", "text": "iki"})
    path = _rollout(
        tmp_path / "rollout-test.jsonl",
        tmp_path,
        [
            _user("# AGENTS.md instructions", ["agents_md.instructions"]),
            _user("environment", ["environments.environment_context"]),
            _user("mixed", ["user.text", "agents_md.instructions"]),
            multi,
            _message("assistant", "Working...", phase="commentary"),
            _message("developer", "injected"),
            _user(" üç "),
            _message("assistant", " Dört ", phase="final_answer"),
            {"type": "response_item", "payload": {"type": "function_call", "name": "tool"}},
            {"type": "event_msg", "payload": {"type": "message", "role": "user"}},
            _message("assistant", "Beş"),
            _user("  "),
        ],
    )
    result = read_rollout(path)
    assert result is not None
    assert result.turns == (Turn("learner", "Bir \niki\n\n üç"), Turn("tutor", "Dört \n\nBeş"))
    assert result.session_id == SID


def test_extraction_fallback_malformed_lines_and_last_kept_timestamp(tmp_path: Path) -> None:
    last = _message("assistant", "Answer")
    last["timestamp"] = "2026-10-10T13:00:00Z"
    ignored = _message("assistant", "Still working", phase="commentary")
    ignored["timestamp"] = "2026-10-10T14:00:00Z"
    path = _rollout(
        tmp_path / "rollout-test.jsonl",
        tmp_path,
        [
            _message("user", "# AGENTS.md instructions\nignore"),
            _message("user", "<environment_context>ignore</environment_context>"),
            _message("user", "  Fallback question.  "),
            last,
            ignored,
        ],
    )
    with path.open("ab") as handle:
        handle.write(b'broken JSON\n\xff\n[]\n{"type":"response_item","payload":null}\n')
    result = read_rollout(path)
    assert result is not None
    assert result.turns == (Turn("learner", "Fallback question."), Turn("tutor", "Answer"))
    assert result.updated_at == last["timestamp"]


def test_capture_no_learner_is_silent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _rollout(
        tmp_path / "sessions" / "rollout-test.jsonl",
        tmp_path / "Dersler",
        [_message("assistant", "Only tutor"), _user("Context", ["agents_md.instructions"])],
    )
    inbox = tmp_path / "inbox"
    assert _capture(config, inbox) == []
    assert not inbox.exists()


@pytest.mark.parametrize("metadata_fields", [{}, {"thread_source": "internal"}])
def test_subagent_rollout_is_skipped_silently(
    tmp_path: Path, metadata_fields: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = _rollout(
        tmp_path / "sessions" / "rollout-test.jsonl",
        tmp_path / "Dersler",
        [_user("Parent session transcript")],
        source={"subagent": {"other": "guardian"}},
        **metadata_fields,
    )
    assert read_rollout(path) is None
    inbox = tmp_path / "inbox"
    assert _capture(_config(tmp_path), inbox) == []
    assert not inbox.exists()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_exec_rollout_is_skipped_silently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _rollout(
        tmp_path / "sessions" / "rollout-test.jsonl",
        tmp_path / "Dersler",
        [_user("Run the automation lane.")],
        source="exec",
        originator="codex_exec",
    )
    assert read_rollout(path) is None
    inbox = tmp_path / "inbox"
    config = _config(tmp_path)
    assert _capture(config, inbox) == []
    assert capture_rollout(path, config, inbox, course="Backfill") is False
    assert not inbox.exists()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("source", ["vscode", "cli"])
@pytest.mark.parametrize("metadata_fields", [{}, {"thread_source": "user"}])
def test_interactive_rollout_is_captured(
    tmp_path: Path, source: str, metadata_fields: dict[str, Any]
) -> None:
    path = _rollout(
        tmp_path / "sessions" / "rollout-test.jsonl",
        tmp_path / "Dersler",
        source=source,
        **metadata_fields,
    )
    result = read_rollout(path)
    assert result is not None
    assert result.turns == (Turn("learner", "Adım adım anlat."), Turn("tutor", "Tamam."))
    inbox = tmp_path / "inbox"
    assert _capture(_config(tmp_path), inbox) == [SID]
    assert read_json(inbox / f"codex-{SID}.json")["turns"] == 1


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"id": "bad", "cwd": "/study", "timestamp": STAMP},
        {"id": SID, "cwd": "/study", "timestamp": "invalid"},
    ],
)
def test_invalid_metadata_is_ignored_silently(
    tmp_path: Path, metadata: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "sessions" / "rollout-bad.jsonl"
    path.parent.mkdir()
    records = [_user("Question")]
    if metadata is not None:
        records.insert(0, {"type": "session_meta", "payload": metadata})
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    os.utime(path, (NOW - 1800, NOW - 1800))
    assert _capture(_config(tmp_path), tmp_path / "inbox") == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    with pytest.raises(ParseError, match="session_meta"):
        read_rollout(path)
    assert not (tmp_path / "inbox").exists()


@pytest.mark.parametrize(
    "relative,title", [("OOAD/notes", "OOAD"), ("", ""), ("../Dersler-other/OOAD", None)]
)
def test_root_filter_and_course_derivation(
    tmp_path: Path, relative: str, title: str | None
) -> None:
    config = _config(tmp_path)
    _rollout(
        tmp_path / "sessions" / "2026" / "10" / "10" / "rollout-test.jsonl",
        tmp_path / "Dersler" / relative,
    )
    inbox = tmp_path / "inbox"
    assert _capture(config, inbox) == ([] if title is None else [SID])
    if title is not None:
        sidecar = read_json(inbox / f"codex-{SID}.json")
        assert sidecar["title"] == title
        assert sidecar["rollout"] == "rollout-test.jsonl"
        assert sidecar["turns"] == 1
        assert sidecar["source"] == "codex-cli"


def test_roots_and_cwd_resolve_symlinks_and_expand_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Dersler"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _config(tmp_path, ("~/alias",))
    assert config.codex.roots == (str(root),)
    _rollout(tmp_path / "sessions" / "rollout-test.jsonl", alias / "OOAD" / "notes")
    assert _capture(config, tmp_path / "inbox") == [SID]
    assert read_json(tmp_path / "inbox" / f"codex-{SID}.json")["title"] == "OOAD"


@pytest.mark.parametrize("age_minutes", [5, 15 * 24 * 60])
def test_capture_skips_active_and_old_rollouts(tmp_path: Path, age_minutes: int) -> None:
    _rollout(
        tmp_path / "sessions" / "rollout-test.jsonl", tmp_path / "Dersler", age_minutes=age_minutes
    )
    assert _capture(_config(tmp_path), tmp_path / "inbox") == []
    assert not (tmp_path / "inbox").exists()


def test_capture_private_format_mtimes_idempotence_and_growth(tmp_path: Path) -> None:
    path = _rollout(tmp_path / "sessions" / "rollout-test.jsonl", tmp_path / "Dersler" / "OOAD")
    inbox = tmp_path / "inbox"
    config = _config(tmp_path)
    assert _capture(config, inbox) == [SID]
    transcript = inbox / f"codex-{SID}.turns.json"
    sidecar = inbox / f"codex-{SID}.json"
    payload = read_json(transcript)
    assert (
        transcript.read_text(encoding="utf-8")
        == json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )
    assert "Adım" in transcript.read_text(encoding="utf-8")
    assert (
        read_json(sidecar)["transcript_sha256"]
        == hashlib.sha256(transcript.read_bytes()).hexdigest()
    )
    assert inbox.stat().st_mode & 0o777 == 0o700
    for target in (transcript, sidecar):
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.stat().st_mtime == path.stat().st_mtime
        assert target.stat().st_atime == path.stat().st_mtime
    original = (transcript.read_bytes(), sidecar.read_bytes())
    original_inodes = (transcript.stat().st_ino, sidecar.stat().st_ino)
    assert _capture(config, inbox) == []
    assert (transcript.stat().st_ino, sidecar.stat().st_ino) == original_inodes
    assert (transcript.read_bytes(), sidecar.read_bytes()) == original
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_user("Bir örnek daha.")) + "\n")
    os.utime(path, (NOW - 1200, NOW - 1200))
    assert _capture(config, inbox) == [SID]
    assert transcript.read_bytes() != original[0]
    assert read_json(sidecar)["turns"] == 2
    for target in (transcript, sidecar):
        assert target.stat().st_mtime == path.stat().st_mtime
        assert target.stat().st_mode & 0o777 == 0o600


def test_immediate_capture_ignores_roots_and_idle_and_derives_title(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _rollout(tmp_path / "rollout-test.jsonl", tmp_path / "outside", age_minutes=0)
    inbox = tmp_path / "inbox"
    assert capture_rollout(path, config, inbox, course="Backfill") is True
    assert read_json(inbox / f"codex-{SID}.json")["title"] == "Backfill"
    assert capture_rollout(path, config, inbox) is False
    nested = _rollout(tmp_path / "rollout-nested.jsonl", tmp_path / "Dersler" / "OOAD")
    derived_inbox = tmp_path / "derived"
    assert capture_rollout(nested, config, derived_inbox) is True
    assert read_json(derived_inbox / f"codex-{SID}.json")["title"] == "OOAD"


def test_explicit_user_text_keeps_context_looking_text(tmp_path: Path) -> None:
    path = _rollout(
        tmp_path / "rollout-test.jsonl",
        tmp_path,
        [_user("# AGENTS.md instructions\nQuoted by the learner.")],
    )
    result = read_rollout(path)
    assert result is not None
    assert result.turns == (Turn("learner", "# AGENTS.md instructions\nQuoted by the learner."),)


def test_immediate_capture_ignores_invalid_metadata_without_output(tmp_path: Path) -> None:
    path = tmp_path / "rollout-test.jsonl"
    path.write_text(json.dumps(_user("Question")) + "\n", encoding="utf-8")
    assert capture_rollout(path, _config(tmp_path), tmp_path / "inbox") is False
    assert not (tmp_path / "inbox").exists()
