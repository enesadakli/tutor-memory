from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest

from tutormem.capture_host import (
    MAX_MESSAGE_BYTES,
    handle,
    install_capture_host,
    main,
    read_message,
    write_message,
)
from tutormem.errors import CaptureHostError
from tutormem.storage import Workspace


def _save_message(chat_id: str = "a1") -> dict[str, object]:
    return {
        "type": "save",
        "chatId": chat_id,
        "transcript": "### learner\nMerhaba.\n",
        "sidecar": {"title": "Türkçe", "chat_id": chat_id},
    }


def test_framing_round_trip() -> None:
    stream = io.BytesIO()
    message = {"ok": True, "text": "İzmir"}

    write_message(stream, message)
    stream.seek(0)

    assert read_message(stream) == message


def test_read_message_rejects_oversize() -> None:
    stream = io.BytesIO(struct.pack("<I", MAX_MESSAGE_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        read_message(stream)


def test_handle_writes_transcript_before_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tutormem.capture_host as capture_host

    writes: list[str] = []
    real_write_text = capture_host.write_text
    real_write_json = capture_host.write_json

    def tracked_text(path: Path, text: str) -> None:
        writes.append(path.suffix)
        real_write_text(path, text)

    def tracked_json(path: Path, obj: object) -> None:
        writes.append(path.suffix)
        real_write_json(path, obj)

    monkeypatch.setattr(capture_host, "write_text", tracked_text)
    monkeypatch.setattr(capture_host, "write_json", tracked_json)

    inbox = tmp_path / "inbox"
    response = handle(_save_message("Ab12"), inbox)

    assert response == {"ok": True}
    assert writes == [".md", ".json"]
    assert (inbox / "gemini-Ab12.md").read_text(encoding="utf-8") == "### learner\nMerhaba.\n"
    assert json.loads((inbox / "gemini-Ab12.json").read_text(encoding="utf-8")) == {
        "title": "Türkçe",
        "chat_id": "Ab12",
    }
    assert (inbox / "gemini-Ab12.json").read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"chatId": "not-hex"}, "chatId"),
        ({"transcript": ""}, "transcript"),
        ({"type": "other"}, "type"),
    ],
)
def test_handle_rejects_invalid_messages_without_files(
    tmp_path: Path, changes: dict[str, object], error: str
) -> None:
    message = _save_message()
    message.update(changes)

    response = handle(message, tmp_path / "inbox")

    assert response["ok"] is False
    assert error in str(response["error"])
    assert not list(tmp_path.rglob("gemini-*"))


def test_main_processes_two_framed_messages(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "ws")
    inbox = tmp_path / "capture inbox"
    ws.root.mkdir(parents=True)
    ws.config_path.write_text(f'[auto]\ninbox = "{inbox}"\n', encoding="utf-8")
    input_stream = io.BytesIO()
    write_message(input_stream, _save_message("aa"))
    write_message(input_stream, _save_message("bb"))
    input_stream.seek(0)
    output_stream = io.BytesIO()

    main(stdin=input_stream, stdout=output_stream, workspace=ws)

    output_stream.seek(0)
    assert read_message(output_stream) == {"ok": True}
    assert read_message(output_stream) == {"ok": True}
    with pytest.raises(EOFError):
        read_message(output_stream)
    assert (inbox / "gemini-aa.md").exists()
    assert (inbox / "gemini-bb.json").exists()


def test_install_writes_manifest_and_executable_wrapper(tmp_path: Path) -> None:
    ws = Workspace(tmp_path / "workspace with spaces")
    home = tmp_path / "home"
    executable = tmp_path / "bin" / "tutormem"
    extension_id = "abcdefghijklmnopabcdefghijklmnop"

    wrapper, manifest = install_capture_host(
        ws,
        extension_id,
        home=home,
        executable=str(executable),
    )

    assert wrapper.stat().st_mode & 0o777 == 0o755
    assert wrapper.read_text(encoding="utf-8") == (
        "#!/bin/sh\n"
        f"export TUTORMEM_WORKSPACE='{ws.root.resolve()}'\n"
        f'exec {executable.resolve()} capture-host "$@"\n'
    )
    assert manifest == (
        home
        / "Library"
        / "Application Support"
        / "Google"
        / "Chrome"
        / "NativeMessagingHosts"
        / "com.tutormem.capture.json"
    )
    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "name": "com.tutormem.capture",
        "description": "tutor-memory capture inbox writer",
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


@pytest.mark.parametrize(
    "extension_id",
    ["short", "a" * 31, "a" * 33, "q" * 32, "A" * 32],
)
def test_install_rejects_invalid_extension_id(tmp_path: Path, extension_id: str) -> None:
    with pytest.raises(CaptureHostError, match="extension id"):
        install_capture_host(
            Workspace(tmp_path / "ws"),
            extension_id,
            manifest_dir=tmp_path / "manifests",
            executable=str(tmp_path / "tutormem"),
        )
