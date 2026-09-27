from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from tutormem import extract, ingest, promote, render, review, sync, verify
from tutormem.cli import main
from tutormem.config import Config
from tutormem.errors import SchemaError, StaleArtifactError
from tutormem.models import (
    SCHEMA_VERSION,
    ApprovedEvidence,
    Decision,
    DecisionFile,
    DroppedRecord,
    EvidenceRef,
    ExtractResult,
    Hypothesis,
    Instruction,
    Observation,
    ProfileState,
    RejectedObservation,
    Revocation,
    Session,
    SessionApproval,
    SessionRecord,
    StuckPoint,
    Turn,
    VerifiedObservation,
    VerifiedQuote,
    VerifyResult,
    observation_list_schema,
    turns_sha256,
)
from tutormem.storage import (
    Workspace,
    list_sessions,
    load_artifact,
    save_session,
    write_json,
    write_text,
)


def _samples() -> list[object]:
    turns = (Turn("learner", "İlişkisel ığüşöç"), Turn("tutor", "Açıklama"))
    sha = turns_sha256(turns)
    session = Session("oturum-1", sha, "Veritabanları", "2026-09-27", 1, "ders.md", turns)
    observation = Observation(
        "oturum-1:1", "inference", "Şema ister.", ("İlişkisel",), None, "Şema"
    )
    dropped = DroppedRecord('{"kind": 4}', "kind: expected string")
    extracted = ExtractResult("oturum-1", sha, "file", None, (observation,), (dropped,))
    quote = VerifiedQuote("İlişkisel", 0, 0, 10, "İlişkisel", True)
    verified_observation = VerifiedObservation(observation, (quote,))
    rejected = RejectedObservation(observation, "not_found", "missing: ığüşöç")
    verified = VerifyResult("oturum-1", sha, (verified_observation,), (rejected,))
    decision = Decision("oturum-1:1", "accept", "human", "dar kanıt", "Şema ister.")
    revocation = Revocation("hyp-eski:1", "human", "Geri çekildi")
    decision_file = DecisionFile("oturum-1", sha, (decision,), (revocation,))
    evidence = ApprovedEvidence(
        "oturum-1", "oturum-1:1", "inference", "Şema ister.", (quote,), None, False, "Şema"
    )
    approval = SessionApproval("oturum-1", (evidence,), (revocation,))
    evidence_ref = EvidenceRef("oturum-1", "oturum-1:1", "İlişkisel")
    instruction = Instruction(
        "ins-oturum-1:1", "Adım adım ilerle.", "active", (evidence_ref,), 1, None
    )
    hypothesis = Hypothesis(
        "hyp-oturum-1:1",
        "Şema ister.",
        "open",
        ("oturum-1",),
        (evidence_ref,),
        1,
        1,
        None,
        None,
        None,
    )
    stuck = StuckPoint("oturum-1", "Veritabanları", "Normalizasyon", "Zorlandı.", "ığüşöç")
    record = SessionRecord("oturum-1", 1, "Veritabanları", None, sha, "applied", 1)
    profile = ProfileState(SCHEMA_VERSION, (instruction,), (hypothesis,), (stuck,), (record,))
    return [
        turns[0],
        session,
        observation,
        dropped,
        extracted,
        quote,
        verified_observation,
        rejected,
        verified,
        decision,
        revocation,
        decision_file,
        evidence,
        approval,
        evidence_ref,
        instruction,
        hypothesis,
        stuck,
        record,
        profile,
    ]


@pytest.mark.parametrize("value", _samples(), ids=lambda value: type(value).__name__)
def test_model_round_trip(value: object) -> None:
    assert type(value).from_dict(value.to_dict()) == value  # type: ignore[attr-defined]


def test_optional_fields_may_be_omitted() -> None:
    observation = Observation.from_dict(
        {"id": "s1:1", "kind": "inference", "claim": "Claim", "quotes": ["quote"]}
    )
    decision = Decision.from_dict(
        {"observation_id": "s1:1", "action": "accept", "reviewer": "human"}
    )
    decisions = DecisionFile.from_dict(
        {"session_id": "s1", "content_sha256": "a" * 64, "decisions": []}
    )
    assert observation.proposed_match is None
    assert decision.reason == ""
    assert decisions.revocations == ()


@pytest.mark.parametrize(
    "factory,payload",
    [
        (Turn.from_dict, {"speaker": "learner"}),
        (Turn.from_dict, {"speaker": "learner", "text": "x", "extra": True}),
        (Turn.from_dict, {"speaker": "learner", "text": 7}),
        (Turn.from_dict, {"speaker": "system", "text": "x"}),
        (
            Session.from_dict,
            {
                "session_id": "Bad_id",
                "content_sha256": "a" * 64,
                "course": "C",
                "date": None,
                "index": 1,
                "source": "x.md",
                "turns": [],
            },
        ),
        (
            Observation.from_dict,
            {"id": "s1:1", "kind": "inference", "claim": "x", "quotes": []},
        ),
        (
            Decision.from_dict,
            {
                "observation_id": "s1:1",
                "action": "accept",
                "reviewer": "human",
                "match": "hyp-s1:1",
                "force_new": True,
            },
        ),
    ],
)
def test_schema_errors(factory: object, payload: dict[str, object]) -> None:
    with pytest.raises(SchemaError):
        factory(payload)  # type: ignore[operator]


