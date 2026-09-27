from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from .errors import DuplicateContentError, ParseError, SessionExistsError
from .models import Session, Turn, turns_sha256
from .storage import Workspace, list_sessions, save_session

_TURKISH_TRANSLATION = str.maketrans(
    {"ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o", "ş": "s", "ü": "u"}
)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SESSION_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
_USER_MARKER_RE = re.compile(r"\*?User prompt:\s*")
_RESPONSE_MARKER_RE = re.compile(r"\s*\*?\s*Response:\s*")
_ITALIC_LINE_BREAK_RE = re.compile(r"\*[ \t]*\n?[ \t]*\*")
_MANUAL_HEADING_RE = re.compile(r"^### (learner|tutor)$", re.MULTILINE)


def slugify(stem: str) -> str:
    """Convert a file stem to a stable ASCII session id."""
    value = stem.replace("İ", "i").lower().translate(_TURKISH_TRANSLATION)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value:
        raise ParseError("file name does not produce a valid session id")
    return value


def _clean_turn(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.strip())


def _clean_gemini_learner(text: str) -> str:
    text = _ITALIC_LINE_BREAK_RE.sub(" ", text)
    return _clean_turn(re.sub(r" +", " ", text))


def _parse_manual(text: str) -> tuple[Turn, ...]:
    headings = list(_MANUAL_HEADING_RE.finditer(text))
    if not headings:
        if text.strip():
            raise ParseError("manual transcript has text before the first heading")
        raise ParseError("manual transcript has no learner turns")
    if text[: headings[0].start()].strip():
        raise ParseError("manual transcript has text before the first heading")

    turns: list[Turn] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = _clean_turn(text[heading.end() : end])
        if body:
            turns.append(Turn(heading.group(1), body))  # type: ignore[arg-type]
    if not any(turn.speaker == "learner" for turn in turns):
        raise ParseError("manual transcript has no learner turns")
    return tuple(turns)


def _parse_gemini(text: str) -> tuple[Turn, ...]:
    markers = list(_USER_MARKER_RE.finditer(text))
    if not markers:
        raise ParseError("Gemini transcript has no User prompt marker")

    turns: list[Turn] = []
    for index, marker in enumerate(markers):
        segment_end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segment = text[marker.end() : segment_end]
        response = _RESPONSE_MARKER_RE.search(segment)
        if response is None:
            if marker.group().startswith("*"):
                segment = re.sub(r"\*\s*\Z", "", segment)
            learner_text = _clean_gemini_learner(segment)
            tutor_text = ""
        else:
            learner_text = _clean_gemini_learner(segment[: response.start()])
            tutor_text = _clean_turn(segment[response.end() :])
        if learner_text:
            turns.append(Turn("learner", learner_text))
        if tutor_text:
            turns.append(Turn("tutor", tutor_text))
    return tuple(turns)


def _read_input(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in (".txt", ".md"):
        return path.read_text(encoding="utf-8")
    if extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ParseError(
                "PDF input needs the 'pdf' extra: pip install 'tutor-memory[pdf]'"
            ) from exc
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ParseError(f"unsupported input extension: {path.suffix or '<none>'}")


def ingest(
    path: Path,
    ws: Workspace,
    *,
    course: str,
    speakers: Literal["gemini", "manual"] = "gemini",
    session_id: str | None = None,
    date: str | None = None,
    replace: bool = False,
) -> Session:
    """Parse and persist a transcript as a canonical session."""
    if date is not None and not _DATE_RE.fullmatch(date):
        raise ParseError("date must use YYYY-MM-DD")

    sid = session_id if session_id is not None else slugify(path.stem)
    if not _SESSION_ID_RE.fullmatch(sid):
        raise ParseError("session_id must match [a-z0-9][a-z0-9-]*")

    text = _read_input(path)
    turns = _parse_gemini(text) if speakers == "gemini" else _parse_manual(text)
    content_sha256 = turns_sha256(turns)
    existing = list_sessions(ws)

    if any(item.content_sha256 == content_sha256 for item in existing):
        raise DuplicateContentError(f"duplicate transcript content: {path.name}")

    previous = next((item for item in existing if item.session_id == sid), None)
    if previous is not None and not replace:
        raise SessionExistsError(f"session already exists: {sid}")
    index = (
        previous.index
        if previous is not None
        else max((item.index for item in existing), default=0) + 1
    )

    session = Session(sid, content_sha256, course, date, index, path.name, turns)
    save_session(ws, session)
    return session
