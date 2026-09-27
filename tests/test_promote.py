from __future__ import annotations

import copy

import pytest

from tutormem.config import Config, RulesConfig
from tutormem.errors import ReplayError
from tutormem.models import (
    ApprovedEvidence,
    Revocation,
    Session,
    SessionApproval,
    Turn,
    VerifiedQuote,
    turns_sha256,
)
from tutormem.promote import replay


def _session(sid: str, index: int, course: str = "Ders") -> Session:
    turns = (Turn("learner", f"{sid} metni"),)
    return Session(sid, turns_sha256(turns), course, None, index, f"{sid}.md", turns)


def _quote(text: str) -> VerifiedQuote:
    return VerifiedQuote(text, 0, 0, len(text), text, True)


def _evidence(
    sid: str,
    number: int,
    kind: str = "inference",
    claim: str = "Örnek iddia",
    *,
    match: str | None = None,
    force_new: bool = False,
    quotes: tuple[str, ...] = ("kanıt",),
    concept: str | None = None,
) -> ApprovedEvidence:
    return ApprovedEvidence(
        session_id=sid,
        observation_id=f"{sid}:{number}",
        kind=kind,  # type: ignore[arg-type]
        claim=claim,
        quotes=tuple(_quote(text) for text in quotes),
        match=match,
        force_new=force_new,
        concept=concept,
    )


def _approval(
    sid: str,
    evidence: tuple[ApprovedEvidence, ...] = (),
    revocations: tuple[Revocation, ...] = (),
) -> SessionApproval:
    return SessionApproval(sid, evidence, revocations)


def _config(*, threshold: int = 3, stale_after: int = 5) -> Config:
    return Config(rules=RulesConfig(threshold=threshold, stale_after=stale_after))


def _empty_history(count: int) -> tuple[list[Session], dict[str, SessionApproval | None]]:
    sessions = [_session(f"s{index}", index) for index in range(1, count + 1)]
    approvals = {session.session_id: _approval(session.session_id) for session in sessions}
    return sessions, approvals


def test_promotes_on_third_distinct_session_not_third_quote() -> None:
    sessions, approvals = _empty_history(3)
    approvals["s1"] = _approval(
        "s1",
        (_evidence("s1", 1, quotes=("ilk", "ikinci")),),
    )
    approvals["s2"] = _approval("s2", (_evidence("s2", 1, match="hyp-s1:1", quotes=("üçüncü",)),))
    approvals["s3"] = _approval("s3", (_evidence("s3", 1, match="hyp-s1:1"),))

    before_threshold = replay(sessions[:2], {key: approvals[key] for key in ("s1", "s2")}, Config())
    assert before_threshold.hypotheses[0].status == "open"
    assert len(before_threshold.hypotheses[0].evidence) == 3

    state = replay(sessions, approvals, Config())
    hypothesis = state.hypotheses[0]
    assert hypothesis.status == "promoted"
    assert hypothesis.sessions == ("s1", "s2", "s3")
    assert [item.quote for item in hypothesis.evidence] == ["ilk", "ikinci", "üçüncü", "kanıt"]
    assert hypothesis.promoted_at == 3


def test_match_on_promoted_hypothesis_keeps_claim_and_status() -> None:
    sessions, approvals = _empty_history(4)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1, claim="Sabit iddia"),))
    for index in range(2, 5):
        approvals[f"s{index}"] = _approval(
            f"s{index}",
            (
                _evidence(
                    f"s{index}",
                    1,
                    claim="Daha sonra gelen farklı metin",
                    match="hyp-s1:1",
                ),
            ),
        )

    hypothesis = replay(sessions, approvals, Config()).hypotheses[0]
    assert hypothesis.status == "promoted"
    assert hypothesis.claim == "Sabit iddia"
    assert hypothesis.promoted_at == 3
    assert hypothesis.last_seen == 4
    assert len(hypothesis.evidence) == 4


