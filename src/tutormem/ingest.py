from pathlib import Path
from typing import Literal

from .models import Session
from .storage import Workspace


def slugify(stem: str) -> str:
    """Convert a file stem to a stable ASCII session id."""
    raise NotImplementedError


def ingest(
    path: Path,
    ws: Workspace,
    *,
    course: str,
    speakers: Literal["gemini", "manual"] = "gemini",
    session_id: str | None = None,
    date: str | None = None,
    replace: bool = False,
) -> Session:
    """Parse and persist a transcript as a canonical session."""
    raise NotImplementedError
