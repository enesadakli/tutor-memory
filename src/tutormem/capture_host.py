from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

from .config import Config, store_resolved_tools
from .errors import CaptureHostError
from .storage import Workspace, write_json, write_text

HOST_NAME = "com.tutormem.capture"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_CHAT_ID = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_SIDECAR_FIELDS = {
    "source",
    "gem_id",
    "chat_id",
    "title",
    "url",
    "turns",
    "updated_at",
    "transcript_sha256",
}


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ValueError("unexpected EOF in native message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> dict[str, Any]:
    """Read one Chrome native-messaging frame."""
    header = stream.read(4)
    if not header:
        raise EOFError
    if len(header) != 4:
        raise ValueError("incomplete native message header")
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        raise ValueError("native message exceeds 8 MiB")
    try:
        message = json.loads(_read_exact(stream, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid native message JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("native message must be a JSON object")
    return message


def write_message(stream: BinaryIO, obj: Any) -> None:
    """Write one Chrome native-messaging frame."""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("native message exceeds 8 MiB")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def handle(message: dict[str, Any], inbox: Path) -> dict[str, Any]:
    """Validate and persist one capture request without propagating failures."""
    try:
        if message.get("type") != "save":
            raise ValueError("message type must be 'save'")
        chat_id = message.get("chatId")
        if not isinstance(chat_id, str) or len(chat_id) > 64 or _CHAT_ID.fullmatch(chat_id) is None:
            raise ValueError("chatId must be 1-64 hexadecimal characters")
        transcript = message.get("transcript")
        if not isinstance(transcript, str) or not transcript:
            raise ValueError("transcript must be a non-empty string")
        encoded = transcript.encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError("transcript exceeds 8 MiB")
        try:
            turns_payload = json.loads(transcript)
        except json.JSONDecodeError as exc:
            raise ValueError(f"transcript must be valid turns JSON: {exc}") from exc
        if (
            not isinstance(turns_payload, dict)
            or turns_payload.get("format") != "tutor-memory-turns/1"
            or not isinstance(turns_payload.get("turns"), list)
        ):
            raise ValueError("transcript must use tutor-memory-turns/1")
        sidecar = message.get("sidecar")
        if not isinstance(sidecar, dict):
            raise ValueError("sidecar must be an object")
        unknown = set(sidecar) - _SIDECAR_FIELDS
        if unknown:
            raise ValueError(f"sidecar has unknown fields: {', '.join(sorted(unknown))}")
        required_types: dict[str, type[Any]] = {
            "source": str,
            "gem_id": str,
            "chat_id": str,
            "title": str,
            "url": str,
            "turns": int,
            "updated_at": str,
            "transcript_sha256": str,
        }
        if set(sidecar) != set(required_types):
            raise ValueError("sidecar is missing required fields")
        if any(type(sidecar[key]) is not expected for key, expected in required_types.items()):
            raise ValueError("sidecar field has invalid type")
        if sidecar["chat_id"] != chat_id:
            raise ValueError("sidecar chat_id must match chatId")
        string_limits = {
            "source": 32,
            "gem_id": 64,
            "chat_id": 64,
            "title": 200,
            "url": 2048,
            "updated_at": 64,
            "transcript_sha256": 64,
        }
        if any(len(sidecar[key]) > limit for key, limit in string_limits.items()):
            raise ValueError("sidecar string field exceeds its maximum length")
        if sidecar["turns"] < 1 or sidecar["turns"] > 100_000:
            raise ValueError("sidecar turns must be between 1 and 100000")
        digest = hashlib.sha256(encoded).hexdigest()
        if sidecar["transcript_sha256"] != digest:
            raise ValueError("sidecar transcript_sha256 does not match transcript")

        inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        base = inbox / f"gemini-{chat_id}"
        write_text(inbox / f"{base.name}.turns.json", transcript)
        committed_sidecar = dict(sidecar)
        committed_sidecar["transcript_sha256"] = digest
        write_json(base.with_suffix(".json"), committed_sidecar)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _append_log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(message.rstrip() + "\n")
    except OSError:
        pass


def main(
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    workspace: Workspace | None = None,
) -> None:
    """Run the native host until Chrome closes stdin."""
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    ws = workspace or Workspace.resolve(None)
    config = Config.load(ws)
    inbox = Path(config.auto.inbox).expanduser()
    log_path = ws.root / "capture-host.log"

    while True:
        try:
            message = read_message(input_stream)
        except EOFError:
            return
        except Exception as exc:
            _append_log(log_path, f"framing error: {exc}")
            write_message(output_stream, {"ok": False, "error": str(exc)})
            return

        response = handle(message, inbox)
        if not response["ok"]:
            _append_log(log_path, str(response["error"]))
        write_message(output_stream, response)


def _manifest_path(home: Path | None, manifest_dir: Path | None) -> Path:
    if manifest_dir is not None:
        directory = manifest_dir.expanduser()
    else:
        user_home = (home or Path.home()).expanduser()
        directory = (
            user_home
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "NativeMessagingHosts"
        )
    return directory / f"{HOST_NAME}.json"


def install_capture_host(
    ws: Workspace,
    extension_id: str,
    *,
    browser: str = "chrome",
    home: Path | None = None,
    manifest_dir: Path | None = None,
    executable: str | None = None,
) -> tuple[Path, Path]:
    """Install the native host wrapper and Chrome manifest."""
    if browser != "chrome":
        raise CaptureHostError(f"unsupported browser: {browser}")
    if _EXTENSION_ID.fullmatch(extension_id) is None:
        raise CaptureHostError("extension id must be 32 characters in the range a-p")

    root = ws.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store_resolved_tools(Workspace(root))
    wrapper = root / "capture-host.sh"
    resolved_executable = executable or shutil.which("tutormem") or sys.argv[0]
    command = str(Path(resolved_executable).expanduser().resolve())
    wrapper_text = (
        "#!/bin/sh\n"
        f"export TUTORMEM_WORKSPACE={shlex.quote(str(root))}\n"
        f'exec {shlex.quote(command)} capture-host "$@"\n'
    )
    write_text(wrapper, wrapper_text)
    wrapper.chmod(0o755)

    manifest = _manifest_path(home, manifest_dir)
    write_json(
        manifest,
        {
            "name": HOST_NAME,
            "description": "tutor-memory capture inbox writer",
            "path": str(wrapper),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{extension_id}/"],
        },
    )
    print(wrapper)
    print(manifest)
    return wrapper, manifest


def uninstall_capture_host(
    ws: Workspace,
    *,
    home: Path | None = None,
    manifest_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Remove the native host wrapper and Chrome manifest if present."""
    wrapper = ws.root.expanduser().resolve() / "capture-host.sh"
    manifest = _manifest_path(home, manifest_dir)
    for path in (wrapper, manifest):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return wrapper, manifest
