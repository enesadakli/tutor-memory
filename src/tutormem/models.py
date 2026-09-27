from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import types
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Literal, Self, Union, get_args, get_origin, get_type_hints

from .errors import SchemaError

SCHEMA_VERSION = 1

Speaker = Literal["learner", "tutor"]
ObservationKind = Literal["explicit_instruction", "inference", "stuck_point"]
RejectReason = Literal["not_found", "tutor_turn", "empty_quote", "unattributed_transcript"]
DecisionAction = Literal["accept", "reject"]
Reviewer = Literal["claude", "human"]
InstructionStatus = Literal["active", "revoked"]
HypothesisStatus = Literal["open", "promoted", "dropped", "revoked"]
SessionStatus = Literal["applied", "pending"]

_SESSION_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _serialize(value: Any) -> Any:
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    return value


def _parse_value(value: Any, annotation: Any, path: str) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        if value not in args or type(value) is not type(args[0]):
            raise SchemaError(f"{path}: expected one of {args!r}")
        return value
    if origin in (types.UnionType, Union):
        if type(None) in args and value is None:
            return None
        non_none = tuple(arg for arg in args if arg is not type(None))
        for candidate in non_none:
            try:
                return _parse_value(value, candidate, path)
            except SchemaError:
                pass
        raise SchemaError(f"{path}: wrong type")
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise SchemaError(f"{path}: expected array")
        item_type = args[0]
        return tuple(
            _parse_value(item, item_type, f"{path}[{index}]") for index, item in enumerate(value)
        )
    if isinstance(annotation, type) and issubclass(annotation, Model):
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected object")
        return annotation.from_dict(value)
    if annotation is Any:
        return value
    if annotation is int:
        if type(value) is not int:
            raise SchemaError(f"{path}: expected int")
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise SchemaError(f"{path}: expected bool")
        return value
    if annotation is str:
        if type(value) is not str:
            raise SchemaError(f"{path}: expected str")
        return value
    if value is None and annotation is type(None):
        return None
    raise SchemaError(f"{path}: unsupported or wrong type")


