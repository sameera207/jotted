"""Load and validate config.toml into frozen dataclasses.

Every setting lives in one file. Unknown sections or keys are an error, so a
typo fails fast instead of silently falling back to a default.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

ENV_VAR = "JOTTED_CONFIG"
HOME_VAR = "JOTTED_HOME"
OLD_NAME = "rmtasks"  # the project's name before Jotted
DEFAULT_PATH = Path("config.toml")  # in the current folder: a development checkout
EXAMPLE = Path(__file__).with_name("config.example.toml")  # every setting, with its default


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class PathsConfig:
    cache_dir: Path = Path("./cache")
    secrets_dir: Path = Path("./.secrets")


@dataclass(frozen=True)
class RmapiConfig:
    binary: str = "rmapi"
    token_file: Path = Path("./.secrets/rmapi.conf")
    timeout_s: int = 120
    trace: bool = False


@dataclass(frozen=True)
class StrokesConfig:
    ignore_tools: list[str] = field(default_factory=lambda: ["highlighter", "eraser", "eraser_area"])
    min_points: int = 2


@dataclass(frozen=True)
class LinesConfig:
    band_tolerance: float = 0.6
    min_vertical_overlap: float = 0.5
    tall_stroke_factor: float = 3.0
    merge_containment: float = 0.6
    same_row_overlap: float = 0.35


@dataclass(frozen=True)
class RecognitionConfig:
    enabled: bool = True
    provider: str = "anthropic"  # which HandwritingReader adapter reads the lines
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    effort: str = "low"  # anthropic only
    timeout_s: int = 120


@dataclass(frozen=True)
class ClassificationConfig:
    enabled: bool = True
    provider: str = "typesafe"  # which LineJudge adapter judges the lines
    model: str = "jev-latest"
    api_key_env: str = "TYPESAFE_API_KEY"
    continuation_threshold: float = 0.25
    continuation_spacing: float = 0.75
    context_lines: int = 2
    timeout_s: int = 30


@dataclass(frozen=True)
class TemplateConfig:
    scale: float = 1.0525


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db: Path = Path("./data/jotted.db")
    auto_push: bool = True
    auto_push_delay_s: int = 5


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    source: Path
    paths: PathsConfig
    rmapi: RmapiConfig
    strokes: StrokesConfig
    lines: LinesConfig
    recognition: RecognitionConfig
    classification: ClassificationConfig
    template: TemplateConfig
    server: ServerConfig
    logging: LoggingConfig

    def as_dict(self) -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Path):
                return str(v)
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        return {
            f.name: {k: conv(v) for k, v in dataclasses.asdict(getattr(self, f.name)).items()}
            for f in fields(self)
            if f.name != "source"
        }


SECTIONS: dict[str, type] = {
    "paths": PathsConfig,
    "rmapi": RmapiConfig,
    "strokes": StrokesConfig,
    "lines": LinesConfig,
    "recognition": RecognitionConfig,
    "classification": ClassificationConfig,
    "template": TemplateConfig,
    "server": ServerConfig,
    "logging": LoggingConfig,
}

RECOGNITION_PROVIDERS = {"anthropic"}  # kept in step with recognise.PROVIDERS
CLASSIFICATION_PROVIDERS = {"typesafe"}  # kept in step with classify.PROVIDERS

# Settings of the retired Tasks notebook: still accepted in old config files, and ignored.
RETIRED_SECTIONS = {"notebook", "checkbox", "output"}
RETIRED_KEYS = {
    "paths": {"output_dir"},
    "classification": {"todo_threshold", "checkbox_is_task"},
    "template": {"pages", "header_height", "footer_height", "line_spacing", "strike_width"},
}


def _platform_home(name: str) -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / name
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / name


def app_home() -> Path:
    """Where an installed Jotted keeps its config, data and secrets. JOTTED_HOME overrides.

    A folder left by the project's earlier name (rmtasks) is moved here the first time."""
    if os.environ.get(HOME_VAR):
        return Path(os.environ[HOME_VAR]).expanduser()
    home, old = _platform_home("jotted"), _platform_home(OLD_NAME)
    if not home.exists() and old.is_dir():
        old.rename(home)
    return home


def resolve_path() -> Path:
    """JOTTED_CONFIG; else config.toml in the current folder (a checkout); else the app home's."""
    if os.environ.get(ENV_VAR):
        return Path(os.environ[ENV_VAR])
    if DEFAULT_PATH.is_file():
        return DEFAULT_PATH
    return app_home() / "config.toml"


