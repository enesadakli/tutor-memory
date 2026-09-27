from typing import Any

from .config import Config
from .storage import Workspace


def sync(ws: Workspace, config: Config, *, dry_run: bool = False, drive: Any = None) -> str:
    """Create or update the configured Google Doc with the rendered brief."""
    raise NotImplementedError
