from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tutormem.cli import main

FIXTURE = Path(__file__).parents[1] / "examples" / "workspace"


def _copy_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE, workspace)
    return workspace


def _manifest(workspace: Path) -> list[dict[str, str]]:
    return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))


def _ingest(workspace: Path, item: dict[str, str]) -> None:
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "ingest",
                str(workspace / "inputs" / item["file"]),
                "--course",
                item["course"],
                "--speakers",
                "manual",
                "--session-id",
                item["session_id"],
            ]
        )
        == 0
    )


def _extract_and_verify(workspace: Path, sid: str) -> None:
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "extract",
                sid,
                "--extractor",
                "file",
                "--from",
                str(workspace / "observations" / f"{sid}.json"),
            ]
        )
        == 0
    )
    assert main(["--workspace", str(workspace), "verify", sid]) == 0


def test_run_example_workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = _copy_workspace(tmp_path)
    for item in _manifest(workspace):
        _ingest(workspace, item)
        sid = item["session_id"]
        source = workspace / "decisions" / f"{sid}.json"
        if source.exists():
            destination = workspace / "runs" / sid / "decisions.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "run",
                "--extractor",
                "file",
                "--observations-dir",
                str(workspace / "observations"),
            ]
        )
        == 0
    )
    assert (workspace / "out" / "brief.md").read_bytes() == (
        workspace / "expected" / "brief.md"
    ).read_bytes()

    capsys.readouterr()
    assert main(["--workspace", str(workspace), "status"]) == 0
    status = capsys.readouterr().out
    assert "s04" in status
    assert "[pending]" in next(line for line in status.splitlines() if "s04" in line)


def test_approve_copies_proposed_decisions(tmp_path: Path) -> None:
    workspace = _copy_workspace(tmp_path)
    _ingest(workspace, _manifest(workspace)[0])
    proposed = workspace / "runs" / "s01" / "decisions.proposed.json"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads((workspace / "decisions" / "s01.json").read_text(encoding="utf-8"))
    payload["revocations"] = []
    proposed.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert main(["--workspace", str(workspace), "approve", "s01"]) == 0
    assert (workspace / "runs" / "s01" / "decisions.json").read_bytes() == proposed.read_bytes()


def test_approve_refuses_stale_proposed_decisions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _copy_workspace(tmp_path)
    _ingest(workspace, _manifest(workspace)[0])
    payload = json.loads((workspace / "decisions" / "s01.json").read_text(encoding="utf-8"))
    payload["content_sha256"] = "f" * 64
    proposed = workspace / "runs" / "s01" / "decisions.proposed.json"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    proposed.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--workspace", str(workspace), "approve", "s01"]) == 1
    assert "stale artifact" in capsys.readouterr().err
    assert not (workspace / "runs" / "s01" / "decisions.json").exists()


def test_review_packet_writes_review_markdown(tmp_path: Path) -> None:
    workspace = _copy_workspace(tmp_path)
    _ingest(workspace, _manifest(workspace)[0])
    _extract_and_verify(workspace, "s01")

    assert main(["--workspace", str(workspace), "review", "s01", "--mode", "packet"]) == 0
    packet = workspace / "runs" / "s01" / "review.md"
    assert packet.exists()
    assert "# Review packet: s01" in packet.read_text(encoding="utf-8")


def test_deliver_writes_current_brief_without_rendering(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    (tmp_path / "tutormem.toml").write_text(
        f'[deliver]\nagents_md = "{target}"\n', encoding="utf-8"
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "brief.md").write_text("# Existing brief\n", encoding="utf-8")
    assert main(["--workspace", str(tmp_path), "deliver"]) == 0
    assert "# Existing brief" in target.read_text(encoding="utf-8")
    assert not (tmp_path / "state").exists()


def test_deliver_reports_malformed_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("<!-- tutor-memory:start -->", encoding="utf-8")
    (tmp_path / "tutormem.toml").write_text(
        f'[deliver]\nagents_md = "{target}"\n', encoding="utf-8"
    )
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "brief.md").write_text("Brief", encoding="utf-8")
    assert main(["--workspace", str(tmp_path), "deliver"]) == 1
    assert "malformed" in capsys.readouterr().err


def test_codex_capture_course_override_backfills_outside_roots(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (tmp_path / "tutormem.toml").write_text(f'[auto]\ninbox = "{inbox}"\n', encoding="utf-8")
    sid = "12345678-1234-1234-1234-123456789abc"
    path = tmp_path / "rollout-test.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": sid,
                "timestamp": "2026-10-10T12:00:00Z",
                "cwd": str(tmp_path / "outside"),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-10-10T12:01:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Question"}],
                "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]},
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    assert main(["--workspace", str(tmp_path), "codex-capture", str(path), "--course", "OOAD"]) == 0
    assert json.loads((inbox / f"codex-{sid}.json").read_text(encoding="utf-8"))["title"] == "OOAD"
    assert (inbox / f"codex-{sid}.turns.json").exists()
    assert not (tmp_path / "sessions").exists()


def test_codex_capture_ignores_invalid_metadata_silently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox = tmp_path / "inbox"
    (tmp_path / "tutormem.toml").write_text(f'[auto]\ninbox = "{inbox}"\n', encoding="utf-8")
    path = tmp_path / "rollout-test.jsonl"
    path.write_text("{}\ninvalid\n", encoding="utf-8")
    assert main(["--workspace", str(tmp_path), "codex-capture", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert not inbox.exists()
