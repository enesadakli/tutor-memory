from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tutormem.config import Config, SyncConfig
from tutormem.errors import SyncError
from tutormem.storage import Workspace
from tutormem.sync import sync


class _Request:
    def __init__(self, response: dict[str, str]) -> None:
        self.response = response

    def execute(self) -> dict[str, str]:
        return self.response


class _Files:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Request:
        self.created.append(kwargs)
        return _Request({"id": "doc-123"})

    def update(self, **kwargs: Any) -> _Request:
        self.updated.append(kwargs)
        return _Request({"id": kwargs["fileId"]})


def _config(tmp_path: Path) -> Config:
    return Config(sync=SyncConfig(doc_title="Öğrenme özeti", config_dir=str(tmp_path / "cfg")))


def _brief(tmp_path: Path, text: str = "# Brief\n\nBody\n") -> Workspace:
    ws = Workspace(tmp_path / "workspace")
    ws.out_dir.mkdir(parents=True)
    (ws.out_dir / "brief.md").write_text(text, encoding="utf-8")
    return ws


def _media_factory(records: list[dict[str, Any]]):
    def factory(path: str, **kwargs: Any) -> dict[str, Any]:
        item = {
            "path": Path(path),
            "body": Path(path).read_text(encoding="utf-8"),
            **kwargs,
        }
        records.append(item)
        return item

    return factory


def test_sync_creates_then_updates_same_document_and_deletes_temp_file(tmp_path: Path) -> None:
    ws = _brief(tmp_path)
    config = _config(tmp_path)
    files = _Files()
    media: list[dict[str, Any]] = []

    first_url = sync(ws, config, drive=files, media_factory=_media_factory(media))
    second_url = sync(ws, config, drive=files, media_factory=_media_factory(media))

    assert first_url == second_url == "https://docs.google.com/document/d/doc-123/edit"
    assert files.created[0]["body"] == {
        "name": "Öğrenme özeti",
        "mimeType": "application/vnd.google-apps.document",
    }
    assert files.created[0]["fields"] == "id"
    assert files.updated[0]["fileId"] == "doc-123"
    assert all(item["body"] == "# Brief\n\nBody\n" for item in media)
    assert all(item["mimetype"] == "text/markdown" for item in media)
    assert all(not item["path"].exists() for item in media)
    state_path = Path(config.sync.config_dir) / "state.json"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"file_id": "doc-123"}


def test_sync_dry_run_returns_body_without_touching_state_or_drive(tmp_path: Path) -> None:
    ws = _brief(tmp_path, "---\ntitle: Private metadata\n---\n\n# Brief\n")
    config = _config(tmp_path)

    assert sync(ws, config, dry_run=True, drive=object()) == "# Brief\n"
    assert not Path(config.sync.config_dir).exists()


def test_sync_requires_rendered_brief(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="run 'tutormem render' first"):
        sync(Workspace(tmp_path / "workspace"), _config(tmp_path), dry_run=True)


def test_sync_state_file_has_contract_format(tmp_path: Path) -> None:
    ws = _brief(tmp_path)
    config = _config(tmp_path)
    sync(ws, config, drive=_Files(), media_factory=_media_factory([]))

    state = Path(config.sync.config_dir) / "state.json"
    assert state.read_text(encoding="utf-8") == '{\n  "file_id": "doc-123"\n}\n'
