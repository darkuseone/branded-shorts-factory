"""Runtime configuration: credentials, budgets and filesystem layout.

Everything is resolved from environment variables so the same code runs locally
and in GitHub Actions. Missing credentials are never fatal at import time — the
components that need them report a precise, actionable message instead.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Order matters: cheaper/freer sources are consulted first, and ties in the
# ranker are broken in favour of whatever appears earlier in this list.
FREE_STOCK_PRIORITY = (
    "pexels",
    "pixabay",
    "mixkit",
    "nasa",
    "wikimedia",
    "internet_archive",
    "pond5_free",
)

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30


def _env(mapping: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = mapping.get(name)
        if value:
            return value.strip()
    return None


def _env_float(mapping: Mapping[str, str], name: str, default: float) -> float:
    raw = mapping.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(mapping: Mapping[str, str], name: str, default: int) -> int:
    return int(_env_float(mapping, name, default))


def _env_bool(mapping: Mapping[str, str], name: str, default: bool) -> bool:
    raw = mapping.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Credentials:
    """API keys. Any of them may be ``None``; consumers check what they need."""

    heygen: str | None = None
    elevenlabs: str | None = None
    magnific: str | None = None
    grok: str | None = None
    pexels: str | None = None
    pixabay: str | None = None
    pond5: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Credentials:
        env = environ if environ is not None else os.environ
        return cls(
            heygen=_env(env, "HEYGEN_API_KEY"),
            elevenlabs=_env(env, "ELEVENLABS_API_KEY", "ELEVEN_API_KEY"),
            magnific=_env(env, "MAGNIFIC_API_KEY"),
            grok=_env(env, "GROK_API_KEY", "XAI_API_KEY"),
            pexels=_env(env, "PEXELS_API_KEY"),
            pixabay=_env(env, "PIXABAY_API_KEY"),
            pond5=_env(env, "POND5_API_KEY"),
        )

    def available_stock_sources(self) -> tuple[str, ...]:
        """Free sources that can actually be queried with the current keys."""
        keyed = {"pexels": self.pexels, "pixabay": self.pixabay, "pond5_free": self.pond5}
        return tuple(name for name in FREE_STOCK_PRIORITY if keyed.get(name, "keyless"))


@dataclass(frozen=True)
class Budgets:
    """Guard rails for paid generation.

    ``magnific_tokens`` is the hard ceiling for one run. ``magnific_reserve``
    is kept aside for high-priority visuals so an early, greedy escalation can
    never starve the hook or the CTA.
    """

    magnific_tokens: int = 40
    magnific_reserve: int = 10
    magnific_max_per_visual: int = 2
    grok_vision_calls: int = 60
    escalate_below_score: float = 0.62
    accept_above_score: float = 0.80
    #: Wall clock for one run (§19). Past it the run stops reaching for slow
    #: optional tiers and finishes with what it has, rather than being killed
    #: from outside with nothing to show. 0 disables the guard.
    wallclock_minutes: int = 35


@dataclass(frozen=True)
class Paths:
    """Filesystem layout for one run."""

    root: Path
    workdir: Path

    @property
    def downloads(self) -> Path:
        return self.workdir / "downloads"

    @property
    def generated(self) -> Path:
        return self.workdir / "generated"

    @property
    def voice(self) -> Path:
        return self.workdir / "voice"

    @property
    def avatar(self) -> Path:
        return self.workdir / "avatar"

    @property
    def keyframes(self) -> Path:
        return self.workdir / "keyframes"

    @property
    def cards(self) -> Path:
        """Infographics we render ourselves, rather than source (§13)."""
        return self.workdir / "cards"

    @property
    def composition(self) -> Path:
        return self.workdir / "composition"

    @property
    def output(self) -> Path:
        return self.workdir / "output"

    @property
    def reports(self) -> Path:
        return self.workdir / "reports"

    @property
    def music_library(self) -> Path:
        return self.root / "assets" / "music"

    @property
    def sfx_library(self) -> Path:
        return self.root / "assets" / "sfx"

    @property
    def meme_library(self) -> Path:
        return self.root / "assets" / "memes"

    @property
    def brand_dir(self) -> Path:
        return self.root / "brand"

    def ensure(self) -> Paths:
        for directory in (
            self.workdir,
            self.downloads,
            self.generated,
            self.voice,
            self.avatar,
            self.keyframes,
            self.composition,
            self.output,
            self.reports,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class Settings:
    """Everything the pipeline needs to know about its environment."""

    credentials: Credentials = field(default_factory=Credentials)
    budgets: Budgets = field(default_factory=Budgets)
    paths: Paths = field(default_factory=lambda: Paths(Path.cwd(), Path.cwd() / "build"))
    offline: bool = False
    dry_run: bool = False
    render_with_docker: bool = True
    hyperframes_cmd: tuple[str, ...] = ("npx", "--yes", "hyperframes")
    ffmpeg_cmd: str = "ffmpeg"
    ffprobe_cmd: str = "ffprobe"
    http_timeout: float = 30.0
    max_parallel_searches: int = 8
    grok_vision_model: str = "grok-4-vision"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        root: Path | None = None,
        workdir: Path | None = None,
    ) -> Settings:
        env = environ if environ is not None else os.environ
        project_root = root or Path(__file__).resolve().parents[2]
        work = workdir or project_root / "build"
        return cls(
            credentials=Credentials.from_env(env),
            budgets=Budgets(
                magnific_tokens=_env_int(env, "MAGNIFIC_TOKEN_BUDGET", 40),
                magnific_reserve=_env_int(env, "MAGNIFIC_TOKEN_RESERVE", 10),
                magnific_max_per_visual=_env_int(env, "MAGNIFIC_MAX_PER_VISUAL", 2),
                grok_vision_calls=_env_int(env, "GROK_VISION_CALL_BUDGET", 60),
                escalate_below_score=_env_float(env, "ESCALATE_BELOW_SCORE", 0.62),
                accept_above_score=_env_float(env, "ACCEPT_ABOVE_SCORE", 0.80),
                wallclock_minutes=_env_int(env, "RUN_WALLCLOCK_MINUTES", 35),
            ),
            paths=Paths(project_root, work),
            offline=_env_bool(env, "SHORTS_OFFLINE", False),
            dry_run=_env_bool(env, "SHORTS_DRY_RUN", False),
            render_with_docker=_env_bool(env, "HYPERFRAMES_DOCKER", True),
            ffmpeg_cmd=_env(env, "FFMPEG_BIN") or "ffmpeg",
            ffprobe_cmd=_env(env, "FFPROBE_BIN") or "ffprobe",
            http_timeout=_env_float(env, "HTTP_TIMEOUT", 30.0),
            max_parallel_searches=_env_int(env, "MAX_PARALLEL_SEARCHES", 8),
            grok_vision_model=_env(env, "GROK_VISION_MODEL") or "grok-4-vision",
        )
