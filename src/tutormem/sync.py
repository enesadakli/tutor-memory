from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .errors import SyncError
from .storage import Workspace, read_json, write_json, write_text

_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_MARKDOWN_MIME = "text/markdown"
_SCOPES = ("https://www.googleapis.com/auth/drive.file",)
MediaFactory = Callable[..., Any]


def _without_frontmatter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :]).lstrip("\r\n")
    return text


def _load_state(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        state = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"cannot read sync state: {exc}") from exc
    if not isinstance(state, dict) or set(state) != {"file_id"}:
        raise SyncError("invalid sync state")
    file_id = state["file_id"]
    if not isinstance(file_id, str) or not file_id:
        raise SyncError("invalid sync state")
    return file_id


def _save_token(path: Path, payload: str) -> None:
    write_text(path, payload)
    path.chmod(0o600)


def _google_resource(config_dir: Path) -> tuple[Any, MediaFactory]:
    try:
        import google.auth
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise SyncError("Google Drive sync requires the 'gdrive' extra") from exc

    credentials: Any = None
    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    try:
        if adc_path.exists():
            credentials, _ = google.auth.load_credentials_from_file(str(adc_path), scopes=_SCOPES)
        else:
            token_path = config_dir / "token.json"
            if token_path.exists():
                credentials = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
            if credentials is not None and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            if credentials is None or not credentials.valid:
                client_secret = config_dir / "client_secret.json"
                if not client_secret.exists():
                    raise SyncError(f"OAuth client secret not found: {client_secret}")
                flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), _SCOPES)
                credentials = flow.run_local_server(port=0)
            config_dir.mkdir(parents=True, exist_ok=True)
            _save_token(token_path, credentials.to_json())
        service = build("drive", "v3", credentials=credentials)
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f"cannot authenticate with Google Drive: {exc}") from exc
    return service.files(), MediaFileUpload


def _upload(
    files: Any,
    media_factory: MediaFactory,
    body: str,
    title: str,
    file_id: str | None,
) -> str:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        media = media_factory(str(temporary), mimetype=_MARKDOWN_MIME)
        if file_id is None:
            response = files.create(
                body={"name": title, "mimeType": _GOOGLE_DOC_MIME},
                media_body=media,
                fields="id",
            ).execute()
            if not isinstance(response, dict) or not isinstance(response.get("id"), str):
                raise SyncError("Google Drive create response did not contain a file id")
            return response["id"]
        files.update(fileId=file_id, media_body=media).execute()
        return file_id
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError(f"Google Drive upload failed: {exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def sync(
    ws: Workspace,
    config: Config,
    *,
    dry_run: bool = False,
    drive: Any = None,
    media_factory: MediaFactory | None = None,
) -> str:
    """Create or update the configured Google Doc with the rendered brief."""
    brief_path = ws.out_dir / "brief.md"
    if not brief_path.exists():
        raise SyncError("run 'tutormem render' first")
    try:
        body = _without_frontmatter(brief_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SyncError(f"cannot read rendered brief: {exc}") from exc
    if dry_run:
        return body

    config_dir = Path(config.sync.config_dir).expanduser()
    state_path = config_dir / "state.json"
    file_id = _load_state(state_path)
    files = drive
    factory = media_factory
    if files is None:
        files, default_factory = _google_resource(config_dir)
        factory = factory or default_factory
    elif hasattr(files, "files"):
        files = files.files()
    if factory is None:
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise SyncError("Google Drive sync requires the 'gdrive' extra") from exc
        factory = MediaFileUpload

    file_id = _upload(files, factory, body, config.sync.doc_title, file_id)
    write_json(state_path, {"file_id": file_id})
    return f"https://docs.google.com/document/d/{file_id}/edit"
