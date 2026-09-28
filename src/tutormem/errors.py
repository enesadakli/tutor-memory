class TutormemError(Exception):
    """Base class for expected tutor-memory failures."""


class SchemaError(TutormemError):
    """Raised when structured data does not match the contract."""


class ParseError(TutormemError):
    """Raised when an input transcript cannot be parsed."""


class DuplicateContentError(TutormemError):
    """Raised when transcript content has already been ingested."""


class SessionExistsError(TutormemError):
    """Raised when a session id already exists without replacement enabled."""


class SessionNotFoundError(TutormemError):
    """Raised when a requested session does not exist."""


class StaleArtifactError(TutormemError):
    """Raised when an artifact no longer matches its session."""


class PendingReviewError(TutormemError):
    """Raised when verified observations do not all have review decisions."""


class ReplayError(TutormemError):
    """Raised when approved history cannot be replayed consistently."""


class ExtractorError(TutormemError):
    """Raised when an extractor fails."""


class ReviewerError(TutormemError):
    """Raised when a reviewer fails or returns invalid output."""


class SyncError(TutormemError):
    """Raised when document synchronization fails."""


class AutomaticModeError(TutormemError):
    """Raised when automatic orchestration cannot complete."""


class CaptureHostError(TutormemError):
    """Raised when the native capture host cannot be installed or configured."""