class Model:
    """Strict JSON-compatible serialization shared by all contract models."""

    _type_hints: ClassVar[dict[type[Model], dict[str, Any]]] = {}

    def to_dict(self) -> dict[str, Any]:
        return {field.name: _serialize(getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_dict(cls, d: Any) -> Self:
        if not isinstance(d, dict):
            raise SchemaError(f"{cls.__name__}: expected object")
        model_fields = {field.name: field for field in fields(cls)}
        unknown = set(d) - set(model_fields)
        if unknown:
            raise SchemaError(f"{cls.__name__}: unknown keys: {', '.join(sorted(unknown))}")
        missing = [
            name
            for name, field in model_fields.items()
            if name not in d
            and field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ]
        if missing:
            raise SchemaError(f"{cls.__name__}: missing keys: {', '.join(missing)}")
        hints = Model._type_hints.setdefault(cls, get_type_hints(cls))
        values = {
            name: _parse_value(d[name], hints[name], f"{cls.__name__}.{name}")
            for name in model_fields
            if name in d
        }
        session_id = values.get("session_id")
        if session_id is not None and not _SESSION_ID_RE.fullmatch(session_id):
            raise SchemaError(f"{cls.__name__}.session_id: invalid session id")
        content_sha256 = values.get("content_sha256")
        if content_sha256 is not None and not _SHA256_RE.fullmatch(content_sha256):
            raise SchemaError(f"{cls.__name__}.content_sha256: invalid SHA-256")
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"{cls.__name__}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Turn(Model):
    speaker: Speaker
    text: str


@dataclass(frozen=True, slots=True)
class Session(Model):
    session_id: str
    content_sha256: str
    course: str
    date: str | None
    index: int
    source: str
    turns: tuple[Turn, ...]

    def __post_init__(self) -> None:
        if not _SESSION_ID_RE.fullmatch(self.session_id):
            raise ValueError("session_id must match [a-z0-9][a-z0-9-]*")
        if not _SHA256_RE.fullmatch(self.content_sha256):
            raise ValueError("content_sha256 must be a lowercase hexadecimal SHA-256")
        if self.date is not None and not _DATE_RE.fullmatch(self.date):
            raise ValueError("date must use YYYY-MM-DD")
        if self.index < 1:
            raise ValueError("index must be at least 1")
        if not self.source or self.source != self.source.rsplit("/", 1)[-1] or "\\" in self.source:
            raise ValueError("source must be a file name only")


@dataclass(frozen=True, slots=True)
class Observation(Model):
    id: str
    kind: ObservationKind
    claim: str
    quotes: tuple[str, ...]
    proposed_match: str | None = None
    concept: str | None = None

    def __post_init__(self) -> None:
        if not self.quotes:
            raise ValueError("quotes must contain at least one element")


@dataclass(frozen=True, slots=True)
class DroppedRecord(Model):
    raw: str
    error: str


@dataclass(frozen=True, slots=True)
class ExtractResult(Model):
    session_id: str
    content_sha256: str
    extractor: str
    model: str | None
    observations: tuple[Observation, ...]
    dropped: tuple[DroppedRecord, ...]


@dataclass(frozen=True, slots=True)
class VerifiedQuote(Model):
    quote: str
    turn_index: int
    start: int
    end: int
    matched_text: str
    exact: bool


@dataclass(frozen=True, slots=True)
class VerifiedObservation(Model):
    observation: Observation
    quotes: tuple[VerifiedQuote, ...]


@dataclass(frozen=True, slots=True)
class RejectedObservation(Model):
    observation: Observation
    reason: RejectReason
    detail: str


@dataclass(frozen=True, slots=True)
class VerifyResult(Model):
    session_id: str
    content_sha256: str
    verified: tuple[VerifiedObservation, ...]
    rejected: tuple[RejectedObservation, ...]


@dataclass(frozen=True, slots=True)
class Decision(Model):
    observation_id: str
    action: DecisionAction
    reviewer: Reviewer
    reason: str = ""
    claim: str | None = None
    kind: ObservationKind | None = None
    match: str | None = None
    force_new: bool = False

    def __post_init__(self) -> None:
        if self.force_new and self.match is not None:
            raise ValueError("force_new and match cannot be used together")


@dataclass(frozen=True, slots=True)
class Revocation(Model):
    target_id: str
    reviewer: Reviewer
    reason: str


@dataclass(frozen=True, slots=True)
class DecisionFile(Model):
    session_id: str
    content_sha256: str
    decisions: tuple[Decision, ...]
    revocations: tuple[Revocation, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovedEvidence(Model):
    session_id: str
    observation_id: str
    kind: ObservationKind
    claim: str
    quotes: tuple[VerifiedQuote, ...]
    match: str | None
    force_new: bool
    concept: str | None


@dataclass(frozen=True, slots=True)
class SessionApproval(Model):
    session_id: str
    evidence: tuple[ApprovedEvidence, ...]
    revocations: tuple[Revocation, ...]


@dataclass(frozen=True, slots=True)
class EvidenceRef(Model):
    session_id: str
    observation_id: str
    quote: str


@dataclass(frozen=True, slots=True)
class Instruction(Model):
    id: str
    claim: str
    status: InstructionStatus
    evidence: tuple[EvidenceRef, ...]
    first_seen: int
    revoked_at: int | None


@dataclass(frozen=True, slots=True)
class Hypothesis(Model):
    id: str
    claim: str
    status: HypothesisStatus
    sessions: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    first_seen: int
    last_seen: int
    promoted_at: int | None
    dropped_at: int | None
    revoked_at: int | None


@dataclass(frozen=True, slots=True)
class StuckPoint(Model):
    session_id: str
    course: str
    concept: str | None
    claim: str
    quote: str


@dataclass(frozen=True, slots=True)
class SessionRecord(Model):
    session_id: str
    index: int
    course: str
    date: str | None
    content_sha256: str
    status: SessionStatus
    ordinal: int | None


@dataclass(frozen=True, slots=True)
class ProfileState(Model):
    schema_version: int
    instructions: tuple[Instruction, ...]
    hypotheses: tuple[Hypothesis, ...]
    stuck_points: tuple[StuckPoint, ...]
    sessions: tuple[SessionRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION}")


def turns_sha256(turns: Sequence[Turn]) -> str:
    """Return the canonical SHA-256 digest for an ordered turn sequence."""
    payload = json.dumps(
        [[turn.speaker, turn.text] for turn in turns], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def observation_list_schema() -> dict[str, Any]:
    """Return the extractor output schema accepted by the observation parser."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["explicit_instruction", "inference", "stuck_point"],
                        },
                        "claim": {"type": "string"},
                        "quotes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "proposed_match": {"type": ["string", "null"]},
                        "concept": {"type": ["string", "null"]},
                    },
                    "required": ["kind", "claim", "quotes"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["observations"],
        "additionalProperties": False,
    }
