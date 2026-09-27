from __future__ import annotations

import unicodedata

import pytest

from tutormem.models import Observation, Session, Turn, turns_sha256
from tutormem.verify import normalize, verify


def _session(*turns: Turn) -> Session:
    return Session("s1", turns_sha256(turns), "Ders", None, 1, "s1.md", turns)


def _observation(number: int, *quotes: str) -> Observation:
    return Observation(f"s1:{number}", "inference", f"claim {number}", quotes)


def test_exact_hit_reports_original_location() -> None:
    session = _session(Turn("tutor", "başlangıç"), Turn("learner", "önce beta_1 sonra"))

    result = verify(session, (_observation(1, "beta_1"),))

    assert not result.rejected
    quote = result.verified[0].quotes[0]
    assert (quote.turn_index, quote.start, quote.end) == (1, 5, 11)
    assert quote.matched_text == "beta_1"
    assert quote.exact is True


def test_first_learner_turn_wins_when_quote_is_repeated() -> None:
    session = _session(
        Turn("learner", "ilk ortak metin"),
        Turn("tutor", "ara"),
        Turn("learner", "ikinci ortak metin"),
    )

    quote = verify(session, (_observation(1, "ortak"),)).verified[0].quotes[0]

    assert quote.turn_index == 0
    assert quote.start == 4


def test_exact_search_across_all_turns_precedes_normalized_search() -> None:
    session = _session(Turn("learner", "“alıntı”"), Turn("learner", '"alıntı"'))

    quote = verify(session, (_observation(1, '"alıntı"'),)).verified[0].quotes[0]

    assert quote.turn_index == 1
    assert quote.exact is True


@pytest.mark.parametrize(
    ("text", "quote", "matched_text"),
    [
        ("Bana “örnek” ver.", '"örnek"', "“örnek”"),
        ('Bana "örnek" ver.', "“örnek”", '"örnek"'),
    ],
)
def test_typographic_and_ascii_quotes_match_after_normalization(
    text: str, quote: str, matched_text: str
) -> None:
    result = verify(_session(Turn("learner", text)), (_observation(1, quote),))

    matched = result.verified[0].quotes[0]
    assert matched.exact is False
    assert matched.matched_text == matched_text


@pytest.mark.parametrize("text", ["alpha\nbeta", "alpha  beta"])
def test_whitespace_runs_match_after_normalization(text: str) -> None:
    matched = (
        verify(_session(Turn("learner", text)), (_observation(1, "alpha beta"),))
        .verified[0]
        .quotes[0]
    )

    assert matched.exact is False
    assert matched.matched_text == text


@pytest.mark.parametrize(
    ("text", "quote"),
    [("gül", "gu\u0308l"), ("gu\u0308l", "gül")],
)
def test_nfd_and_nfc_match_in_both_directions(text: str, quote: str) -> None:
    matched = (
        verify(_session(Turn("learner", text)), (_observation(1, quote),)).verified[0].quotes[0]
    )

    assert matched.exact is False
    assert matched.matched_text == text
    assert text[matched.start : matched.end] == text


@pytest.mark.parametrize(
    ("text", "quote"),
    [("İlişkisel", "ilişkisel"), ("ilişkisel", "İlişkisel"), ("I", "ı"), ("ı", "I")],
)
def test_case_and_turkish_i_are_preserved(text: str, quote: str) -> None:
    result = verify(_session(Turn("learner", text)), (_observation(1, quote),))

    assert not result.verified
    assert result.rejected[0].reason == "not_found"


def test_markdown_punctuation_is_significant() -> None:
    session = _session(Turn("learner", "a*b ve beta_1; beta1 değil"))
    observations = (_observation(1, "ab"), _observation(2, "beta_1"))

    result = verify(session, observations)

    assert [item.observation.id for item in result.verified] == ["s1:2"]
    assert result.verified[0].quotes[0].matched_text == "beta_1"
    assert [(item.observation.id, item.reason) for item in result.rejected] == [
        ("s1:1", "not_found")
    ]


@pytest.mark.parametrize("quote", ["", " \t\n"])
def test_empty_quote_is_rejected(quote: str) -> None:
    rejected = verify(_session(Turn("learner", "metin")), (_observation(1, quote),)).rejected[0]

    assert rejected.reason == "empty_quote"
    assert rejected.detail == f"quote 1: {quote!r}"


def test_quote_found_only_in_tutor_turn_is_rejected() -> None:
    session = _session(Turn("learner", "başka metin"), Turn("tutor", "“yalnız tutor”"))

    rejected = verify(session, (_observation(1, '"yalnız tutor"'),)).rejected[0]

    assert rejected.reason == "tutor_turn"
    assert rejected.detail == "quote 1: '\"yalnız tutor\"'"


def test_missing_quote_is_rejected() -> None:
    rejected = verify(_session(Turn("learner", "bulunan")), (_observation(1, "kayıp"),)).rejected[0]

    assert rejected.reason == "not_found"
    assert rejected.detail == "quote 1: 'kayıp'"


def test_session_without_learner_turns_rejects_every_observation() -> None:
    observations = (_observation(1, "tutor sözü"), _observation(2, ""))

    result = verify(_session(Turn("tutor", "tutor sözü")), observations)

    assert not result.verified
    assert [item.reason for item in result.rejected] == [
        "unattributed_transcript",
        "unattributed_transcript",
    ]
    assert [item.detail for item in result.rejected] == [
        "quote 1: 'tutor sözü'",
        "quote 1: ''",
    ]


def test_second_failing_quote_rejects_the_whole_observation() -> None:
    observation = _observation(1, "bulunan", "kayıp")

    result = verify(_session(Turn("learner", "bulunan")), (observation,))

    assert not result.verified
    assert result.rejected[0].reason == "not_found"
    assert result.rejected[0].detail == "quote 2: 'kayıp'"


def test_verified_and_rejected_results_preserve_input_order() -> None:
    observations = (
        _observation(1, "bir"),
        _observation(2, "yok iki"),
        _observation(3, "üç"),
        _observation(4, "yok dört"),
    )

    result = verify(_session(Turn("learner", "bir ve üç")), observations)

    assert [item.observation.id for item in result.verified] == ["s1:1", "s1:3"]
    assert [item.observation.id for item in result.rejected] == ["s1:2", "s1:4"]


def test_normalize_mapping_is_valid_and_non_decreasing() -> None:
    samples = (
        "",
        "  İlişkisel\töğrenme\n",
        "gu\u0308l “alıntı” ve ‘tek’",
        "\u0308başta işaret   sonra_1*a",
        "çğışöü\r\n„karma‟\u0301",
    )

    for text in samples:
        normalized, mapping = normalize(text)
        assert len(mapping) == len(normalized)
        assert all(0 <= source_index < len(text) for source_index in mapping)
        assert mapping == sorted(mapping)
        assert unicodedata.is_normalized("NFC", normalized)
