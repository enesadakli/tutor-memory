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
