from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
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
        allowed = {"rules", "extract", "review", "sync", "auto"}
        unknown = set(raw) - allowed
        if unknown:
            raise SchemaError(f"config: unknown keys: {', '.join(sorted(unknown))}")
        return cls(
            rules=_section(RulesConfig, raw.get("rules", {}), "rules"),
            extract=_section(ExtractConfig, raw.get("extract", {}), "extract"),
            review=_section(ReviewConfig, raw.get("review", {}), "review"),
            sync=_section(SyncConfig, raw.get("sync", {}), "sync"),
            auto=_section(AutoConfig, raw.get("auto", {}), "auto"),
        )
