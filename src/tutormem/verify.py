from collections.abc import Sequence

from .models import Observation, Session, VerifyResult


def normalize(text: str) -> tuple[str, list[int]]:
    """Normalize quote-search text and map normalized characters to source indices."""
    raise NotImplementedError


def verify(session: Session, observations: Sequence[Observation]) -> VerifyResult:
    """Verify every observation quote against learner turns."""
    raise NotImplementedError
