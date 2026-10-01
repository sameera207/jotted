"""Load and validate config.toml into frozen dataclasses.

Every setting lives in one file. Unknown sections or keys are an error, so a
typo fails fast instead of silently falling back to a default.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

ENV_VAR = "RMTASKS_CONFIG"
DEFAULT_PATH = Path("config.toml")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class PathsConfig:
    cache_dir: Path = Path("./cache")
    output_dir: Path = Path("./out")
    secrets_dir: Path = Path("./.secrets")


@dataclass(frozen=True)
class RmapiConfig:
    binary: str = "rmapi"
    token_file: Path = Path("./.secrets/rmapi.conf")
    timeout_s: int = 120
    trace: bool = False


@dataclass(frozen=True)
class NotebookConfig:
    name: str = "Tasks"
    folder: str = "/"
    pages: str | list[int] = "all"


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
class CheckboxConfig:
    styles: list[str] = field(default_factory=lambda: ["bracket_pair", "single_box"])
    lead_zone: float = 3.0
    size_min: float = 0.6
    size_max: float = 2.5
    bracket_aspect_min: float = 1.5
    bracket_height_ratio: list[float] = field(default_factory=lambda: [0.7, 1.4])
    bracket_gap: list[float] = field(default_factory=lambda: [0.3, 2.0])
    bracket_overlap_min: float = 0.7
    box_aspect: list[float] = field(default_factory=lambda: [0.6, 1.6])
    box_closure_max: float = 0.25
    box_path_ratio: list[float] = field(default_factory=lambda: [0.7, 1.5])
    required_checks: list[str] = field(default_factory=lambda: ["bracket_aspect", "gap", "closure"])
    require_text: bool = True
    min_confidence: float = 0.5


@dataclass(frozen=True)
class RecognitionConfig:
    enabled: bool = True
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    effort: str = "low"
    timeout_s: int = 120


@dataclass(frozen=True)
class ClassificationConfig:
    enabled: bool = True
    model: str = "jev-latest"
    api_key_env: str = "TYPESAFE_API_KEY"
    todo_threshold: float = 0.6
    continuation_threshold: float = 0.25
    continuation_spacing: float = 0.75
    checkbox_is_task: bool = False
    context_lines: int = 2
    timeout_s: int = 30


@dataclass(frozen=True)
class TemplateConfig:
    pages: int = 20
    header_height: float = 0.08
    footer_height: float = 0.22
    line_spacing: float = 0.045
    strike_width: float = 1.2
    scale: float = 1.0525


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db: Path = Path("./data/rmtasks.db")
    auto_push: bool = True
    auto_push_delay_s: int = 5
    auto_pull_interval_s: int = 60


@dataclass(frozen=True)
class OutputConfig:
    formats: list[str] = field(default_factory=lambda: ["table", "json", "svg"])
    svg_scale: float = 0.5


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    source: Path
    paths: PathsConfig
    rmapi: RmapiConfig
    notebook: NotebookConfig
    strokes: StrokesConfig
    lines: LinesConfig
    checkbox: CheckboxConfig
    recognition: RecognitionConfig
    classification: ClassificationConfig
    template: TemplateConfig
    server: ServerConfig
    output: OutputConfig
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
    "notebook": NotebookConfig,
    "strokes": StrokesConfig,
    "lines": LinesConfig,
    "checkbox": CheckboxConfig,
    "recognition": RecognitionConfig,
    "classification": ClassificationConfig,
    "template": TemplateConfig,
    "server": ServerConfig,
    "output": OutputConfig,
    "logging": LoggingConfig,
}

KNOWN_STYLES = {"bracket_pair", "single_box"}
KNOWN_FORMATS = {"table", "json", "svg"}
KNOWN_CHECKS = {
    "bracket_aspect", "height_ratio", "overlap", "gap", "clear_between",  # bracket_pair
    "box_aspect", "closure", "path_ratio", "clear_inside",  # single_box
    "size",  # both
}


def resolve_path() -> Path:
    return Path(os.environ.get(ENV_VAR) or DEFAULT_PATH)


def load(path: Path | None = None) -> Config:
    path = (path or resolve_path()).expanduser()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path} (copy config.example.toml to config.toml)")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    unknown = set(raw) - set(SECTIONS)
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
        elif hint == list[float]:
            if not (isinstance(value, list) and len(value) == 2 and all(isinstance(x, (int, float)) for x in value)):
                raise ConfigError(f"{where} must be a [min, max] pair of numbers")
            lo, hi = (float(x) for x in value)
            if lo > hi:
                raise ConfigError(f"{where}: min {lo} is greater than max {hi}")
            kwargs[key] = [lo, hi]
        elif hint == list[str]:
            if not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
                raise ConfigError(f"{where} must be a list of strings")
            kwargs[key] = list(value)
        else:  # notebook.pages: "all", "last" or a list of 1-based page numbers
            if value in ("all", "last"):
                kwargs[key] = value
            elif isinstance(value, list) and value and all(isinstance(x, int) and x >= 1 for x in value):
                kwargs[key] = list(value)
            else:
                raise ConfigError(f'{where} must be "all", "last", or a list of page numbers such as [1, 3]')
    return cls(**kwargs)


def _validate(cfg: Config) -> None:
    cb = cfg.checkbox
    bad = set(cb.styles) - KNOWN_STYLES
    if bad:
        raise ConfigError(f"checkbox.styles: unknown style(s) {sorted(bad)}; known: {sorted(KNOWN_STYLES)}")
    bad = set(cb.required_checks) - KNOWN_CHECKS
    if bad:
        raise ConfigError(f"checkbox.required_checks: unknown check(s) {sorted(bad)}; known: {sorted(KNOWN_CHECKS)}")
    if not 0 <= cb.min_confidence <= 1:
        raise ConfigError("checkbox.min_confidence must be between 0 and 1")
    bad = set(cfg.output.formats) - KNOWN_FORMATS
    if bad:
        raise ConfigError(f"output.formats: unknown format(s) {sorted(bad)}; known: {sorted(KNOWN_FORMATS)}")
    if cfg.logging.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING or ERROR")
    if cfg.recognition.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("recognition.effort must be low, medium, high, xhigh or max")
    if not 0 <= cfg.classification.todo_threshold <= 1:
        raise ConfigError("classification.todo_threshold must be between 0 and 1")
    if not 0 <= cfg.classification.continuation_threshold <= 1:
        raise ConfigError("classification.continuation_threshold must be between 0 and 1")
    if cfg.classification.context_lines < 0:
        raise ConfigError("classification.context_lines must be 0 or more")
    if cfg.classification.enabled and not cfg.recognition.enabled:
        raise ConfigError("classification needs recognition: Jev judges the transcribed text")
    t = cfg.template
    if t.pages < 1:
        raise ConfigError("template.pages must be at least 1")
    if not (0 < t.header_height < 1 and 0 < t.footer_height < 1 and t.header_height + t.footer_height < 0.9):
        raise ConfigError("template.header_height and footer_height must leave room for the body")
    if cfg.rmapi.timeout_s <= 0:
        raise ConfigError("rmapi.timeout_s must be positive")