def test_default_staleness_boundary() -> None:
    sessions, approvals = _empty_history(7)
    approvals["s2"] = _approval("s2", (_evidence("s2", 1),))

    at_six = replay(sessions[:6], {f"s{i}": approvals[f"s{i}"] for i in range(1, 7)}, Config())
    assert at_six.hypotheses[0].status == "open"
    assert at_six.hypotheses[0].last_seen == 2

    at_seven = replay(sessions, approvals, Config())
    assert at_seven.hypotheses[0].status == "dropped"
    assert at_seven.hypotheses[0].dropped_at == 7


def test_stale_after_one_drops_at_next_applied_session() -> None:
    sessions, approvals = _empty_history(2)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1),))

    hypothesis = replay(sessions, approvals, _config(stale_after=1)).hypotheses[0]
    assert hypothesis.status == "dropped"
    assert hypothesis.dropped_at == 2


def test_pending_sessions_do_not_advance_ordinals_or_age_hypotheses() -> None:
    sessions = [_session("s3", 3), _session("s1", 1), _session("s2", 2), _session("s4", 4)]
    approvals = {
        "s1": _approval("s1", (_evidence("s1", 1),)),
        "s2": None,
        "s3": None,
        "s4": _approval("s4"),
    }

    state = replay(sessions, approvals, _config(stale_after=2))
    assert [(item.session_id, item.status, item.ordinal) for item in state.sessions] == [
        ("s1", "applied", 1),
        ("s2", "pending", None),
        ("s3", "pending", None),
        ("s4", "applied", 2),
    ]
    assert state.hypotheses[0].status == "open"
    assert state.hypotheses[0].last_seen == 1


def test_promoted_hypothesis_never_goes_stale() -> None:
    sessions, approvals = _empty_history(12)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1),))
    approvals["s2"] = _approval("s2", (_evidence("s2", 1, match="hyp-s1:1"),))
    approvals["s3"] = _approval("s3", (_evidence("s3", 1, match="hyp-s1:1"),))

    hypothesis = replay(sessions, approvals, Config()).hypotheses[0]
    assert hypothesis.status == "promoted"
    assert hypothesis.promoted_at == 3
    assert hypothesis.dropped_at is None


def test_revokes_instruction_and_promoted_hypothesis() -> None:
    sessions, approvals = _empty_history(4)
    approvals["s1"] = _approval(
        "s1",
        (
            _evidence("s1", 1, "explicit_instruction", "Adım adım git"),
            _evidence("s1", 2),
        ),
    )
    approvals["s2"] = _approval("s2", (_evidence("s2", 1, match="hyp-s1:2"),))
    approvals["s3"] = _approval("s3", (_evidence("s3", 1, match="hyp-s1:2"),))
    approvals["s4"] = _approval(
        "s4",
        revocations=(
            Revocation("ins-s1:1", "human", "geri çekildi"),
            Revocation("hyp-s1:2", "human", "geri çekildi"),
        ),
    )

    state = replay(sessions, approvals, Config())
    assert (state.instructions[0].status, state.instructions[0].revoked_at) == ("revoked", 4)
    assert (state.hypotheses[0].status, state.hypotheses[0].revoked_at) == ("revoked", 4)


@pytest.mark.parametrize("target", ["missing-id", "ins-missing:1", "hyp-missing:1"])
def test_revoking_unknown_item_is_an_error(target: str) -> None:
    session = _session("s1", 1)
    approval = _approval("s1", revocations=(Revocation(target, "human", "geçersiz"),))

    with pytest.raises(ReplayError) as caught:
        replay((session,), {"s1": approval}, Config())
    assert "s1" in str(caught.value)
    assert target in str(caught.value)


def test_revoking_dropped_item_is_an_error() -> None:
    sessions, approvals = _empty_history(3)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1),))
    approvals["s3"] = _approval("s3", revocations=(Revocation("hyp-s1:1", "human", "çok geç"),))

    with pytest.raises(ReplayError, match="hyp-s1:1") as caught:
        replay(sessions, approvals, _config(stale_after=1))
    assert "s3" in str(caught.value)


