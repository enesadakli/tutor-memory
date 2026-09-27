from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .config import Config
from .errors import ReplayError
from .models import (
    SCHEMA_VERSION,
    ApprovedEvidence,
    EvidenceRef,
    Hypothesis,
    HypothesisStatus,
    Instruction,
    InstructionStatus,
    ProfileState,
    Revocation,
    Session,
    SessionApproval,
    SessionRecord,
    StuckPoint,
)


@dataclass(slots=True)
class _InstructionRecord:
    id: str
    claim: str
    first_seen: int
    status: InstructionStatus = "active"
    evidence: list[EvidenceRef] = field(default_factory=list)
    revoked_at: int | None = None

    def freeze(self) -> Instruction:
        return Instruction(
            id=self.id,
            claim=self.claim,
            status=self.status,
            evidence=tuple(self.evidence),
            first_seen=self.first_seen,
            revoked_at=self.revoked_at,
        )


@dataclass(slots=True)
class _HypothesisRecord:
    id: str
    claim: str
    first_seen: int
    last_seen: int
    status: HypothesisStatus = "open"
    sessions: list[str] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    promoted_at: int | None = None
    dropped_at: int | None = None
    revoked_at: int | None = None

    def freeze(self) -> Hypothesis:
        return Hypothesis(
            id=self.id,
            claim=self.claim,
            status=self.status,
            sessions=tuple(self.sessions),
            evidence=tuple(self.evidence),
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            promoted_at=self.promoted_at,
            dropped_at=self.dropped_at,
            revoked_at=self.revoked_at,
        )


def _refs(session_id: str, evidence: ApprovedEvidence) -> list[EvidenceRef]:
    return [
        EvidenceRef(session_id, evidence.observation_id, quote.matched_text)
        for quote in evidence.quotes
    ]


def _replay_error(session_id: str, offending_id: str, detail: str) -> ReplayError:
    return ReplayError(f"session {session_id}: {detail}: {offending_id}")


def _apply_instruction(
    session_id: str,
    ordinal: int,
    evidence: ApprovedEvidence,
    instructions: list[_InstructionRecord],
    by_id: dict[str, _InstructionRecord],
) -> None:
    target: _InstructionRecord | None = None
    if evidence.match is not None and not evidence.force_new:
        if not evidence.match.startswith("ins-"):
            raise _replay_error(
                session_id, evidence.match, f"invalid match for {evidence.observation_id}"
            )
        target = by_id.get(evidence.match)
        if target is None or target.status != "active":
            raise _replay_error(
                session_id,
                evidence.match,
                f"inactive or unknown match for {evidence.observation_id}",
            )
    elif not evidence.force_new:
        target = next(
            (
                item
                for item in instructions
                if item.status == "active" and item.claim == evidence.claim
            ),
            None,
        )

    if target is None:
        target = _InstructionRecord(
            id=f"ins-{evidence.observation_id}", claim=evidence.claim, first_seen=ordinal
        )
        instructions.append(target)
        by_id[target.id] = target
    target.evidence.extend(_refs(session_id, evidence))


def _apply_inference(
    session_id: str,
    ordinal: int,
    evidence: ApprovedEvidence,
    threshold: int,
    hypotheses: list[_HypothesisRecord],
    by_id: dict[str, _HypothesisRecord],
) -> None:
    target: _HypothesisRecord | None = None
    if evidence.match is not None and not evidence.force_new:
        if not evidence.match.startswith("hyp-"):
            raise _replay_error(
                session_id, evidence.match, f"invalid match for {evidence.observation_id}"
            )
        target = by_id.get(evidence.match)
        if target is None or target.status not in ("open", "promoted"):
            raise _replay_error(
                session_id,
                evidence.match,
                f"inactive or unknown match for {evidence.observation_id}",
            )

    if target is None:
        target = _HypothesisRecord(
            id=f"hyp-{evidence.observation_id}",
            claim=evidence.claim,
            first_seen=ordinal,
            last_seen=ordinal,
        )
        hypotheses.append(target)
        by_id[target.id] = target

    if session_id not in target.sessions:
        target.sessions.append(session_id)
    target.evidence.extend(_refs(session_id, evidence))
    target.last_seen = ordinal
    if target.status == "open" and len(target.sessions) >= threshold:
        target.status = "promoted"
        target.promoted_at = ordinal


