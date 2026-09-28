from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Literal, Protocol

from .config import Config
from .errors import ExtractorError, SchemaError
from .models import (
    DroppedRecord,
    ExtractResult,
    Hypothesis,
    Instruction,
    Model,
    Observation,
    Session,
    Turn,
    observation_list_schema,
)

Runner = Callable[[list[str], str, float], subprocess.CompletedProcess[str]]
_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


class Extractor(Protocol):
    """Contract implemented by observation extractors."""

    name: str
    model: str | None

    def extract(
        self, session: Session, open_items: Sequence[Instruction | Hypothesis]
    ) -> ExtractResult:
        """Extract proposed observations from one session."""
        raise NotImplementedError


def _default_runner(
    args: list[str], stdin: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _stderr_suffix(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr or ""
    return f"; stderr: {stderr[:500]}" if stderr else ""


def _first_json_object(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object found")


def _run_agy(
    prompt: str,
    schema: dict[str, object],
    model: str,
    timeout_s: int,
    runner: Runner,
    *,
    operation: str,
) -> dict[str, object]:
    args = [
        "agy",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--sandbox",
        "--print-timeout",
        f"{timeout_s}s",
        "--json-schema",
        json.dumps(schema),
        "-p=",
    ]
    stdin = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
    try:
        completed = runner(args, stdin, float(timeout_s))
    except subprocess.TimeoutExpired as exc:
        raise ExtractorError(f"agy {operation} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise ExtractorError(f"cannot run agy {operation}: {exc}") from exc
    if completed.returncode != 0:
        message = f"agy {operation} exited with status {completed.returncode}"
        raise ExtractorError(message + _stderr_suffix(completed))

    result_event: dict[str, object] | None = None
    for line in (completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result_event = event
    if result_event is None or not isinstance(result_event.get("result"), dict):
        raise ExtractorError(f"agy {operation} returned no result event{_stderr_suffix(completed)}")
    result = result_event["result"]
    assert isinstance(result, dict)
    if result.get("status") != "SUCCESS":
        error = result.get("error")
        message = f"agy {operation} failed: {error}" if error else f"agy {operation} failed"
        raise ExtractorError(message + _stderr_suffix(completed))
    response = result.get("response")
    if not isinstance(response, str):
        raise ExtractorError(
            f"agy {operation} result has no response text{_stderr_suffix(completed)}"
        )
    try:
        return _first_json_object(response)
    except ValueError as exc:
        raise ExtractorError(
            f"agy {operation} response contains no JSON object{_stderr_suffix(completed)}"
        ) from exc


def _parse_observations(
    payload: object, session: Session, *, extractor: str, model: str | None
) -> ExtractResult:
    if not isinstance(payload, dict) or set(payload) != {"observations"}:
        raise ExtractorError("extractor output must be an object containing only observations")
    items = payload["observations"]
    if not isinstance(items, list):
        raise ExtractorError("extractor observations must be an array")

    observations: list[Observation] = []
    dropped: list[DroppedRecord] = []
    for number, item in enumerate(items, start=1):
        raw = json.dumps(item, ensure_ascii=False)
        candidate = (
            {**item, "id": f"{session.session_id}:{number}"} if isinstance(item, dict) else item
        )
        try:
            observation = Observation.from_dict(candidate)
        except (SchemaError, TypeError, ValueError) as exc:
            dropped.append(DroppedRecord(raw, str(exc)))
        else:
            observations.append(observation)
    return ExtractResult(
        session.session_id,
        session.content_sha256,
        extractor,
        model,
        tuple(observations),
        tuple(dropped),
    )


class FileExtractor:
    """Read deterministic extractor output from a JSON file."""

    name = "file"
    model = None

    def __init__(self, path: Path) -> None:
        self.path = path

    def extract(
        self, session: Session, open_items: Sequence[Instruction | Hypothesis]
    ) -> ExtractResult:
        """Extract observations from the configured JSON file."""
        del open_items
        try:
            with self.path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ExtractorError(f"cannot read extractor file: {exc}") from exc
        return _parse_observations(payload, session, extractor=self.name, model=self.model)


class AgyExtractor:
    """Extract observations with the configured Antigravity command."""

    name = "agy"

    def __init__(
        self,
        model: str,
        timeout_s: int,
        *,
        runner: Runner | None = None,
        base: str | None = None,
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._runner = runner or _default_runner
        self.base = base

    def extract(
        self, session: Session, open_items: Sequence[Instruction | Hypothesis]
    ) -> ExtractResult:
        """Extract observations from a canonical transcript."""
        transcript = "\n\n".join(
            f"[{turn.speaker} #{index}]\n{turn.text}" for index, turn in enumerate(session.turns)
        )
        open_items_text = "\n".join(f"{item.id}: {item.claim}" for item in open_items) or "(none)"
        try:
            prompt_text = (_PROMPTS / "extract.md").read_text(encoding="utf-8")
        except OSError as exc:
            raise ExtractorError(f"cannot read extraction prompt: {exc}") from exc
        prompt = Template(prompt_text).substitute(
            transcript=transcript,
            open_items=open_items_text,
            base=self.base.strip() if self.base is not None and self.base.strip() else "(none)",
        )
        payload = _run_agy(
            prompt,
            observation_list_schema(),
            self.model,
            self.timeout_s,
            self._runner,
            operation="extractor",
        )
        return _parse_observations(payload, session, extractor=self.name, model=self.model)


@dataclass(frozen=True, slots=True)
class ProgressResult(Model):
    course: str | None
    covered: str
    next: str


@dataclass(frozen=True, slots=True)
class ProgressRun(Model):
    session_id: str
    content_sha256: str
    course: str | None
    covered: str
    next: str

    @classmethod
    def from_result(
        cls, session_id: str, content_sha256: str, result: ProgressResult
    ) -> ProgressRun:
        return cls(session_id, content_sha256, result.course, result.covered, result.next)

    def result(self) -> ProgressResult:
        return ProgressResult(self.course, self.covered, self.next)


def _progress_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "course": {"type": ["string", "null"]},
            "covered": {"type": "string", "maxLength": 200},
            "next": {"type": "string", "maxLength": 160},
        },
        "required": ["course", "covered", "next"],
        "additionalProperties": False,
    }


class ProgressExtractor:
    """Extract a course and concrete continuation point with Antigravity."""

    def __init__(
        self,
        model: str,
        timeout_s: int,
        *,
        runner: Runner | None = None,
        language: str = "English",
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.language = language
        self._runner = runner or _default_runner

    def extract(self, session_turns: Sequence[Turn], course_names: Sequence[str]) -> ProgressResult:
        """Extract progress from transcript turns, constrained to known course names."""
        transcript = "\n\n".join(
            f"[{turn.speaker} #{index}]\n{turn.text}" for index, turn in enumerate(session_turns)
        )
        courses = "\n".join(f"- {name}" for name in course_names) or "(none)"
        try:
            prompt_text = (_PROMPTS / "progress.md").read_text(encoding="utf-8")
        except OSError as exc:
            raise ExtractorError(f"cannot read progress prompt: {exc}") from exc
        prompt = Template(prompt_text).substitute(
            courses=courses,
            transcript=transcript,
            language=self.language,
        )
        payload = _run_agy(
            prompt,
            _progress_schema(),
            self.model,
            self.timeout_s,
            self._runner,
            operation="progress extractor",
        )
        if set(payload) != {"course", "covered", "next"}:
            raise ExtractorError("progress extractor output has invalid fields")
        course = payload["course"]
        covered = payload["covered"]
        next_value = payload["next"]
        if course is not None and not isinstance(course, str):
            raise ExtractorError("progress extractor course must be a string or null")
        if not isinstance(covered, str) or not isinstance(next_value, str):
            raise ExtractorError("progress extractor covered and next must be strings")
        normalized_course = course.strip() if isinstance(course, str) else None
        if normalized_course not in course_names:
            normalized_course = None
        normalized_covered = covered.strip()[:200]
        if not normalized_covered:
            raise ExtractorError("progress extractor covered must not be empty")
        return ProgressResult(
            normalized_course,
            normalized_covered,
            next_value.strip()[:160],
        )


def make_extractor(
    name: Literal["agy", "file"],
    config: Config,
    *,
    path: Path | None = None,
    base: str | None = None,
) -> Extractor:
    """Create an extractor from CLI and configuration values."""
    if name == "agy":
        return AgyExtractor(config.extract.model, config.extract.timeout_s, base=base)
    if name == "file":
        if path is None:
            raise ExtractorError("file extractor requires --from PATH")
        return FileExtractor(path)
    raise ExtractorError(f"unknown extractor: {name}")
