from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
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
    Observation,
    Session,
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

    def __init__(self, model: str, timeout_s: int, *, runner: Runner | None = None) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._runner = runner or _default_runner

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
        )
        args = [
            "agy",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            self.model,
            "--sandbox",
            "--print-timeout",
            f"{self.timeout_s}s",
            "--json-schema",
            json.dumps(observation_list_schema()),
            "-p=",
        ]
        stdin = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        try:
            completed = self._runner(args, stdin, float(self.timeout_s))
        except subprocess.TimeoutExpired as exc:
            raise ExtractorError(f"agy extractor timed out after {self.timeout_s}s") from exc
        if completed.returncode != 0:
            message = f"agy extractor exited with status {completed.returncode}"
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
            raise ExtractorError(
                f"agy extractor returned no result event{_stderr_suffix(completed)}"
            )
        result = result_event["result"]
        assert isinstance(result, dict)
        if result.get("status") != "SUCCESS":
            error = result.get("error")
            message = f"agy extractor failed: {error}" if error else "agy extractor failed"
            raise ExtractorError(message + _stderr_suffix(completed))
        response = result.get("response")
        if not isinstance(response, str):
            raise ExtractorError(
                f"agy extractor result has no response text{_stderr_suffix(completed)}"
            )
        try:
            payload = _first_json_object(response)
        except ValueError as exc:
            raise ExtractorError(
                f"agy extractor response contains no JSON object{_stderr_suffix(completed)}"
            ) from exc
        return _parse_observations(payload, session, extractor=self.name, model=self.model)


def make_extractor(
    name: Literal["agy", "file"], config: Config, *, path: Path | None = None
) -> Extractor:
    """Create an extractor from CLI and configuration values."""
    if name == "agy":
        return AgyExtractor(config.extract.model, config.extract.timeout_s)
    if name == "file":
        if path is None:
            raise ExtractorError("file extractor requires --from PATH")
        return FileExtractor(path)
    raise ExtractorError(f"unknown extractor: {name}")
