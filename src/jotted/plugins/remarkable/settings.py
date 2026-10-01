"""The reMarkable plugin's config sections: [rmapi], [strokes] and [template]."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...config import Config, ConfigError


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
class TemplateConfig:
    scale: float = 1.0525  # tablet units per nominal unit (1 pt = 3), measured on an rM2


SECTIONS = {"rmapi": RmapiConfig, "strokes": StrokesConfig, "template": TemplateConfig}


def validate(cfg: Config) -> None:
    if not 0.5 < cfg.template.scale < 2:
        raise ConfigError("template.scale must be between 0.5 and 2")
    if cfg.rmapi.timeout_s <= 0:
        raise ConfigError("rmapi.timeout_s must be positive")
