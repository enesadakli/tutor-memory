from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tutormem.errors import PendingReviewError, ReviewerError, SchemaError, StaleArtifactError
from tutormem.models import (
    Decision,
    DecisionFile,
    EvidenceRef,
    Hypothesis,
    Instruction,
    Observation,
    ProfileState,
    RejectedObservation,
    Revocation,
    Session,
    Turn,
    VerifiedObservation,
    VerifiedQuote,
    VerifyResult,
    turns_sha256,
)
from tutormem.review import ClaudeReviewer, apply_decisions, skeleton, write_packet


def test_review_prompt_distinguishes_standing_instructions_from_one_off_requests() -> None:
    prompt = (Path(__file__).parents[1] / "prompts" / "review.md").read_text(encoding="utf-8")

    assert "standing preference" in prompt
    assert "one-off request" in prompt
    assert "slayttaki terimleri Türkçeleştirme" in prompt
    assert "bunu da detaylı anlat" in prompt


def _session(long: bool = False) -> Session:
    text = "a" * 240 + "alıntı" + "b" * 240 if long else "önce alıntı sonra"
    turns = (Turn("learner", text),)
    return Session("s1", turns_sha256(turns), "DB", None, 1, "s1.md", turns)


def _verified_result(*observations: Observation, long: bool = False) -> VerifyResult:
    session = _session(long)
    verified = []
    for item in observations:
        text = session.turns[0].text
        quote = item.quotes[0]
        start = text.index(quote)
        verified.append(
            VerifiedObservation(
                item,
                (VerifiedQuote(quote, 0, start, start + len(quote), quote, True),),
            )
        )
    return VerifyResult(session.session_id, session.content_sha256, tuple(verified), ())


def _observation(number: int, **changes: object) -> Observation:
    values: dict[str, object] = {
        "id": f"s1:{number}",
        "kind": "inference",
        "claim": f"Claim {number}",
        "quotes": ("alıntı",),
        "proposed_match": None,
        "concept": None,
    }
    values.update(changes)
    return Observation(**values)  # type: ignore[arg-type]


def _profile() -> ProfileState:
    evidence = EvidenceRef("old", "old:1", "quote")
    instruction = Instruction("ins-old:1", "Use steps.", "active", (evidence,), 1, None)
    hypothesis = Hypothesis(
        "hyp-old:1", "Likes diagrams.", "open", ("old",), (evidence,), 1, 1, None, None, None
    )
    return ProfileState(1, (instruction,), (hypothesis,), (), ())


def test_write_packet_lists_verified_rejected_matches_and_highlights_context() -> None:
    session = _session(long=True)
    verified_observation = _observation(1, proposed_match="hyp-old:1")
    rejected_observation = _observation(2)
    result = _verified_result(verified_observation, long=True)
    result = VerifyResult(
        result.session_id,
        result.content_sha256,
        result.verified,
        (RejectedObservation(rejected_observation, "not_found", "missing quote"),),
    )

    packet = write_packet(session, result, _profile())

    assert "s1:1" in packet
    assert "s1:2" in packet and "not_found" in packet and "missing quote" in packet
    assert "hyp-old:1" in packet and "Likes diagrams." in packet
    assert "a" * 200 + "**alıntı**" + "b" * 200 in packet
    assert "…" in packet
    assert "claim" in packet.lower() and "broader" in packet.lower()


def test_skeleton_is_empty_and_current() -> None:
    result = _verified_result(_observation(1))
    assert skeleton(result) == DecisionFile(result.session_id, result.content_sha256, (), ())


def _decision_payload(result: VerifyResult, *, sha: str | None = None) -> dict[str, object]:
    return {
        "session_id": result.session_id,
        "content_sha256": sha or result.content_sha256,
        "decisions": [
            {
                "observation_id": "s1:1",
                "action": "accept",
                "reviewer": "human",
                "reason": "ok",
            }
        ],
        "revocations": [{"target_id": "hyp-old:1", "reviewer": "human", "reason": "obsolete"}],
    }


def test_claude_reviewer_success_forces_reviewer_and_disables_tools() -> None:
    result = _verified_result(_observation(1))
    captured: dict[str, object] = {}

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, stdin=stdin, timeout=timeout)
        model_text = "Decision follows: " + json.dumps(_decision_payload(result))
        return subprocess.CompletedProcess(args, 0, json.dumps({"result": model_text}), "")

    decisions = ClaudeReviewer("sonnet", 12, runner=runner).propose("PACKET", result)

    assert captured["args"] == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        "sonnet",
        "--tools",
        "",
    ]
    assert "PACKET" in str(captured["stdin"])
    assert decisions.decisions[0].reviewer == "claude"
    assert decisions.revocations[0].reviewer == "claude"


