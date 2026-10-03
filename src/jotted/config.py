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
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, get_type_hints

ENV_VAR = "JOTTED_CONFIG"
HOME_VAR = "JOTTED_HOME"
OLD_NAME = "rmtasks"  # the project's name before Jotted
DEFAULT_PATH = Path("config.toml")  # in the current folder: a development checkout
EXAMPLE = Path(__file__).with_name("config.example.toml")  # every setting, with its default


class ConfigError(Exception):
    code = "config"


class ConfigMissing(ConfigError):
    """No config.toml yet: the first setup step hasn't run."""
    code = "not_set_up"
    step = "app_folder"


@dataclass(frozen=True)
class PathsConfig:
    cache_dir: Path = Path("./cache")
    secrets_dir: Path = Path("./.secrets")


@dataclass(frozen=True)
class LinesConfig:
    band_tolerance: float = 0.6
    min_vertical_overlap: float = 0.5
    tall_stroke_factor: float = 3.0
    merge_containment: float = 0.6
    same_row_overlap: float = 0.35


@dataclass(frozen=True)
class LLMConfig:
    """The language model: reads handwriting, and judges lines when Jev isn't set up."""
    provider: str = "anthropic"  # which LLM adapter (llm.PROVIDERS)
    model: str = "claude-opus-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    effort: str = "low"  # anthropic only
    timeout_s: int = 120


@dataclass(frozen=True)
class JudgingConfig:
    """Policy for judging lines, whichever model answers."""
    continuation_threshold: float = 0.25
    continuation_spacing: float = 0.75
    context_lines: int = 2


@dataclass(frozen=True)
class JevConfig:
    """Jev, TypeSafe's judging model: an optional plugin, on while its key is set."""
    model: str = "jev-latest"
    api_key_env: str = "TYPESAFE_API_KEY"
    timeout_s: int = 30


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    db: Path = Path("./data/jotted.db")
    auto_push: bool = True
    auto_push_delay_s: int = 5


@dataclass(frozen=True)
class PluginsConfig:
    source: str = "remarkable"  # where handwriting comes from: a source plugin (jotted.plugins)


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    source: Path
    paths: PathsConfig
    lines: LinesConfig
    llm: LLMConfig
    judging: JudgingConfig
    jev: JevConfig
    server: ServerConfig
    plugins: PluginsConfig
    logging: LoggingConfig
    extra: dict[str, Any] = field(default_factory=dict)  # sections owned by plugins, e.g. [rmapi]

    def section(self, name: str) -> Any:
        """A plugin's config section."""
        try:
            return self.extra[name]
        except KeyError:
            raise AttributeError(f"no config section [{name}]") from None

    def __getattr__(self, name: str) -> Any:
        # Plugin sections read like core ones (cfg.rmapi); only called for names that aren't fields.
        if name == "extra":
            raise AttributeError(name)
        return self.section(name)

    def as_dict(self) -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, Path):
                return str(v)
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        sections = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("source", "extra")}
        return {name: {k: conv(v) for k, v in dataclasses.asdict(value).items()}
                for name, value in (sections | self.extra).items()}


SECTIONS: dict[str, type] = {
    "paths": PathsConfig,
    "lines": LinesConfig,
    "llm": LLMConfig,
    "judging": JudgingConfig,
    "jev": JevConfig,
    "server": ServerConfig,
    "plugins": PluginsConfig,
    "logging": LoggingConfig,
}

LLM_PROVIDERS = {"anthropic"}  # kept in step with llm.PROVIDERS

# Settings of the retired Tasks notebook: still accepted in old config files, and ignored.
RETIRED_SECTIONS = {"notebook", "checkbox", "output"}
RETIRED_KEYS = {
    "paths": {"output_dir"},
    "template": {"pages", "header_height", "footer_height", "line_spacing", "strike_width"},
}

