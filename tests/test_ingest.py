from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from tutormem.errors import (
    DuplicateContentError,
    ParseError,
    SessionExistsError,
)
from tutormem.ingest import ingest, slugify
from tutormem.storage import Workspace, list_sessions


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_manual_happy_path_normalizes_turns_and_source(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "nested.md",
        "\n### learner\n  İlk soru\n\n\n\nDevam  \n### tutor\n Yanıt \n",
    )
    session = ingest(
        source,
        Workspace(tmp_path / "workspace"),
        course="Veritabanları",
        speakers="manual",
        date="2026-09-27",
    )
    assert session.session_id == "nested"
    assert session.source == "nested.md"
    assert session.date == "2026-09-27"
    assert [(turn.speaker, turn.text) for turn in session.turns] == [
        ("learner", "İlk soru\n\nDevam"),
        ("tutor", "Yanıt"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "unexpected\n### learner\nquestion",
        "### tutor\nanswer",
        "### learner\n\n### tutor\nanswer",
    ],
)
def test_manual_rejects_text_before_heading_or_zero_learner_turns(
    tmp_path: Path, text: str
) -> None:
    source = _write(tmp_path / "manual.md", text)
    with pytest.raises(ParseError):
        ingest(source, Workspace(tmp_path / "workspace"), course="Course", speakers="manual")


def test_gemini_mid_line_and_italic_markers(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "gemini.txt",
        "discard this *User prompt: week 3'e geçtim* Response: Süper, devam. "
        "## Heading User prompt: grafik\n\n\n\nçiz Response: Elbette",
    )
    session = ingest(source, Workspace(tmp_path / "workspace"), course="DL")
    assert [(turn.speaker, turn.text) for turn in session.turns] == [
        ("learner", "week 3'e geçtim"),
        ("tutor", "Süper, devam. ## Heading"),
        ("learner", "grafik\n\nçiz"),
        ("tutor", "Elbette"),
    ]


def test_gemini_prompt_without_response_is_learner_only(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "gemini.md",
        "*User prompt: first only* User prompt: second Response: second answer",
    )
    session = ingest(source, Workspace(tmp_path / "workspace"), course="Course")
    assert [(turn.speaker, turn.text) for turn in session.turns] == [
        ("learner", "first only"),
        ("learner", "second"),
        ("tutor", "second answer"),
    ]


@pytest.mark.parametrize(
    ("learner_text", "expected"),
    [
        (
            "bunun üstünde dura dura* *gidelim. atlamayalım bir yeri",
            "bunun üstünde dura dura gidelim. atlamayalım bir yeri",
        ),
        (
            "sanırım çok önemli. covariate* \n *shift gerçek bir sorun",
            "sanırım çok önemli. covariate shift gerçek bir sorun",
        ),
    ],
)
def test_gemini_cleans_learner_italic_line_break_artifacts(
    tmp_path: Path, learner_text: str, expected: str
) -> None:
    source = _write(
        tmp_path / "gemini.txt",
        f"User prompt: {learner_text} Response: **bold** and *italic*",
    )
    session = ingest(source, Workspace(tmp_path / "workspace"), course="DL")
    assert [(turn.speaker, turn.text) for turn in session.turns] == [
        ("learner", expected),
        ("tutor", "**bold** and *italic*"),
    ]


def test_gemini_keeps_legitimate_single_asterisk_in_learner_turn(tmp_path: Path) -> None:
    source = _write(tmp_path / "gemini.txt", "User prompt: a*b Response: çarpım")
    session = ingest(source, Workspace(tmp_path / "workspace"), course="Math")
    assert session.turns[0].text == "a*b"


def test_gemini_without_marker_is_parse_error(tmp_path: Path) -> None:
    source = _write(tmp_path / "gemini.txt", "Response: no prompt")
    with pytest.raises(ParseError):
        ingest(source, Workspace(tmp_path / "workspace"), course="Course")


