from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from tutormem.cli import main

FIXTURE = Path(__file__).parents[1] / "examples" / "workspace"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE, workspace)
    return workspace


def _manifest(workspace: Path) -> list[dict[str, str]]:
    return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))


def _run_pipeline(workspace: Path) -> None:
    for item in _manifest(workspace):
        sid = item["session_id"]
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
                    sid,
                ]
            )
            == 0
        )
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
        source = workspace / "decisions" / f"{sid}.json"
        if source.exists():
            destination = workspace / "runs" / sid / "decisions.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    assert main(["--workspace", str(workspace), "replay"]) == 0
    assert main(["--workspace", str(workspace), "render"]) == 0


@pytest.mark.xfail(raises=NotImplementedError, strict=True, reason="modules not implemented yet")
def test_full_pipeline(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _run_pipeline(workspace)
    actual = json.loads((workspace / "state" / "profile.json").read_text(encoding="utf-8"))
    expected = json.loads((workspace / "expected" / "profile.json").read_text(encoding="utf-8"))
    assert actual == expected
    assert (workspace / "out" / "brief.md").read_bytes() == (
        workspace / "expected" / "brief.md"
    ).read_bytes()
    s02 = json.loads((workspace / "runs" / "s02" / "verify.json").read_text())
    s03 = json.loads((workspace / "runs" / "s03" / "verify.json").read_text())
    assert s02["rejected"][0]["observation"]["id"] == "s02:3"
    assert s02["rejected"][0]["reason"] == "not_found"
    assert s03["rejected"][0]["observation"]["id"] == "s03:2"
    assert s03["rejected"][0]["reason"] == "tutor_turn"
    assert [item["exact"] for item in s03["verified"][0]["quotes"]] == [True, False]


@pytest.mark.xfail(raises=NotImplementedError, strict=True, reason="modules not implemented yet")
def test_duplicate_ingest_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    item = _manifest(workspace)[0]
    base = [
        "--workspace",
        str(workspace),
        "ingest",
        str(workspace / "inputs" / item["file"]),
        "--course",
        item["course"],
        "--speakers",
        "manual",
    ]
    assert main([*base, "--session-id", "s01"]) == 0
    assert main([*base, "--session-id", "s01-copy"]) == 1
    assert len(list((workspace / "sessions").glob("*.json"))) == 1


@pytest.mark.xfail(raises=NotImplementedError, strict=True, reason="modules not implemented yet")
def test_replace_invalidates_review(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _run_pipeline(workspace)
    replacement = workspace / "inputs" / "s07-replacement.md"
    replacement.write_text(
        (workspace / "inputs" / "s07.md").read_text(encoding="utf-8")
        + "\n### learner\nbir sorum daha var\n",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "ingest",
                str(replacement),
                "--course",
                "Deep Learning",
                "--speakers",
                "manual",
                "--session-id",
                "s07",
                "--replace",
            ]
        )
        == 0
    )
    assert main(["--workspace", str(workspace), "replay"]) == 0
    assert main(["--workspace", str(workspace), "render"]) == 0
    profile = json.loads((workspace / "state" / "profile.json").read_text(encoding="utf-8"))
    records = {item["session_id"]: item for item in profile["sessions"]}
    assert records["s07"]["status"] == "pending"
    assert records["s07"]["ordinal"] is None
    applied_ids = ("s01", "s02", "s03", "s05", "s06", "s08", "s09")
    assert [records[sid]["ordinal"] for sid in applied_ids] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
    ]
    hypotheses = {item["id"]: item for item in profile["hypotheses"]}
    instructions = {item["id"]: item for item in profile["instructions"]}
    assert "hyp-s07:1" not in hypotheses
    assert hypotheses["hyp-s02:1"]["dropped_at"] == 7
    assert instructions["ins-s06:1"]["revoked_at"] == 6
    assert "from 7 reviewed sessions" in (workspace / "out" / "brief.md").read_text()


def _manual_turns(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8")
    pieces = re.split(r"^### (learner|tutor)\s*$", text, flags=re.MULTILINE)
    turns: list[list[str]] = []
    for index in range(1, len(pieces), 2):
        body = re.sub(r"\n{3,}", "\n\n", pieces[index + 1].strip())
        turns.append([pieces[index], body])
    return turns


def test_fixture_hashes_match() -> None:
    for path in sorted((FIXTURE / "decisions").glob("*.json")):
        decisions = json.loads(path.read_text(encoding="utf-8"))
        turns = _manual_turns(FIXTURE / "inputs" / f"{path.stem}.md")
        payload = json.dumps(turns, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert decisions["content_sha256"] == digest
