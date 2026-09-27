from .models import DecisionFile, ProfileState, Session, SessionApproval, VerifyResult


def write_packet(session: Session, result: VerifyResult, profile: ProfileState) -> str:
    """Build the Markdown review packet for one verified session."""
    raise NotImplementedError


def skeleton(result: VerifyResult) -> DecisionFile:
    """Create an empty decision file for manual review."""
    raise NotImplementedError


class ClaudeReviewer:
    """Propose review decisions using the configured command-line reviewer."""

    def __init__(self, model: str, timeout_s: int) -> None:
        self.model = model
        self.timeout_s = timeout_s

    def propose(self, packet: str, result: VerifyResult) -> DecisionFile:
        """Return proposed decisions for a review packet."""
        raise NotImplementedError


def apply_decisions(result: VerifyResult, decisions: DecisionFile) -> SessionApproval:
    """Apply complete review decisions to verified observations."""
    raise NotImplementedError