def test_pdf_happy_path(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf", reason="PDF happy path needs the installed pdf extra")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    source = tmp_path / "session.pdf"
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    content = DecodedStreamObject()
    content.set_data(
        b"BT /F1 12 Tf 72 720 Td (### learner) Tj 0 -20 Td (pdf learner) Tj "
        b"0 -20 Td (### tutor) Tj 0 -20 Td (pdf tutor) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(source)

    session = ingest(source, Workspace(tmp_path / "workspace"), course="Course", speakers="manual")
    assert [turn.text for turn in session.turns] == ["pdf learner", "pdf tutor"]


def test_pdf_missing_extra_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "session.pdf"
    source.write_bytes(b"not read")
    original_import = builtins.__import__

    def missing_pypdf(name: str, *args: object, **kwargs: object) -> object:
        if name == "pypdf":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_pypdf)
    with pytest.raises(ParseError, match=r"pip install 'tutor-memory\[pdf\]'"):
        ingest(source, Workspace(tmp_path / "workspace"), course="Course")


@pytest.mark.parametrize("name", ["session.csv", "session"])
def test_unsupported_extension_is_parse_error(tmp_path: Path, name: str) -> None:
    source = _write(tmp_path / name, "User prompt: hello")
    with pytest.raises(ParseError):
        ingest(source, Workspace(tmp_path / "workspace"), course="Course")


def test_slugify_turkish_letters_and_empty_result() -> None:
    assert slugify("İlişkisel Modeller Ödev") == "iliskisel-modeller-odev"
    assert slugify("ÇAĞRI_ışığı ÜSTÜ") == "cagri-isigi-ustu"
    with pytest.raises(ParseError):
        slugify("---")


@pytest.mark.parametrize("date", ["27-09-2026", "2026-9-27", "2026-09-27x"])
def test_invalid_date_is_parse_error(tmp_path: Path, date: str) -> None:
    source = _write(tmp_path / "session.md", "### learner\nhello")
    with pytest.raises(ParseError):
        ingest(
            source,
            Workspace(tmp_path / "workspace"),
            course="Course",
            speakers="manual",
            date=date,
        )


def test_duplicate_content_is_rejected_even_with_replace(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "workspace")
    first = _write(tmp_path / "first.md", "### learner\nsame")
    second = _write(tmp_path / "second.md", "### learner\nsame")
    ingest(first, ws, course="Course", speakers="manual", session_id="same")
    with pytest.raises(DuplicateContentError):
        ingest(
            second,
            ws,
            course="Course",
            speakers="manual",
            session_id="same",
            replace=True,
        )


def test_existing_id_requires_replace_and_replace_keeps_index(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "workspace")
    first = _write(tmp_path / "one.md", "### learner\none")
    replacement = _write(tmp_path / "replacement.md", "### learner\ntwo")
    original = ingest(first, ws, course="Course", speakers="manual", session_id="stable")
    with pytest.raises(SessionExistsError):
        ingest(replacement, ws, course="Course", speakers="manual", session_id="stable")

    replaced = ingest(
        replacement,
        ws,
        course="New course",
        speakers="manual",
        session_id="stable",
        replace=True,
    )
    assert original.index == replaced.index == 1
    assert replaced.source == "replacement.md"
    assert list_sessions(ws) == [replaced]


def test_new_session_index_is_max_plus_one(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "workspace")
    first = _write(tmp_path / "first.md", "### learner\nfirst")
    second = _write(tmp_path / "second.md", "### learner\nsecond")
    assert ingest(first, ws, course="C", speakers="manual").index == 1
    assert ingest(second, ws, course="C", speakers="manual").index == 2


def test_json_speakers_preserve_role_markers_inside_tutor_text(tmp_path: Path) -> None:
    source = tmp_path / "gemini-a.turns.json"
    source.write_text(
        json.dumps(
            {
                "format": "tutor-memory-turns/1",
                "turns": [
                    {"speaker": "learner", "text": "Real learner text."},
                    {"speaker": "tutor", "text": "Example:\n### learner\nInjected text."},
                ],
            }
        ),
        encoding="utf-8",
    )

    session = ingest(source, Workspace(tmp_path / "ws"), course="C", speakers="json")

    assert [(turn.speaker, turn.text) for turn in session.turns] == [
        ("learner", "Real learner text."),
        ("tutor", "Example:\n### learner\nInjected text."),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"format": "wrong", "turns": []},
        {"format": "tutor-memory-turns/1", "turns": []},
        {
            "format": "tutor-memory-turns/1",
            "turns": [{"speaker": "system", "text": "bad"}],
        },
    ],
)
def test_json_speakers_validate_structure_and_learner(payload: object, tmp_path: Path) -> None:
    source = tmp_path / "turns.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ParseError):
        ingest(source, Workspace(tmp_path / "ws"), course="C", speakers="json")
