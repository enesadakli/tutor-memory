from __future__ import annotations

from pathlib import Path

import pytest

from tutormem.config import Config, DeliverConfig
from tutormem.deliver import END, START, deliver_agents_md
from tutormem.errors import DeliverError
from tutormem.storage import Workspace, write_text


def _brief(tmp_path: Path) -> tuple[Workspace, Config, Path]:
    ws = Workspace(tmp_path / "ws")
    write_text(ws.out_dir / "brief.md", "# Brief\n\nAdım adım.\n")
    target = tmp_path / "study" / "AGENTS.md"
    return ws, Config(deliver=DeliverConfig(agents_md=str(target))), target


def test_create_and_idempotent_delivery(tmp_path: Path) -> None:
    ws, config, target = _brief(tmp_path)
    assert deliver_agents_md(ws, config) is True
    assert target.read_text(encoding="utf-8") == (
        config.deliver.agents_md_header
        + "\n\n"
        + START
        + "\n# Brief\n\nAdım adım.\n\n"
        + END
        + "\n"
    )
    inode = target.stat().st_ino
    assert deliver_agents_md(ws, config) is False
    assert target.stat().st_ino == inode


def test_replace_preserves_outside_bytes_and_mode(tmp_path: Path) -> None:
    ws, config, target = _brief(tmp_path)
    target.parent.mkdir()
    prefix = b"# My instructions\r\nKeep these.\r\n" + START.encode() + b"\r\n"
    suffix = END.encode() + b"\r\n\r\nAfter: keep this too."
    target.write_bytes(prefix + b"Old brief\r\n" + suffix)
    target.chmod(0o640)
    assert deliver_agents_md(ws, config) is True
    assert target.read_bytes() == prefix + (ws.out_dir / "brief.md").read_bytes() + b"\n" + suffix
    assert target.stat().st_mode & 0o777 == 0o640
    assert deliver_agents_md(ws, config) is False


@pytest.mark.parametrize("existing", ["", "# Rules\nKeep these.", "# Rules\r\nKeep these.\r\n"])
def test_append_preserves_existing_text(tmp_path: Path, existing: str) -> None:
    ws, config, target = _brief(tmp_path)
    target.parent.mkdir()
    target.write_bytes(existing.encode())
    assert deliver_agents_md(ws, config) is True
    assert (
        target.read_bytes()
        == (existing + "\n\n" + START + "\n# Brief\n\nAdım adım.\n\n" + END + "\n").encode()
    )


@pytest.mark.parametrize(
    "existing",
    [
        START,
        END,
        END + "\n" + START,
        START + "\n" + START + "\n" + END,
        START + "\n" + END + "\n" + END,
        "inline " + START + "\n" + END,
        START + "\ninline " + END,
    ],
)
def test_malformed_markers_leave_file_untouched(tmp_path: Path, existing: str) -> None:
    ws, config, target = _brief(tmp_path)
    target.parent.mkdir()
    target.write_bytes(existing.encode())
    original = target.read_bytes()
    inode = target.stat().st_ino
    with pytest.raises(DeliverError, match="malformed"):
        deliver_agents_md(ws, config)
    assert target.read_bytes() == original
    assert target.stat().st_ino == inode


def test_unconfigured_or_missing_brief_is_noop(tmp_path: Path) -> None:
    ws, config, target = _brief(tmp_path)
    assert deliver_agents_md(ws, Config()) is False
    (ws.out_dir / "brief.md").unlink()
    assert deliver_agents_md(ws, config) is False
    assert not target.exists()


def test_delivery_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws, _, _ = _brief(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert deliver_agents_md(ws, Config(deliver=DeliverConfig(agents_md="~/AGENTS.md"))) is True
    assert (tmp_path / "AGENTS.md").exists()


def test_delivery_io_failure_is_typed(tmp_path: Path) -> None:
    ws, config, target = _brief(tmp_path)
    target.mkdir(parents=True)
    with pytest.raises(DeliverError, match="cannot deliver"):
        deliver_agents_md(ws, config)
