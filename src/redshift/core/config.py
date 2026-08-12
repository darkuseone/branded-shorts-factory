"""Config loading. Every number in this project lives in config/*.yaml (§0.3).

`Config` is a read-only view with dotted lookup, so code reads
`cfg.rhythm("shot_duration_s.MODE_B.max")` instead of hard-coding 2.9. A missing
key raises rather than silently defaulting — a typo in a path must not quietly
become a magic number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

CONFIG_FILES = ("brand", "rhythm", "sources", "budget", "critic", "memes", "publish")

_MISSING = object()


def project_root() -> Path:
    """Repository root, resolved from this file's location."""
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Config:
    """All config/*.yaml files, loaded once."""

    root: Path
    data: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path | None = None) -> Config:
        base = root or project_root()
        directory = base / "config"
        if not directory.is_dir():
            raise ConfigError(f"config directory not found: {directory}")

        data: dict[str, dict[str, Any]] = {}
        for name in CONFIG_FILES:
            path = directory / f"{name}.yaml"
            if not path.exists():
                raise ConfigError(f"missing config file: {path}")
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"{path} must contain a mapping at the top level")
            data[name] = loaded
        return cls(root=base, data=data)

    # -- lookup -------------------------------------------------------------

    def get(self, file: str, path: str, default: Any = _MISSING) -> Any:
        """`get("rhythm", "shot_duration_s.MODE_B.max")` → 2.9."""
        node: Any = self.data.get(file)
        if node is None:
            raise ConfigError(f"unknown config file {file!r}")
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is not _MISSING:
                    return default
                raise ConfigError(f"config/{file}.yaml has no key {path!r}")
            node = node[part]
        return node

    def brand(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("brand", path, default)

    def rhythm(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("rhythm", path, default)

    def sources(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("sources", path, default)

    def budget(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("budget", path, default)

    def critic(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("critic", path, default)

    def memes(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("memes", path, default)

    def publish(self, path: str, default: Any = _MISSING) -> Any:
        return self.get("publish", path, default)

    # -- derived paths ------------------------------------------------------

    @property
    def cache_dir(self) -> Path:
        return Path(os.environ.get("REDSHIFT_CACHE", self.root / "cache"))

    @property
    def runs_dir(self) -> Path:
        return Path(os.environ.get("REDSHIFT_RUNS", self.root / "runs"))

    @property
    def out_dir(self) -> Path:
        return Path(os.environ.get("REDSHIFT_OUT", self.root / "out"))

    @property
    def assets_dir(self) -> Path:
        return self.root / "assets"


@lru_cache(maxsize=4)
def load_config(root: str | None = None) -> Config:
    """Process-wide cached config. Pass a root only in tests."""
    return Config.load(Path(root) if root else None)


def secret(name: str) -> str | None:
    """Read an API key. Never logged, never written to an artifact (§0.8)."""
    value = os.environ.get(name)
    return value.strip() if value else None