def _apply_evidence(
    session: Session,
    ordinal: int,
    evidence: ApprovedEvidence,
    config: Config,
    instructions: list[_InstructionRecord],
    instruction_by_id: dict[str, _InstructionRecord],
    hypotheses: list[_HypothesisRecord],
    hypothesis_by_id: dict[str, _HypothesisRecord],
    stuck_points: list[StuckPoint],
) -> None:
    if evidence.kind == "explicit_instruction":
        _apply_instruction(
            session.session_id,
            ordinal,
            evidence,
            instructions,
            instruction_by_id,
        )
    elif evidence.kind == "inference":
        _apply_inference(
            session.session_id,
            ordinal,
            evidence,
            config.threshold,
            hypotheses,
            hypothesis_by_id,
        )
    else:
        if evidence.match is not None:
            raise _replay_error(
                session.session_id,
                evidence.match,
                f"stuck point {evidence.observation_id} cannot have a match",
            )
        stuck_points.append(
            StuckPoint(
                session_id=session.session_id,
                course=session.course,
                concept=evidence.concept,
                claim=evidence.claim,
                quote=evidence.quotes[0].matched_text,
            )
        )


def _apply_revocation(
    session_id: str,
    ordinal: int,
    revocation: Revocation,
    instruction_by_id: dict[str, _InstructionRecord],
    hypothesis_by_id: dict[str, _HypothesisRecord],
) -> None:
    instruction = instruction_by_id.get(revocation.target_id)
    if instruction is not None:
        if instruction.status == "revoked":
            raise _replay_error(session_id, revocation.target_id, "invalid revocation target")
        instruction.status = "revoked"
        instruction.revoked_at = ordinal
        return

    hypothesis = hypothesis_by_id.get(revocation.target_id)
    if hypothesis is None or hypothesis.status in ("dropped", "revoked"):
        raise _replay_error(session_id, revocation.target_id, "invalid revocation target")
    hypothesis.status = "revoked"
    hypothesis.revoked_at = ordinal


def _validate_approvals(
    sessions: Sequence[Session], approvals: Mapping[str, SessionApproval | None]
) -> None:
    session_ids = {session.session_id for session in sessions}
    for key, approval in approvals.items():
        if key not in session_ids:
            raise _replay_error(key, key, "approval for unknown session")
        if approval is not None and approval.session_id != key:
            raise _replay_error(key, approval.session_id, "approval session id mismatch")


def replay(
    sessions: Sequence[Session],
    approvals: Mapping[str, SessionApproval | None],
    config: Config,
) -> ProfileState:
    """Recompute profile state from canonical sessions and approvals."""
    ordered_sessions = sorted(sessions, key=lambda session: session.index)
    _validate_approvals(ordered_sessions, approvals)

    instructions: list[_InstructionRecord] = []
    instruction_by_id: dict[str, _InstructionRecord] = {}
    hypotheses: list[_HypothesisRecord] = []
    hypothesis_by_id: dict[str, _HypothesisRecord] = {}
    stuck_points: list[StuckPoint] = []
    session_records: list[SessionRecord] = []
    ordinal = 0

    for session in ordered_sessions:
        approval = approvals.get(session.session_id)
        if approval is None:
            session_records.append(
                SessionRecord(
                    session_id=session.session_id,
                    index=session.index,
                    course=session.course,
                    date=session.date,
                    content_sha256=session.content_sha256,
                    status="pending",
                    ordinal=None,
                )
            )
            continue

        ordinal += 1
        session_records.append(
            SessionRecord(
                session_id=session.session_id,
                index=session.index,
                course=session.course,
                date=session.date,
                content_sha256=session.content_sha256,
                status="applied",
                ordinal=ordinal,
            )
        )
        for evidence in approval.evidence:
            _apply_evidence(
                session,
                ordinal,
                evidence,
                config,
                instructions,
                instruction_by_id,
                hypotheses,
                hypothesis_by_id,
                stuck_points,
            )
        for revocation in approval.revocations:
            _apply_revocation(
                session.session_id,
                ordinal,
                revocation,
                instruction_by_id,
                hypothesis_by_id,
            )
        for hypothesis in hypotheses:
            if (
                hypothesis.status == "open"
                and hypothesis.last_seen < ordinal
                and ordinal - hypothesis.last_seen >= config.stale_after
            ):
                hypothesis.status = "dropped"
                hypothesis.dropped_at = ordinal

    return ProfileState(
        schema_version=SCHEMA_VERSION,
        instructions=tuple(item.freeze() for item in instructions),
        hypotheses=tuple(item.freeze() for item in hypotheses),
        stuck_points=tuple(stuck_points),
        sessions=tuple(session_records),
    )
