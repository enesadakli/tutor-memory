from collections.abc import Mapping, Sequence

from .config import Config
from .models import ProfileState, Session, SessionApproval


def replay(
    sessions: Sequence[Session],
    approvals: Mapping[str, SessionApproval | None],
    config: Config,
) -> ProfileState:
    """Recompute profile state from canonical sessions and approvals."""
    raise NotImplementedError
