from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from .models import (
    Observation,
    RejectedObservation,
    Session,
    Turn,
    VerifiedObservation,
    VerifiedQuote,
    VerifyResult,
)

_QUOTE_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
    }
)


def _nfc_clusters(text: str):
    index = 0
    while index < len(text):
        start = index
        index += 1
        while index < len(text) and unicodedata.combining(text[index]):
            index += 1
        yield unicodedata.normalize("NFC", text[start:index]), start


def _cluster_end(text: str, start: int) -> int:
    end = start + 1
    while end < len(text) and unicodedata.combining(text[end]):
        end += 1
    return end


def normalize(text: str) -> tuple[str, list[int]]:
    """Normalize quote-search text and map normalized characters to source indices."""
    characters: list[str] = []
    source_indices: list[int] = []

    for cluster, source_index in _nfc_clusters(text):
        for character in cluster.translate(_QUOTE_TRANSLATION):
            if character.isspace():
                if characters and characters[-1] != " ":
                    characters.append(" ")
                    source_indices.append(source_index)
            else:
                characters.append(character)
                source_indices.append(source_index)

    if characters and characters[-1] == " ":
        characters.pop()
        source_indices.pop()
    return "".join(characters), source_indices


def _find_quote(quote: str, turns: Sequence[tuple[int, Turn]]) -> VerifiedQuote | None:
    for turn_index, turn in turns:
        start = turn.text.find(quote)
        if start >= 0:
            end = start + len(quote)
            return VerifiedQuote(quote, turn_index, start, end, turn.text[start:end], True)

    normalized_quote, _ = normalize(quote)
    for turn_index, turn in turns:
        normalized_text, source_indices = normalize(turn.text)
        normalized_start = normalized_text.find(normalized_quote)
        if normalized_start >= 0:
            normalized_end = normalized_start + len(normalized_quote)
            start = source_indices[normalized_start]
            end = _cluster_end(turn.text, source_indices[normalized_end - 1])
            return VerifiedQuote(quote, turn_index, start, end, turn.text[start:end], False)
    return None


def verify(session: Session, observations: Sequence[Observation]) -> VerifyResult:
    """Verify every observation quote against learner turns."""
    learner_turns = tuple(
        (turn_index, turn)
        for turn_index, turn in enumerate(session.turns)
        if turn.speaker == "learner"
    )
    tutor_turns = tuple(
        (turn_index, turn)
        for turn_index, turn in enumerate(session.turns)
        if turn.speaker == "tutor"
    )
    verified: list[VerifiedObservation] = []
    rejected: list[RejectedObservation] = []

    for observation in observations:
        if not learner_turns:
            rejected.append(
                RejectedObservation(
                    observation,
                    "unattributed_transcript",
                    f"quote 1: {observation.quotes[0]!r}",
                )
            )
            continue

        verified_quotes: list[VerifiedQuote] = []
        for quote_index, quote in enumerate(observation.quotes):
            detail = f"quote {quote_index + 1}: {quote!r}"
            if not quote.strip():
                rejected.append(RejectedObservation(observation, "empty_quote", detail))
                break

            match = _find_quote(quote, learner_turns)
            if match is not None:
                verified_quotes.append(match)
                continue

            reason = "tutor_turn" if _find_quote(quote, tutor_turns) is not None else "not_found"
            rejected.append(RejectedObservation(observation, reason, detail))
            break
        else:
            verified.append(VerifiedObservation(observation, tuple(verified_quotes)))

    return VerifyResult(
        session.session_id,
        session.content_sha256,
        tuple(verified),
        tuple(rejected),
    )
