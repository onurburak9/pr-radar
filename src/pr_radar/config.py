"""Load config.toml into a typed Config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.toml"


@dataclass(frozen=True)
class Config:
    github_login: str
    repos: tuple[str, ...]
    poll_interval_seconds: int = 120
    port: int = 7777
    stale_days: int = 14
    merged_visible_hours: int = 24
    assigned_ready_authors: tuple[str, ...] = ()  # empty = any author
    bot_logins_ignored_for_comments: tuple[str, ...] = ()
    ignore_checks: tuple[str, ...] = ()
    include_team_review_requests: bool = False
    db_path: Path = field(default=PROJECT_ROOT / "pr-radar.sqlite")


class ConfigError(ValueError):
    pass


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        raise ConfigError(f"{path} not found; copy {EXAMPLE_CONFIG_PATH.name} to it and edit")
    raw = tomllib.loads(path.read_text())
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown config keys in {path}: {sorted(unknown)}")
    if not raw.get("github_login"):
        raise ConfigError("github_login is required")
    repos = raw.get("repos") or []
    if not repos or any(r.count("/") != 1 for r in repos):
        raise ConfigError("repos must be a non-empty list of 'owner/name'")
    if raw.get("poll_interval_seconds", 120) < 30:
        raise ConfigError("poll_interval_seconds must be >= 30")

    kwargs = dict(raw)
    for key in ("repos", "assigned_ready_authors", "bot_logins_ignored_for_comments", "ignore_checks"):
        if key in kwargs:
            kwargs[key] = tuple(kwargs[key])
    if "db_path" in kwargs:
        kwargs["db_path"] = Path(kwargs["db_path"]).expanduser()
    return Config(**kwargs)