def test_claude_reviewer_rejects_mismatched_sha() -> None:
    result = _verified_result(_observation(1))

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        text = json.dumps(_decision_payload(result, sha="f" * 64))
        return subprocess.CompletedProcess(args, 0, json.dumps({"result": text}), "")

    with pytest.raises(ReviewerError, match="different session"):
        ClaudeReviewer("m", 1, runner=runner).propose("packet", result)


@pytest.mark.parametrize(
    "stdout",
    [json.dumps({"is_error": True, "result": "failure"}), "not json"],
)
def test_claude_reviewer_rejects_error_and_invalid_json(stdout: str) -> None:
    result = _verified_result(_observation(1))

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout, "")

    with pytest.raises(ReviewerError):
        ClaudeReviewer("m", 1, runner=runner).propose("packet", result)


def test_claude_reviewer_converts_timeout_and_nonzero_exit() -> None:
    result = _verified_result(_observation(1))

    def timeout(args: list[str], stdin: str, seconds: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, seconds)

    with pytest.raises(ReviewerError, match="timed out"):
        ClaudeReviewer("m", 1, runner=timeout).propose("packet", result)

    def failed(args: list[str], stdin: str, seconds: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 3, "", "failed")

    with pytest.raises(ReviewerError, match="status 3"):
        ClaudeReviewer("m", 1, runner=failed).propose("packet", result)


@pytest.mark.parametrize("field", ["session_id", "content_sha256"])
def test_apply_decisions_rejects_stale_identity(field: str) -> None:
    result = _verified_result(_observation(1))
    values = {
        "session_id": result.session_id,
        "content_sha256": result.content_sha256,
        "decisions": (Decision("s1:1", "accept", "human"),),
    }
    values[field] = "other" if field == "session_id" else "f" * 64
    with pytest.raises(StaleArtifactError):
        apply_decisions(result, DecisionFile(**values))  # type: ignore[arg-type]


def test_apply_decisions_requires_every_verified_observation() -> None:
    result = _verified_result(_observation(1), _observation(2))
    decisions = DecisionFile(
        result.session_id,
        result.content_sha256,
        (Decision("s1:1", "accept", "human"),),
    )
    with pytest.raises(PendingReviewError, match="s1:2"):
        apply_decisions(result, decisions)


@pytest.mark.parametrize("observation_id", ["unknown:1", "s1:9"])
def test_apply_decisions_rejects_unknown_or_verify_rejected_id(observation_id: str) -> None:
    result = _verified_result(_observation(1))
    rejected = RejectedObservation(_observation(9), "not_found", "missing")
    result = VerifyResult(result.session_id, result.content_sha256, result.verified, (rejected,))
    decisions = DecisionFile(
        result.session_id,
        result.content_sha256,
        (Decision(observation_id, "accept", "human"),),
    )
    with pytest.raises(SchemaError, match="unverified"):
        apply_decisions(result, decisions)


def test_apply_decisions_rejects_duplicates() -> None:
    result = _verified_result(_observation(1))
    decision = Decision("s1:1", "accept", "human")
    with pytest.raises(SchemaError, match="duplicate"):
        apply_decisions(
            result,
            DecisionFile(result.session_id, result.content_sha256, (decision, decision)),
        )


def test_apply_decisions_applies_overrides_force_new_rejection_and_result_order() -> None:
    observations = (
        _observation(1, proposed_match="hyp-old:1"),
        _observation(2, proposed_match="hyp-old:1"),
        _observation(3),
        _observation(4),
    )
    result = _verified_result(*observations)
    revocation = Revocation("hyp-old:1", "human", "obsolete")
    decisions = DecisionFile(
        result.session_id,
        result.content_sha256,
        (
            Decision("s1:4", "accept", "human", match="hyp-new:1"),
            Decision("s1:2", "accept", "human", force_new=True),
            Decision("s1:3", "reject", "human"),
            Decision(
                "s1:1",
                "accept",
                "human",
                claim="Narrow claim",
                kind="explicit_instruction",
            ),
        ),
        (revocation,),
    )

    approval = apply_decisions(result, decisions)

    assert [item.observation_id for item in approval.evidence] == ["s1:1", "s1:2", "s1:4"]
    assert approval.evidence[0].claim == "Narrow claim"
    assert approval.evidence[0].kind == "explicit_instruction"
    assert approval.evidence[0].match == "hyp-old:1"
    assert approval.evidence[1].force_new is True and approval.evidence[1].match is None
    assert approval.evidence[2].match == "hyp-new:1"
    assert approval.revocations == (revocation,)
