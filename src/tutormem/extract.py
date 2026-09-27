from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from .config import Config
from .models import ExtractResult, Hypothesis, Instruction, Session


class Extractor(Protocol):
    """Contract implemented by observation extractors."""

    name: str
    model: str | None

    def extract(
        self, session: Session, open_items: Sequence[Instruction | Hypothesis]
    ) -> ExtractResult:
        """Extract proposed observations from one session."""
        raise NotImplementedError


class FileExtractor:
    """Read deterministic extractor output from a JSON file."""

    name = "file"
    model = None

    def __init__(self, path: Path) -> None:
        self.path = path

    def extract(
        self, session: Session, open_items: Sequence[Instruction | Hypothesis]
    ) -> ExtractResult:
        """Extract observations from the configured JSON file."""
        raise NotImplementedError


class AgyExtractor:
    """Extract observations with the configured Antigravity command."""

    name = "agy"

    def __init__(self, model: str, timeout_s: int) -> None:
        self.model = model
        self.timeout_s = timeout_s

    def extract(
        self, session: Session, open_items: Sequence[Instruction | Hypothesis]
    ) -> ExtractResult:
        """Extract observations from a canonical transcript."""
        raise NotImplementedError


def make_extractor(
    name: Literal["agy", "file"], config: Config, *, path: Path | None = None
) -> Extractor:
    """Create an extractor from CLI and configuration values."""
    raise NotImplementedError