def test_revoking_already_revoked_item_is_an_error() -> None:
    sessions, approvals = _empty_history(3)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1, "explicit_instruction"),))
    approvals["s2"] = _approval("s2", revocations=(Revocation("ins-s1:1", "human", "ilk"),))
    approvals["s3"] = _approval("s3", revocations=(Revocation("ins-s1:1", "human", "ikinci"),))

    with pytest.raises(ReplayError, match="ins-s1:1") as caught:
        replay(sessions, approvals, Config())
    assert "s3" in str(caught.value)


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("inference", "hyp-unknown:1"),
        ("inference", "ins-unknown:1"),
        ("explicit_instruction", "ins-unknown:1"),
        ("explicit_instruction", "hyp-unknown:1"),
        ("stuck_point", "hyp-unknown:1"),
    ],
)
def test_invalid_match_kind_or_unknown_target_is_an_error(kind: str, target: str) -> None:
    session = _session("s1", 1)
    evidence = _evidence("s1", 1, kind, match=target)

    with pytest.raises(ReplayError) as caught:
        replay((session,), {"s1": _approval("s1", (evidence,))}, Config())
    message = str(caught.value)
    assert "s1" in message
    assert target in message


@pytest.mark.parametrize("final_status", ["dropped", "revoked"])
def test_matching_inactive_hypothesis_is_an_error(final_status: str) -> None:
    sessions, approvals = _empty_history(3)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1),))
    if final_status == "revoked":
        approvals["s2"] = _approval("s2", revocations=(Revocation("hyp-s1:1", "human", "bitti"),))
        config = Config()
    else:
        config = _config(stale_after=1)
    approvals["s3"] = _approval("s3", (_evidence("s3", 1, match="hyp-s1:1"),))

    with pytest.raises(ReplayError, match="hyp-s1:1") as caught:
        replay(sessions, approvals, config)
    assert "s3" in str(caught.value)


def test_matching_revoked_instruction_is_an_error() -> None:
    sessions, approvals = _empty_history(3)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1, "explicit_instruction"),))
    approvals["s2"] = _approval("s2", revocations=(Revocation("ins-s1:1", "human", "bitti"),))
    approvals["s3"] = _approval(
        "s3", (_evidence("s3", 1, "explicit_instruction", match="ins-s1:1"),)
    )

    with pytest.raises(ReplayError, match="ins-s1:1") as caught:
        replay(sessions, approvals, Config())
    assert "s3" in str(caught.value)


def test_instruction_claim_merge_force_new_and_revoked_not_a_target() -> None:
    sessions, approvals = _empty_history(4)
    claim = "Önce örnek ver"
    approvals["s1"] = _approval("s1", (_evidence("s1", 1, "explicit_instruction", claim),))
    approvals["s2"] = _approval("s2", (_evidence("s2", 1, "explicit_instruction", claim),))
    approvals["s3"] = _approval(
        "s3",
        (
            _evidence(
                "s3",
                1,
                "explicit_instruction",
                claim,
                match="ins-s1:1",
                force_new=True,
            ),
        ),
        (Revocation("ins-s1:1", "human", "yenisi kullanılacak"),),
    )
    approvals["s4"] = _approval("s4", (_evidence("s4", 1, "explicit_instruction", claim),))

    state = replay(sessions, approvals, Config())
    assert [item.id for item in state.instructions] == ["ins-s1:1", "ins-s3:1"]
    assert state.instructions[0].status == "revoked"
    assert [ref.session_id for ref in state.instructions[0].evidence] == ["s1", "s2"]
    assert [ref.session_id for ref in state.instructions[1].evidence] == ["s3", "s4"]


def test_custom_threshold_promotes_on_second_distinct_session() -> None:
    sessions, approvals = _empty_history(2)
    approvals["s1"] = _approval("s1", (_evidence("s1", 1),))
    approvals["s2"] = _approval("s2", (_evidence("s2", 1, match="hyp-s1:1"),))

    hypothesis = replay(sessions, approvals, _config(threshold=2)).hypotheses[0]
    assert hypothesis.status == "promoted"
    assert hypothesis.promoted_at == 2


