from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tutormem.config import Config
from tutormem.errors import ExtractorError
from tutormem.extract import AgyExtractor, FileExtractor, make_extractor
from tutormem.models import Session, Turn, turns_sha256


def _session() -> Session:
    turns = (
        Turn("learner", "Şemayı adım adım göster."),
        Turn("tutor", "Elbette."),
    )
    return Session("s1", turns_sha256(turns), "DB", None, 1, "s1.md", turns)


def _agy_stdout(response: str, *, status: str = "SUCCESS", error: str | None = None) -> str:
    result = {"status": status, "response": response, "error": error}
    return "\n".join(
        [json.dumps({"event": "progress"}), json.dumps({"event": "result", "result": result})]
    )


def _payload() -> dict[str, object]:
    return {
        "observations": [
            {
                "kind": "explicit_instruction",
                "claim": "Use steps.",
                "quotes": ["Şemayı adım adım göster."],
            }
        ]
    }


def test_file_extractor_assigns_output_position_ids_and_drops_invalid(tmp_path: Path) -> None:
    path = tmp_path / "observations.json"
    path.write_text(
        json.dumps(
            {
                "observations": [
                    {
                        "kind": "inference",
                        "claim": "Uses diagrams.",
                        "quotes": ["Şemayı"],
                    },
                    {"kind": "inference", "claim": "Broken", "quotes": []},
                    {
                        "kind": "stuck_point",
                        "claim": "Struggled with schemas.",
                        "quotes": ["Şemayı"],
                        "concept": "Schema",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = FileExtractor(path).extract(_session(), ())

    assert [item.id for item in result.observations] == ["s1:1", "s1:3"]
    assert len(result.dropped) == 1
    assert '"quotes": []' in result.dropped[0].raw
    assert "at least one" in result.dropped[0].error


@pytest.mark.parametrize(
    "response",
    [
        "```json\n" + json.dumps(_payload(), ensure_ascii=False) + "\n```",
        "Here is the result: " + json.dumps(_payload(), ensure_ascii=False) + " done.",
    ],
)
def test_agy_extractor_stream_json_protocol_and_wrapped_response(response: str) -> None:
    captured: dict[str, object] = {}

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, stdin=stdin, timeout=timeout)
        return subprocess.CompletedProcess(args, 0, _agy_stdout(response), "")

    result = AgyExtractor("gemini-test", 17, runner=runner).extract(_session(), ())

    args = captured["args"]
    assert isinstance(args, list)
    assert args[-1] == "-p="
    assert "Şemayı adım adım göster." not in args
    assert args[:5] == ["agy", "--input-format", "stream-json", "--output-format", "stream-json"]
    assert "--sandbox" in args
    assert args[args.index("--print-timeout") + 1] == "17s"
    assert json.loads(args[args.index("--json-schema") + 1])["required"] == ["observations"]
    stdin_event = json.loads(str(captured["stdin"]))
    prompt = stdin_event["message"]["content"]
    assert "[learner #0]\nŞemayı adım adım göster." in prompt
    assert "[tutor #1]\nElbette." in prompt
    assert result.observations[0].id == "s1:1"


def test_agy_extractor_shows_open_items_in_prompt(tmp_path: Path) -> None:
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps(_payload()), encoding="utf-8")
    open_item = FileExtractor(item_file).extract(_session(), ()).observations[0]
    # An Observation is structurally unsuitable as an open item, so use the fields through a stub.
    stub = type("OpenItem", (), {"id": "hyp-old:1", "claim": open_item.claim})()
    captured = ""

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal captured
        captured = stdin
        return subprocess.CompletedProcess(args, 0, _agy_stdout(json.dumps(_payload())), "")

    AgyExtractor("m", 1, runner=runner).extract(_session(), (stub,))  # type: ignore[arg-type]
    assert "hyp-old:1: Use steps." in captured


def test_agy_extractor_includes_base_brief_or_none() -> None:
    captured: list[str] = []

    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        captured.append(stdin)
        return subprocess.CompletedProcess(args, 0, _agy_stdout(json.dumps(_payload())), "")

    AgyExtractor("m", 1, runner=runner, base="# Existing\n\nUse diagrams.").extract(_session(), ())
    AgyExtractor("m", 1, runner=runner).extract(_session(), ())

    first_prompt = json.loads(captured[0])["message"]["content"]
    second_prompt = json.loads(captured[1])["message"]["content"]
    assert "Already in the brief (do not propose these again):\n# Existing" in first_prompt
    assert "Already in the brief (do not propose these again):\n(none)" in second_prompt


def test_agy_extractor_error_result_includes_error_and_truncated_stderr() -> None:
    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, _agy_stdout("", status="ERROR", error="quota"), "x" * 700
        )

    with pytest.raises(ExtractorError) as raised:
        AgyExtractor("m", 1, runner=runner).extract(_session(), ())
    assert "quota" in str(raised.value)
    assert "x" * 500 in str(raised.value)
    assert "x" * 501 not in str(raised.value)


def test_agy_extractor_rejects_missing_result_event() -> None:
    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, '{"event":"progress"}\n', "details")

    with pytest.raises(ExtractorError, match="no result event"):
        AgyExtractor("m", 1, runner=runner).extract(_session(), ())


def test_agy_extractor_converts_timeout() -> None:
    def runner(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(ExtractorError, match="timed out"):
        AgyExtractor("m", 1, runner=runner).extract(_session(), ())


def test_agy_extractor_rejects_nonzero_and_invalid_response() -> None:
    def failed(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 2, "", "bad")

    with pytest.raises(ExtractorError, match="status 2"):
        AgyExtractor("m", 1, runner=failed).extract(_session(), ())

    def no_json(args: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, _agy_stdout("nothing structured"), "")

    with pytest.raises(ExtractorError, match="no JSON object"):
        AgyExtractor("m", 1, runner=no_json).extract(_session(), ())


def test_make_extractor_requires_file_path() -> None:
    with pytest.raises(ExtractorError, match="--from"):
        make_extractor("file", Config())


def test_extract_prompt_distinguishes_standing_instructions_from_one_off_requests() -> None:
    prompt = (Path(__file__).parents[1] / "prompts" / "extract.md").read_text(encoding="utf-8")

    assert "standing preference" in prompt
    assert "one-off request" in prompt
    assert "slayttaki terimleri Türkçeleştirme" in prompt
    assert "bunu da detaylı anlat" in prompt
