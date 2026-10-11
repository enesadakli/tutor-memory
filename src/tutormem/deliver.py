from __future__ import annotations

import stat
from pathlib import Path

from .config import Config
from .errors import DeliverError
from .storage import Workspace, write_text

START = "<!-- tutor-memory:start -->"
END = "<!-- tutor-memory:end -->"


def deliver_agents_md(ws: Workspace, config: Config) -> bool:
    """Write the current brief into one managed section, preserving all outside bytes."""
    brief_path = ws.out_dir / "brief.md"
    if not config.deliver.agents_md or not brief_path.exists():
        return False
    path = Path(config.deliver.agents_md).expanduser()
    try:
        brief = brief_path.read_bytes().decode("utf-8")
        section = START + "\n" + brief + "\n" + END + "\n"
        existing = path.read_bytes().decode("utf-8") if path.exists() else None
        mode = stat.S_IMODE(path.stat().st_mode) if existing is not None else None
        if existing is None:
            updated = config.deliver.agents_md_header + "\n\n" + section
        elif START not in existing and END not in existing:
            updated = existing + "\n\n" + section
        else:
            if existing.count(START) != 1 or existing.count(END) != 1:
                raise DeliverError(f"malformed tutor-memory markers: {path}")
            lines = existing.splitlines(keepends=True)
            starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == START]
            ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
            if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
                raise DeliverError(f"malformed tutor-memory markers: {path}")
            updated = "".join(lines[: starts[0] + 1]) + brief + "\n" + "".join(lines[ends[0] :])
        if updated == existing:
            return False
        write_text(path, updated, mode=mode)
    except (OSError, UnicodeError) as exc:
        raise DeliverError(f"cannot deliver brief to {path}: {exc}") from exc
    return True
