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


def pick_largest(
    variants: Sequence[dict],
    *,
    width_key: str = "width",
    height_key: str = "height",
    prefer_vertical: bool = True,
    max_height: int = 4320,
) -> dict | None:
    """Choose the best file variant a provider offers for one asset.

    Vertical variants win outright (they need no crop); otherwise the tallest
    file below `max_height` wins, because 9:16 crops are height-bound.
    """
    usable = [v for v in variants if isinstance(v, dict) and v.get(height_key)]
    if not usable:
        return None

    def sort_key(variant: dict) -> tuple[int, int]:
        width = int(variant.get(width_key) or 0)
        height = int(variant.get(height_key) or 0)
        vertical = 1 if (prefer_vertical and height and width and height > width) else 0
        penalty = 0 if height <= max_height else -1
        return (vertical + penalty, height)

    return max(usable, key=sort_key)