# Sections renamed when the LLM became a port and Jev a plugin: old config files still load.
# (old section, key) -> new section; None drops the key.
RENAMED = {
    **{("recognition", k): "llm" for k in ("provider", "model", "api_key_env", "effort", "timeout_s")},
    ("recognition", "enabled"): None,
    **{("classification", k): "judging" for k in ("continuation_threshold", "continuation_spacing", "context_lines")},
    **{("classification", k): "jev" for k in ("model", "api_key_env", "timeout_s")},
    **{("classification", k): None for k in ("enabled", "provider", "todo_threshold", "checkbox_is_task")},
}


def _upgrade(raw: dict[str, Any]) -> dict[str, Any]:
    """Move keys from renamed sections into their new ones. A key already set in the new
    section wins."""
    raw = dict(raw)
    for old in {o for o, _ in RENAMED}:
        values = raw.pop(old, None)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ConfigError(f"[{old}] must be a table")
        for key, value in values.items():
            if (old, key) not in RENAMED:
                raise ConfigError(f"[{old}] unknown key(s): {key}")
            new = RENAMED[(old, key)]
            if new is not None:
                raw.setdefault(new, {}).setdefault(key, value)
    return raw


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


def with_llm(cfg: Config, provider: str | None = None, model: str | None = None) -> Config:
    """`cfg` with the LLM chosen with `jotted ai provider`/`ai model` (saved in the database),
    which override config.toml's [llm]. A new provider brings its own key variable."""
    changes: dict[str, Any] = {}
    if provider and provider != cfg.llm.provider:
        from . import llm

        changes["provider"] = provider
        changes["api_key_env"] = getattr(llm.llm_class(replace(cfg.llm, provider=provider)), "KEY_ENV",
                                         cfg.llm.api_key_env)
    if model:
        changes["model"] = model
    return replace(cfg, llm=replace(cfg.llm, **changes)) if changes else cfg


def load(path: Path | None = None) -> Config:
    path = (path or resolve_path()).expanduser()
    if not path.is_file():
        raise ConfigMissing(f"Jotted isn't set up yet (no {path}). Run `jotted start`, or `jotted setup prepare`")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    from . import plugins  # plugins import this module for their section types

    raw = _upgrade(raw)
    owned = plugins.sections()  # section name -> (plugin class, section type), for installed plugins
    unknown = set(raw) - set(SECTIONS) - set(owned) - RETIRED_SECTIONS
    if unknown:
        raise ConfigError(f"{path}: unknown section(s): {', '.join(sorted(unknown))}")

    base = path.resolve().parent
    sections = {name: _section(name, cls, raw.get(name, {}), base) for name, cls in SECTIONS.items()}
    extra = {name: _section(name, cls, raw.get(name, {}), base) for name, (_, cls) in owned.items()}
    cfg = Config(source=path.resolve(), **sections, extra=extra)
    _validate(cfg)
    for plugin in {p for p, _ in owned.values()}:
        plugin.validate(cfg)
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


def plugins_available() -> set[str]:
    from . import plugins

    return set(plugins.available())


def _validate(cfg: Config) -> None:
    if cfg.logging.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING or ERROR")
    if cfg.llm.provider not in LLM_PROVIDERS:
        raise ConfigError(f"llm.provider: unknown {cfg.llm.provider!r}; known: {sorted(LLM_PROVIDERS)}")
    if cfg.llm.provider == "anthropic" and cfg.llm.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("llm.effort must be low, medium, high, xhigh or max")
    if not 0 <= cfg.judging.continuation_threshold <= 1:
        raise ConfigError("judging.continuation_threshold must be between 0 and 1")
    if cfg.judging.context_lines < 0:
        raise ConfigError("judging.context_lines must be 0 or more")
    if cfg.plugins.source not in plugins_available():
        raise ConfigError(f"plugins.source: no source plugin {cfg.plugins.source!r}; "
                          f"installed: {sorted(plugins_available())}")
