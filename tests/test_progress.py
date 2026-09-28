from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tutormem.cli import main
from tutormem.config import Config, ProgressConfig
from tutormem.errors import ExtractorError, ParseError
from tutormem.extract import ProgressExtractor
from tutormem.models import Turn
from tutormem.progress import (
    CourseProgress,
    LastSession,
    Progress,
    apply_session,
    load_progress,
    render_progress,
    split_base,
    write_progress,
)
from tutormem.storage import Workspace

HEADING = "## Dersler ve kaldığı yer"
COURSES = (
    "Intro to Database",
    "Deep Learning Specialization (DeepLearning.AI, Coursera) — aktif ders",
    "Object Oriented Analysis and Design",
    "Operating Systems",
    "Computer Networks",
    "Software Testing",
    "Data Structures",
    "Discrete Mathematics",
    "Technical English",
)


def _base() -> str:
    course_blocks = "\n\n".join(
        f"### {name}\n- Sabit not: {number}" for number, name in enumerate(COURSES, 1)
    )
    return (
        "# Öğretmen özeti\n\n"
        "## Oturum başı\n\nHazırlığı kontrol et.\n\n"
        "## Nasıl anlatacaksın\n\nAdım adım anlat.\n\n"
        "## Oturum sonu\n\nSonraki adımı sor.\n\n"
        f"{HEADING}\nBu satır derslerden önceki açıklamadır.\n\n{course_blocks}\n\n"
        "## Ek kurallar\n\nKısa tut.\n"
    )


def _agy_stdout(payload: dict[str, object]) -> str:
    result = {"status": "SUCCESS", "response": json.dumps(payload, ensure_ascii=False)}
    return json.dumps({"event": "result", "result": result}, ensure_ascii=False)


def test_split_base_realistic_courses_and_render_round_trip() -> None:
    base_without, progress = split_base(_base(), HEADING)

    assert HEADING not in base_without
    assert "## Ek kurallar" in base_without
    assert progress.preamble == ("Bu satır derslerden önceki açıklamadır.",)
    assert tuple(course.name for course in progress.courses) == COURSES
    assert progress.courses[1].static == ("- Sabit not: 2",)

    labels = ProgressConfig(
        heading=HEADING,
        last_label="Son oturum",
        next_label="Sıradaki",
    )
    rendered = render_progress(progress, labels)
    expected_blocks = "\n\n".join(
        f"### {name}\n- Sabit not: {number}" for number, name in enumerate(COURSES, 1)
    )
    assert rendered == (
        f"{HEADING}\nBu satır derslerden önceki açıklamadır.\n\n{expected_blocks}\n"
    )


def test_split_base_requires_exact_heading() -> None:
    with pytest.raises(ParseError, match="not found"):
        split_base(_base(), "## Missing")


def test_progress_serialization_and_apply_session_ordering(tmp_path: Path) -> None:
    original = Progress((CourseProgress("Databases", ("- Keep this",), None),), ("Intro",))
    first = apply_session(original, "Databases", "s2", 2, None, "Relations", "Keys")
    older = apply_session(first, "Databases", "s1", 1, "2026-09-01", "Old", "Old next")
    replaced = apply_session(first, "Databases", "s2", 2, "2026-09-28", "Keys", "Joins")

    assert original.courses[0].last is None
    assert older is first
    assert replaced.courses[0].last == LastSession("s2", 2, "2026-09-28", "Keys", "Joins")
    assert apply_session(first, "Unknown", "s3", 3, None, "X", "Y") is first

    ws = Workspace(tmp_path)
    write_progress(ws, replaced)
    assert load_progress(ws) == replaced


def test_progress_extractor_protocol_validation_and_no_transcript_in_argv() -> None:
    captured: dict[str, object] = {}
    covered = "  " + "a" * 205 + "  "
    next_value = "  " + "b" * 165 + "  "

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, stdin=stdin, timeout=timeout)
        payload = {"course": "Unknown", "covered": covered, "next": next_value}
        return subprocess.CompletedProcess(args, 0, _agy_stdout(payload), "")

    turns = (Turn("learner", "Bugün ilişkileri işledik."), Turn("tutor", "Tamam."))
    result = ProgressExtractor("flash", 9, runner=runner, language="Turkish").extract(
        turns, ("Databases",)
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert "Bugün ilişkileri işledik." not in args
    assert json.loads(args[args.index("--json-schema") + 1])["required"] == [
        "course",
        "covered",
        "next",
    ]
    prompt = json.loads(str(captured["stdin"]))["message"]["content"]
    assert "Turkish" in prompt
    assert "- Databases" in prompt
    assert "[learner #0]" in prompt
    assert result.course is None
    assert len(result.covered) == 200
    assert len(result.next) == 160


def test_progress_extractor_rejects_empty_covered() -> None:
    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        del stdin, timeout
        payload = {"course": None, "covered": "  ", "next": "Start here"}
        return subprocess.CompletedProcess(args, 0, _agy_stdout(payload), "")

    with pytest.raises(ExtractorError, match="must not be empty"):
        ProgressExtractor("flash", 9, runner=runner).extract((Turn("learner", "Hi"),), ())


def test_progress_config_defaults_inherit_extract_model_and_override(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    ws.config_path.write_text(
        """
[extract]
model = "extract-model"

[progress]
enabled = false
heading = "## İlerleme"
last_label = "Son"
next_label = "Devam"
language = "Turkish"
""".strip(),
        encoding="utf-8",
    )
    config = Config.load(ws)

    assert Config().progress.model == Config().extract.model
    assert config.progress.model == "extract-model"
    assert config.progress.enabled is False
    assert config.progress.heading == "## İlerleme"


def test_progress_init_writes_backup_and_refuses_second_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = Workspace(tmp_path)
    original = _base()
    ws.base_path.write_text(original, encoding="utf-8")
    ws.config_path.write_text(f'[progress]\nheading = "{HEADING}"\n', encoding="utf-8")

    assert main(["--workspace", str(ws.root), "progress-init"]) == 0
    assert ws.base_path.with_name("base.md.bak").read_text(encoding="utf-8") == original
    assert HEADING not in ws.base_path.read_text(encoding="utf-8")
    assert load_progress(ws) is not None

    assert main(["--workspace", str(ws.root), "progress-show"]) == 0
    assert capsys.readouterr().out.startswith(HEADING)
    assert main(["--workspace", str(ws.root), "progress-init"]) == 1
    assert "already exists" in capsys.readouterr().err
