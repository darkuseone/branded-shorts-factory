"""Provider contract.

Every stock source implements `search()` and is allowed to fail: the aggregator
treats a raising provider as an empty result and records the reason. That keeps
one flaky API from taking down a whole run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ...config import Settings
from ...http import HttpClient
from ...logging_utils import get_logger
from ..candidates import Candidate, MediaType

log = get_logger("search")


class StockProvider(ABC):
    """A searchable source of free (or free-tier) media."""

    name: str = "provider"
    media_types: tuple[MediaType, ...] = ("video", "image")
    requires_key: bool = False
    #: Minimum seconds between requests, to stay inside published rate limits.
    min_interval: float = 0.0

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = HttpClient(
            provider=self.name,
            timeout=settings.http_timeout,
            min_interval=self.min_interval,
            default_headers=self.default_headers(),
        )

    # -- capability ---------------------------------------------------------

    def default_headers(self) -> dict[str, str]:
        return {}

    def is_available(self) -> bool:
        """True when this provider can be queried right now."""
        if self.settings.offline:
            return False
        return not self.requires_key or bool(self.api_key)

    @property
    def api_key(self) -> str | None:
        return None

    def unavailable_reason(self) -> str:
        if self.settings.offline:
            return "offline mode"
        if self.requires_key and not self.api_key:
            return "missing API key"
        return ""

    def supports(self, media_type: MediaType) -> bool:
        return media_type in self.media_types

    # -- search -------------------------------------------------------------

    @abstractmethod
    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        """Return candidates for one query. May raise; the caller handles it."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name}>"


#: The output is 1080x1920. Nothing above the 1080p class is ever worth
#: fetching: a 4K or 8K master is hundreds of megabytes downloaded, probed,
#: decoded per frame and then thrown away in the downscale. The cap is on the
#: SHORT edge, which is the honest way to say "1080p" for both orientations —
#: it admits 1920x1080 landscape and 1080x1920 vertical, and excludes 2160
#: and 4320 in either.
MAX_SHORT_EDGE = 1080

#: What a 9:16 frame needs. A landscape source cropped to 9:16 comes out
#: narrower than this and has to be lifted; see `needs_upscale`.
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


def short_edge(variant: dict, *, width_key: str = "width", height_key: str = "height") -> int:
    width = int(variant.get(width_key) or 0)
    height = int(variant.get(height_key) or 0)
    if not width or not height:
        return max(width, height)
    return min(width, height)


def needs_upscale(width: int, height: int) -> bool:
    """Whether a 9:16 crop of this source lands below the output resolution.

    A 1920x1080 clip cropped to 9:16 is 607x1080 — sharp, but too small, so it
    gets blown up in the composition. Lifting it deliberately with an upscaler
    beats letting the browser stretch it.
    """
    if not width or not height:
        return False
    if height <= 0:
        return False
    # Crop to 9:16 keeps the full height and takes 9/16 of it in width.
    cropped_width = min(width, height * OUTPUT_WIDTH / OUTPUT_HEIGHT)
    return cropped_width < OUTPUT_WIDTH - 1


def pick_best_fit(
    variants: Sequence[dict],
    *,
    width_key: str = "width",
    height_key: str = "height",
    prefer_vertical: bool = True,
    max_short_edge: int = MAX_SHORT_EDGE,
) -> dict | None:
    """Choose the best variant at or below the 1080p class.

    Vertical wins outright — a portrait source needs no crop, so all 1080 of
    its width survive. Otherwise take the largest variant that still respects
    the cap; only if every variant breaks the cap does the smallest of those
    win, since something has to be returned.
    """
    usable = [v for v in variants if isinstance(v, dict) and v.get(height_key)]
    if not usable:
        return None

    def is_vertical(variant: dict) -> bool:
        width = int(variant.get(width_key) or 0)
        height = int(variant.get(height_key) or 0)
        return bool(prefer_vertical and height and width and height > width)

    def rank(variant: dict) -> tuple[int, int, int]:
        edge = short_edge(variant, width_key=width_key, height_key=height_key)
        within = 1 if edge <= max_short_edge else 0
        # Inside the cap take the biggest; outside it take the smallest.
        size_key = edge if within else -edge
        return (int(is_vertical(variant)) if within else 0, within, size_key)

    return max(usable, key=rank)


#: Kept so existing callers and tests keep working.
pick_largest = pick_best_fit