def create(path: Path) -> Path:
    """A new config file at `path` with every default; its relative paths resolve beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE.read_text())
    return path


def set_value(path: Path, section: str, key: str, value: str) -> None:
    """Set a string `key` in `[section]` of a config file, keeping its comments and layout."""
    lines = path.read_text().splitlines(keepends=True)
    current, insert_at = None, None
    rendered = f"{key} = {json.dumps(value)}\n"
    for i, line in enumerate(lines):
        head = re.match(r"\s*\[([^\]]+)\]", line)
        if head:
            if current == section:
                break
            current = head.group(1).strip()
            if current == section:
                insert_at = i + 1
            continue
        if current == section:
            m = re.match(rf"(\s*{re.escape(key)}\s*=\s*)(\"[^\"]*\"|'[^']*'|[^#\s]+)(.*)", line)
            if m:
                lines[i] = m.group(1) + json.dumps(value) + m.group(3).rstrip("\n") + "\n"
                path.write_text("".join(lines))
                return
            insert_at = i + 1 if line.strip() else insert_at
    if insert_at is None:
        lines.append(f"\n[{section}]\n")
        insert_at = len(lines)
    lines.insert(insert_at, rendered)
    path.write_text("".join(lines))


def load(path: Path | None = None) -> Config:
    path = (path or resolve_path()).expanduser()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}. Run `jotted start` to set up")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    unknown = set(raw) - set(SECTIONS) - RETIRED_SECTIONS
    if unknown:
        raise ConfigError(f"{path}: unknown section(s): {', '.join(sorted(unknown))}")

    base = path.resolve().parent
    sections = {name: _section(name, cls, raw.get(name, {}), base) for name, cls in SECTIONS.items()}
    cfg = Config(source=path.resolve(), **sections)
    _validate(cfg)
    return cfg


def _section(name: str, cls: type, values: dict[str, Any], base: Path) -> Any:
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] must be a table")
    hints = get_type_hints(cls)
    values = {k: v for k, v in values.items() if k not in RETIRED_KEYS.get(name, set())}
    unknown = set(values) - set(hints)
    if unknown:
        raise ConfigError(f"[{name}] unknown key(s): {', '.join(sorted(unknown))}")

    kwargs = {}
    for key, value in values.items():
        hint = hints[key]
        where = f"{name}.{key}"
        if hint is Path:
            if not isinstance(value, str):
                raise ConfigError(f"{where} must be a string path")
            p = Path(value).expanduser()
            kwargs[key] = p if p.is_absolute() else (base / p).resolve()
        elif hint is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{where} must be true or false")
            kwargs[key] = value
        elif hint is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{where} must be an integer")
            kwargs[key] = value
        elif hint is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{where} must be a number")
            kwargs[key] = float(value)
        elif hint is str:
            if not isinstance(value, str):
                raise ConfigError(f"{where} must be a string")
            kwargs[key] = value
        elif hint == list[str]:
            if not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                raise ConfigError(f"{where} must be a list of strings")
            kwargs[key] = list(value)
        else:
            raise ConfigError(f"{where}: unsupported setting type")
    return cls(**kwargs)


def _validate(cfg: Config) -> None:
    if cfg.logging.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING or ERROR")
    if cfg.recognition.provider not in RECOGNITION_PROVIDERS:
        raise ConfigError(f"recognition.provider: unknown {cfg.recognition.provider!r}; "
                          f"known: {sorted(RECOGNITION_PROVIDERS)}")
    if cfg.recognition.provider == "anthropic" and cfg.recognition.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("recognition.effort must be low, medium, high, xhigh or max")
    if cfg.classification.provider not in CLASSIFICATION_PROVIDERS:
        raise ConfigError(f"classification.provider: unknown {cfg.classification.provider!r}; "
                          f"known: {sorted(CLASSIFICATION_PROVIDERS)}")
    if not 0 <= cfg.classification.continuation_threshold <= 1:
        raise ConfigError("classification.continuation_threshold must be between 0 and 1")
    if cfg.classification.context_lines < 0:
        raise ConfigError("classification.context_lines must be 0 or more")
    if cfg.classification.enabled and not cfg.recognition.enabled:
        raise ConfigError("classification needs recognition: the judge reads the transcribed text")
    if not 0.5 < cfg.template.scale < 2:
        raise ConfigError("template.scale must be between 0.5 and 2")
    if cfg.rmapi.timeout_s <= 0:
        raise ConfigError("rmapi.timeout_s must be positive")
