from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SessionNotFoundError, StaleArtifactError
from .models import Model, ProfileState, Session


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    def session_path(self, sid: str) -> Path:
        return self.sessions_dir / f"{sid}.json"

    def run_dir(self, sid: str) -> Path:
        return self.root / "runs" / sid

    def observations_path(self, sid: str) -> Path:
        return self.run_dir(sid) / "observations.json"

    def verify_path(self, sid: str) -> Path:
        return self.run_dir(sid) / "verify.json"

    def review_path(self, sid: str) -> Path:
        return self.run_dir(sid) / "review.md"

    def proposed_decisions_path(self, sid: str) -> Path:
        return self.run_dir(sid) / "decisions.proposed.json"

    def decisions_path(self, sid: str) -> Path:
        return self.run_dir(sid) / "decisions.json"

    @property
    def profile_path(self) -> Path:
        return self.root / "state" / "profile.json"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def courses_path(self) -> Path:
        return self.root / "courses.md"

    @property
    def config_path(self) -> Path:
        return self.root / "tutormem.toml"

    @classmethod
    def resolve(cls, cli_value: str | None) -> Workspace:
        value = cli_value or os.environ.get("TUTORMEM_WORKSPACE")
        if value is None:
            value = "~/.local/share/tutor-memory/default"
        return cls(Path(value).expanduser())


def write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_json(path: Path, obj: Any) -> None:
    """Atomically write contract-formatted JSON."""
    payload = obj.to_dict() if isinstance(obj, Model) else obj
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_session(ws: Workspace, session: Session) -> None:
    write_json(ws.session_path(session.session_id), session)


def load_session(ws: Workspace, sid: str) -> Session:
    path = ws.session_path(sid)
    if not path.exists():
        raise SessionNotFoundError(f"session not found: {sid}")
    return Session.from_dict(read_json(path))


def list_sessions(ws: Workspace) -> list[Session]:
    if not ws.sessions_dir.exists():
        return []
    sessions = [Session.from_dict(read_json(path)) for path in ws.sessions_dir.glob("*.json")]
    return sorted(sessions, key=lambda session: session.index)


def load_artifact[T: Model](path: Path, cls: type[T], session: Session) -> T | None:
    if not path.exists():
        return None
    artifact = cls.from_dict(read_json(path))
    if (
        getattr(artifact, "session_id", None) != session.session_id
        or getattr(artifact, "content_sha256", None) != session.content_sha256
    ):
        raise StaleArtifactError(f"stale artifact: {path}")
    return artifact


def write_profile(ws: Workspace, state: ProfileState) -> None:
    write_json(ws.profile_path, state)
