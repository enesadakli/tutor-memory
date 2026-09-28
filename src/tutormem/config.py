from __future__ import annotations

import dataclasses
import json
import re
import shutil
import tomllib
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, get_args, get_origin, get_type_hints

from .errors import SchemaError

if TYPE_CHECKING:
    from .storage import Workspace


@dataclass(frozen=True, slots=True)
class RulesConfig:
    threshold: int = 3
    stale_after: int = 5


@dataclass(frozen=True, slots=True)
class ExtractConfig:
    extractor: Literal["agy", "file"] = "agy"
    model: str = "gemini-3.8-flash-medium"
    timeout_s: int = 300
    send_base: bool = True


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    mode: Literal["packet", "claude"] = "packet"
    claude_model: str = "sonnet"
    timeout_s: int = 300


@dataclass(frozen=True, slots=True)
class SyncConfig:
    doc_title: str = "Study brief"
    config_dir: str = "~/.config/tutor-memory"


@dataclass(frozen=True, slots=True)
class AutoConfig:
    inbox: str = "~/Downloads/tutor-memory/inbox"
    idle_minutes: int = 20
    approve: Literal["claude", "none"] = "claude"
    sync: bool = True
    notify: bool = True
    default_course: str = "General"
    changelog_copy: str = ""


@dataclass(frozen=True, slots=True)
class ProgressConfig:
    enabled: bool = True
    heading: str = "## Courses and progress"
    last_label: str = "Last session"
    next_label: str = "Next"
    language: str = "English"
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ToolsConfig:
    agy: str = "agy"
    claude: str = "claude"


def _section(cls: type[Any], raw: Any, name: str) -> Any:
    if not isinstance(raw, dict):
        raise SchemaError(f"config {name}: expected table")
    allowed = {item.name for item in dataclasses.fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise SchemaError(f"config {name}: unknown keys: {', '.join(sorted(unknown))}")
    hints = get_type_hints(cls)
    for key, value in raw.items():
        expected = hints[key]
        origin = get_origin(expected)
        if origin is Literal:
            if value not in get_args(expected):
                raise SchemaError(f"config {name}.{key}: invalid value")
        elif expected is int and type(value) is not int:
            raise SchemaError(f"config {name}.{key}: expected int")
        elif expected is str and type(value) is not str:
            raise SchemaError(f"config {name}.{key}: expected str")
        elif expected is bool and type(value) is not bool:
            raise SchemaError(f"config {name}.{key}: expected bool")
        elif origin is types.UnionType and type(None) in get_args(expected):
            non_none = tuple(item for item in get_args(expected) if item is not type(None))
            if value is not None and not any(type(value) is item for item in non_none):
                raise SchemaError(f"config {name}.{key}: wrong type")
    try:
        return cls(**raw)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"config {name}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Config:
    rules: RulesConfig = field(default_factory=RulesConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    auto: AutoConfig = field(default_factory=AutoConfig)
    progress: ProgressConfig = field(default_factory=ProgressConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)

    def __post_init__(self) -> None:
        if self.progress.model is None:
            object.__setattr__(
                self,
                "progress",
                dataclasses.replace(self.progress, model=self.extract.model),
            )

    @property
    def threshold(self) -> int:
        return self.rules.threshold

    @property
    def stale_after(self) -> int:
        return self.rules.stale_after

    @classmethod
    def load(cls, ws: Workspace) -> Self:
        if not ws.config_path.exists():
            return cls()
        try:
            with ws.config_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SchemaError(f"cannot read config: {exc}") from exc
        if not isinstance(raw, dict):
            raise SchemaError("config: expected table")
        allowed = {"rules", "extract", "review", "sync", "auto", "progress", "tools"}
        unknown = set(raw) - allowed
        if unknown:
            raise SchemaError(f"config: unknown keys: {', '.join(sorted(unknown))}")
        return cls(
            rules=_section(RulesConfig, raw.get("rules", {}), "rules"),
            extract=_section(ExtractConfig, raw.get("extract", {}), "extract"),
            review=_section(ReviewConfig, raw.get("review", {}), "review"),
            sync=_section(SyncConfig, raw.get("sync", {}), "sync"),
            auto=_section(AutoConfig, raw.get("auto", {}), "auto"),
            progress=_section(ProgressConfig, raw.get("progress", {}), "progress"),
            tools=_section(ToolsConfig, raw.get("tools", {}), "tools"),
        )


def store_resolved_tools(ws: Workspace) -> ToolsConfig:
    """Resolve model CLIs once and persist their paths for scheduled runs."""
    from .storage import write_text

    def resolved(name: str) -> str:
        found = shutil.which(name)
        return str(Path(found).resolve()) if found is not None else name

    tools = ToolsConfig(resolved("agy"), resolved("claude"))
    existing = ws.config_path.read_text(encoding="utf-8") if ws.config_path.exists() else ""
    existing = re.sub(r"(?ms)^\[tools\]\n.*?(?=^\[|\Z)", "", existing).rstrip()
    section = f"[tools]\nagy = {json.dumps(tools.agy)}\nclaude = {json.dumps(tools.claude)}\n"
    write_text(ws.config_path, (existing + "\n\n" if existing else "") + section)
    return tools
