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


#: The Short is 1920 tall. This much source height gives room to crop and
#: reframe; past it every extra pixel is downscaled away before it is ever
#: seen.
TARGET_HEIGHT = 1920
HEADROOM = 1.35


def pick_best_fit(
    variants: Sequence[dict],
    *,
    width_key: str = "width",
    height_key: str = "height",
    prefer_vertical: bool = True,
    target_height: int = TARGET_HEIGHT,
) -> dict | None:
    """Choose the smallest variant that still covers the output resolution.

    This used to take the tallest file under 4320 — up to 8K. For a
    1080x1920 Short that is a UHD master downloaded, stored and transcoded so
    it can be thrown away in the downscale, and it dominated the run: one
    measured pass spent 65 minutes of its 115-minute budget filling slots,
    almost all of it moving pixels nobody would ever see.

    Vertical variants still win outright, since a 9:16 crop of a portrait
    source loses nothing. Among the rest, the smallest file that clears the
    target height wins; if nothing clears it, the tallest available does.
    """
    usable = [v for v in variants if isinstance(v, dict) and v.get(height_key)]
    if not usable:
        return None

    ceiling = int(target_height * HEADROOM)

    def height_of(variant: dict) -> int:
        return int(variant.get(height_key) or 0)

    def is_vertical(variant: dict) -> bool:
        width = int(variant.get(width_key) or 0)
        height = height_of(variant)
        return bool(prefer_vertical and height and width and height > width)

    def rank(variant: dict) -> tuple[int, int, int]:
        height = height_of(variant)
        # Enough resolution, and not wastefully more: the band we want.
        in_band = 1 if target_height <= height <= ceiling else 0
        # Within the band prefer the smallest; outside it prefer the tallest.
        size_key = -height if in_band else height
        return (int(is_vertical(variant)), in_band, size_key)

    return max(usable, key=rank)


#: Kept so existing callers and tests keep working.
pick_largest = pick_best_fit