def test_turns_sha256_is_stable_and_order_sensitive() -> None:
    first = Turn("learner", "İlişkisel")
    second = Turn("tutor", "ığüşöç")
    assert turns_sha256((first, second)) == turns_sha256((first, second))
    assert turns_sha256((first, second)) != turns_sha256((second, first))


def test_observation_list_schema_contract() -> None:
    schema = observation_list_schema()
    assert isinstance(schema, dict)
    assert "observations" in schema["required"]
    assert "id" not in schema["properties"]["observations"]["items"]["properties"]


def _session(sid: str, index: int, text: str = "metin") -> Session:
    turns = (Turn("learner", text),)
    return Session(sid, turns_sha256(turns), "Ders", None, index, f"{sid}.md", turns)


def test_atomic_writes_leave_no_temporary_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.json"
    write_json(target, {"ığüşöç": "İlişkisel"})
    write_text(tmp_path / "nested" / "note.md", "metin\n")
    assert json.loads(target.read_text(encoding="utf-8")) == {"ığüşöç": "İlişkisel"}
    assert not [path for path in target.parent.iterdir() if path.name.startswith(".")]


def test_load_artifact_missing_and_stale(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    session = _session("s1", 1)
    assert load_artifact(ws.observations_path("s1"), ExtractResult, session) is None
    stale = ExtractResult("s1", "f" * 64, "file", None, (), ())
    write_json(ws.observations_path("s1"), stale)
    with pytest.raises(StaleArtifactError):
        load_artifact(ws.observations_path("s1"), ExtractResult, session)


def test_list_sessions_sorts_by_index(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    save_session(ws, _session("second", 2))
    save_session(ws, _session("first", 1))
    assert [item.session_id for item in list_sessions(ws)] == ["first", "second"]


def test_config_defaults_without_file(tmp_path: Path) -> None:
    config = Config.load(Workspace(tmp_path))
    assert config.threshold == 3
    assert config.stale_after == 5
    assert config.extract.model == "gemini-3.8-flash-medium"


def test_config_values_from_file(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.config_path.write_text(
        """
[rules]
threshold = 4
stale_after = 8
[extract]
extractor = "file"
timeout_s = 12
[review]
mode = "claude"
[sync]
doc_title = "Öğrenme özeti"
""".strip(),
        encoding="utf-8",
    )
    config = Config.load(ws)
    assert config.threshold == 4
    assert config.stale_after == 8
    assert config.extract.extractor == "file"
    assert config.extract.timeout_s == 12
    assert config.review.mode == "claude"
    assert config.sync.doc_title == "Öğrenme özeti"


def test_config_rejects_unknown_key(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.config_path.write_text("[rules]\nunknown = 3\n", encoding="utf-8")
    with pytest.raises(SchemaError):
        Config.load(ws)


@pytest.mark.parametrize(
    "callable_,parameters",
    [
        (ingest.ingest, ["path", "ws", "course", "speakers", "session_id", "date", "replace"]),
        (ingest.slugify, ["stem"]),
        (verify.verify, ["session", "observations"]),
        (verify.normalize, ["text"]),
        (extract.FileExtractor, ["path"]),
        (extract.AgyExtractor, ["model", "timeout_s"]),
        (extract.make_extractor, ["name", "config", "path"]),
        (review.write_packet, ["session", "result", "profile"]),
        (review.skeleton, ["result"]),
        (review.ClaudeReviewer, ["model", "timeout_s"]),
        (review.apply_decisions, ["result", "decisions"]),
        (promote.replay, ["sessions", "approvals", "config"]),
        (render.render_brief, ["state", "courses_md"]),
        (render.render_profile, ["state"]),
        (sync.sync, ["ws", "config", "dry_run", "drive"]),
    ],
)
def test_public_contract_signatures(callable_: object, parameters: list[str]) -> None:
    actual = list(inspect.signature(callable_).parameters.values())
    assert [parameter.name for parameter in actual[: len(parameters)]] == parameters
    for parameter in actual[len(parameters) :]:
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is not inspect.Parameter.empty


def test_cli_status_empty_workspace(tmp_path: Path) -> None:
    assert main(["--workspace", str(tmp_path), "status"]) == 0


def test_cli_verify_unknown_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--workspace", str(tmp_path), "verify", "missing"]) == 1
    assert capsys.readouterr().err.startswith("error:")