def test_replay_is_deterministic_and_does_not_mutate_inputs() -> None:
    sessions = [_session("s2", 2), _session("s1", 1)]
    approvals = {
        "s1": _approval("s1", (_evidence("s1", 1, quotes=("bir", "iki")),)),
        "s2": _approval("s2", (_evidence("s2", 1, match="hyp-s1:1"),)),
    }
    sessions_before = copy.deepcopy(sessions)
    approvals_before = copy.deepcopy(approvals)

    first = replay(sessions, approvals, Config())
    second = replay(sessions, approvals, Config())

    assert first == second
    assert sessions == sessions_before
    assert approvals == approvals_before


def test_approval_for_unknown_session_is_an_error() -> None:
    session = _session("known", 1)
    approvals = {"known": None, "missing": _approval("missing")}

    with pytest.raises(ReplayError) as caught:
        replay((session,), approvals, Config())
    assert "missing" in str(caught.value)


def test_approval_session_id_mismatch_is_an_error() -> None:
    sessions = (_session("s1", 1), _session("s2", 2))

    with pytest.raises(ReplayError) as caught:
        replay(sessions, {"s1": _approval("s2")}, Config())
    assert "s1" in str(caught.value)
    assert "s2" in str(caught.value)


def test_compact_golden_scenario_ordinals_and_evidence_rule() -> None:
    sessions = [
        _session(f"s{index:02}", index, "Veritabanları" if index < 5 else "Derin Öğrenme")
        for index in range(1, 10)
    ]
    approvals: dict[str, SessionApproval | None] = {
        "s01": _approval(
            "s01",
            (
                _evidence(
                    "s01", 1, "explicit_instruction", "Sayfa sayfa ilerle", quotes=("sayfa",)
                ),
                _evidence("s01", 2, claim="Görsel destek ister"),
            ),
        ),
        "s02": _approval(
            "s02",
            (
                _evidence("s02", 1, claim="Bölümü sorularla kapatır"),
                _evidence("s02", 2, match="hyp-s01:2"),
            ),
        ),
        "s03": _approval(
            "s03",
            (_evidence("s03", 1, match="hyp-s01:2", quotes=("şema", "tablo")),),
        ),
        "s04": None,
        "s05": _approval(
            "s05",
            (
                _evidence(
                    "s05",
                    1,
                    "stuck_point",
                    "Batch normalization zor geldi",
                    quotes=("çok karmaşık", "ikinci kanıt"),
                    concept="Batch Normalization",
                ),
                _evidence("s05", 2, "explicit_instruction", "Kısa iddia", match="ins-s01:1"),
            ),
        ),
        "s06": _approval(
            "s06", (_evidence("s06", 1, "explicit_instruction", "Sektör durumunu söyle"),)
        ),
        "s07": _approval("s07", (_evidence("s07", 1, claim="Kendi sözüyle tekrarlar"),)),
        "s08": _approval("s08", revocations=(Revocation("ins-s06:1", "human", "geri çekildi"),)),
        "s09": _approval(
            "s09", (_evidence("s09", 1, "explicit_instruction", "Mülakat sorusu sor"),)
        ),
    }

    state = replay(tuple(reversed(sessions)), approvals, Config())

    assert [(item.session_id, item.ordinal) for item in state.sessions] == [
        ("s01", 1),
        ("s02", 2),
        ("s03", 3),
        ("s04", None),
        ("s05", 4),
        ("s06", 5),
        ("s07", 6),
        ("s08", 7),
        ("s09", 8),
    ]
    assert [item.id for item in state.instructions] == [
        "ins-s01:1",
        "ins-s06:1",
        "ins-s09:1",
    ]
    assert state.instructions[0].claim == "Sayfa sayfa ilerle"
    by_id = {item.id: item for item in state.hypotheses}
    assert (by_id["hyp-s01:2"].status, by_id["hyp-s01:2"].promoted_at) == (
        "promoted",
        3,
    )
    assert (by_id["hyp-s02:1"].status, by_id["hyp-s02:1"].dropped_at) == ("dropped", 7)
    assert state.instructions[1].revoked_at == 7
    assert state.stuck_points[0].course == "Derin Öğrenme"
    assert state.stuck_points[0].quote == "çok karmaşık"
