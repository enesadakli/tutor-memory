from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _check(path: str) -> str:
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_private_artifacts_and_credentials_are_ignored_but_synthetic_files_are_not() -> None:
    for path in (
        "gemini-abc.turns.json",
        "workspace/gemini-abc.json",
        "base.md",
        "auto.stderr.log",
        "examples/token.json",
        "tests/fixtures/client_secret-real.json",
        "examples/workspace/credentials-live",
    ):
        output = _check(path)
        assert output
        pattern = output.split("\t", 1)[0].split(":", 2)[2]
        assert not pattern.startswith("!")

    synthetic = _check("examples/workspace/inputs/s01.md")
    assert synthetic and "!examples/workspace/inputs/s01.md" in synthetic
