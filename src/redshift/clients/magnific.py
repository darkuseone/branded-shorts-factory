"""Magnific — the premium tier, entered from the cheap end first (§9.3).

The account this project runs on carries three separable things, and confusing
them is how credits get burned for nothing:

``library``
    Stock bundled with the subscription. Downloads are free but rationed —
    100 a day on the current plan. Cards, company logos and app icons come
    from here, which is what makes them cost nothing.

``unlimited``
    Models the subscription runs unmetered. Generation is free; the only
    limits are wall-clock and rate. Reachable from Actions with just the API
    key, so a run with no credits left still has a generative tier.

``premium``
    Metered models. Every call spends credits, so it is gated by the budget
    and reserved for slots where the cheaper tiers actually failed.

Endpoints and model ids differ by account tier, so each is overridable through
the environment; the defaults match the documented v1 shape. Nothing here
raises on a provider problem — a visual that cannot be fetched degrades (§4.6)
rather than taking the run down.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..core.budget import CostRecord, cost
from ..core.errors import ProviderError
from ..core.log import get_logger
from .base import BaseClient

log = get_logger("magnific")

DEFAULT_BASE = "https://api.magnific.ai/v1"
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 420.0

#: Free stock downloads the plan allows per calendar day (UTC).
DEFAULT_DAILY_FREE_DOWNLOADS = 100


@dataclass
class LibraryAsset:
    """One free asset from the subscription library."""

    id: str
    url: str
    title: str = ""
    kind: str = "image"
    width: int = 0
    height: int = 0
    duration: float = 0.0
    tags: list[str] = field(default_factory=list)
    license: str = "Magnific subscription library"

    @property
    def suffix(self) -> str:
        tail = self.url.split("?", 1)[0].rsplit(".", 1)
        if len(tail) == 2 and 1 <= len(tail[1]) <= 5:
            return f".{tail[1].lower()}"
        return ".mp4" if self.kind == "video" else ".png"


class DailyQuota:
    """A UTC-day counter for the free download allowance.

    Kept on disk under `.state/` because it has to survive the process: a
    workflow run is a fresh clone, but the same day's allowance is shared with
    every other run that day. Committing the ledger is what makes the count
    real rather than per-run wishful thinking.
    """

    def __init__(self, path: Path, limit: int = DEFAULT_DAILY_FREE_DOWNLOADS):
        self.path = path
        self.limit = max(0, int(limit))

    def _read(self) -> tuple[str, int]:
        today = date.today().isoformat()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return today, 0
        if str(data.get("date")) != today:
            return today, 0
        return today, int(data.get("count") or 0)

    @property
    def used(self) -> int:
        return self._read()[1]

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, n: int = 1) -> bool:
        """Claim `n` downloads, returning False when the day is spent."""
        today, used = self._read()
        if used + n > self.limit:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"date": today, "count": used + n, "limit": self.limit}, indent=2) + "\n",
            encoding="utf-8",
        )
        return True


class MagnificClient(BaseClient):
    name = "magnific"
    min_interval = 0.4

    def __init__(self, *, quota: DailyQuota | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.base = os.environ.get("MAGNIFIC_API_BASE", DEFAULT_BASE).rstrip("/")
        self.key = os.environ.get("MAGNIFIC_API_KEY", "")
        self.quota = quota
        self.unlimited_image_model = os.environ.get("MAGNIFIC_UNLIMITED_IMAGE_MODEL", "flux-schnell")
        self.unlimited_video_model = os.environ.get("MAGNIFIC_UNLIMITED_VIDEO_MODEL", "")
        self.premium_image_model = os.environ.get("MAGNIFIC_IMAGE_MODEL", "flux-pro")
        self.premium_video_model = os.environ.get("MAGNIFIC_VIDEO_MODEL", "kling-v2")

    def headers(self) -> dict[str, str]:
        base = super().headers()
        if self.key:
            # §0.8: the value is never logged, only sent.
            base["Authorization"] = f"Bearer {self.key}"
        return base

    def is_available(self) -> bool:
        return bool(self.key) or self.dry_run

    def unavailable_reason(self) -> str:
        return "" if self.is_available() else "MAGNIFIC_API_KEY not set"

    # -- tier 1: the free library ------------------------------------------

    @cost("magnific_search")
    def search_library(self, query: str, *, kind: str = "image", limit: int = 8) -> list[LibraryAsset]:
        """Search subscription stock. Free, and cached, so repeats cost nothing."""
        if not self.is_available():
            return []

        params = {"query": query, "type": kind, "limit": limit}

        def produce() -> Any:
            return self.get_json(f"{self.base}/library/search", params=params)

        try:
            payload = self.cached_query(query, {"type": kind, "limit": limit}, produce)
        except ProviderError as exc:
            log.warning("library search failed: %s", exc, extra={"stage": self.stage})
            return []
        return [asset for asset in (_asset(item, kind) for item in _items(payload)) if asset]

    def download_library_asset(self, asset: LibraryAsset, destination: Path) -> Path | None:
        """Pull a library asset to disk, honouring the daily free allowance.

        Returns None when the day's quota is spent — that is a normal outcome,
        not an error: the caller falls back to something drawn.
        """
        if self.dry_run or not self.is_available():
            return None
        if self.quota is not None and not self.quota.take():
            log.info(
                "daily Magnific download allowance spent (%d) — falling back",
                self.quota.limit,
                extra={"stage": self.stage},
            )
            return None
        try:
            payload = self.request("GET", asset.url, timeout=max(self.timeout, 120.0))
        except ProviderError as exc:
            log.warning("library download failed: %s", exc, extra={"stage": self.stage})
            return None
        if len(payload) < 256:
            log.warning("library download too small (%d bytes)", len(payload), extra={"stage": self.stage})
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return destination

    # -- upscaling ----------------------------------------------------------

    def upscale(self, source_url: str, *, factor: int = 2, allow_paid: bool = False) -> str | None:
        """Lift a small asset to usable size, cheap model first.

        A 1080p landscape clip cropped to 9:16 is 607 wide against a 1080-wide
        frame, and letting the browser stretch it is the worst version of this
        — an upscaler at least knows what it is looking at. The free model is
        tried first and `allow_paid` is the only door to a metered one, so a
        run cannot quietly spend credits on a shot that was fine already.
        """
        if not self.is_available():
            return None
        free_model = os.environ.get("MAGNIFIC_UPSCALE_FREE_MODEL", "upscaler-fast")
        paid_model = os.environ.get("MAGNIFIC_UPSCALE_MODEL", "magnific-precision")

        for model, credits in ((free_model, 0.0), (paid_model, 1.0)):
            if not model:
                continue
            if credits and not allow_paid:
                break
            if credits and self.budget is not None and not self.budget.can_afford("magnific_image"):
                log.info("no budget left to upscale", extra={"stage": self.stage})
                break
            try:
                response = self.post_json(
                    f"{self.base}/upscale",
                    {"model": model, "image_url": source_url, "scale": factor},
                )
            except ProviderError as exc:
                log.warning("upscale failed on %s: %s", model, exc, extra={"stage": self.stage})
                continue
            url = self._resolve(response)
            if url:
                if credits and self.budget is not None:
                    self.budget.charge("magnific_image")
                self._last_credits = credits
                log.info("upscaled x%d via %s", factor, model, extra={"stage": self.stage})
                return url
        return None

    # -- tiers 2 & 3: generation -------------------------------------------

    def generate(
        self,
        prompt: str,
        *,
        kind: str = "image",
        aspect_ratio: str = "9:16",
        duration: float = 4.0,
        allow_premium: bool = False,
    ) -> str | None:
        """Generate a visual, unmetered model first.

        `allow_premium` is the only door to a metered model, and the caller is
        expected to have checked the budget before opening it (§19).
        """
        if not self.is_available():
            return None

        unlimited = self.unlimited_video_model if kind == "video" else self.unlimited_image_model
        if unlimited:
            url = self._submit(prompt, unlimited, kind, aspect_ratio, duration, credits=0.0)
            if url:
                return url
        if not allow_premium:
            return None
        premium = self.premium_video_model if kind == "video" else self.premium_image_model
        credits = 2.0 if kind == "video" else 1.0
        return self._submit(prompt, premium, kind, aspect_ratio, duration, credits=credits)

    def _submit(
        self,
        prompt: str,
        model: str,
        kind: str,
        aspect_ratio: str,
        duration: float,
        *,
        credits: float,
    ) -> str | None:
        # The budget kind depends on the medium, so it is resolved per call
        # rather than baked into a decorator.
        budget_kind = "magnific_video" if kind == "video" else "magnific_image"
        if self.budget is not None:
            if not self.budget.can_afford(budget_kind):
                log.info("%s budget spent — skipping %s", budget_kind, model, extra={"stage": self.stage})
                return None
            self.budget.charge(budget_kind)

        body: dict[str, Any] = {"model": model, "prompt": prompt, "aspect_ratio": aspect_ratio}
        if kind == "video":
            body["duration"] = duration
        try:
            response = self.post_json(f"{self.base}/generations", body)
        except ProviderError as exc:
            log.warning("generation failed on %s: %s", model, exc, extra={"stage": self.stage})
            return None
        self._last_credits = credits
        url = self._resolve(response)
        if self.budget is not None:
            self.budget.record(
                CostRecord(
                    stage=self.stage,
                    provider=self.name,
                    kind=budget_kind,
                    credits=credits,
                    cache_hit=False,
                )
            )
        return url

    def _resolve(self, response: Any) -> str | None:
        """Return a media URL, polling the job endpoint when the API is async."""
        url = _extract_url(response)
        if url:
            return url
        job = _job_id(response)
        if not job:
            return None
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                status = self.get_json(f"{self.base}/generations/{job}")
            except ProviderError as exc:
                log.warning("poll failed: %s", exc, extra={"stage": self.stage})
                return None
            state = str((status or {}).get("status") or "").lower()
            if state in {"failed", "error", "cancelled"}:
                log.warning("generation %s ended as %s", job, state, extra={"stage": self.stage})
                return None
            url = _extract_url(status)
            if url:
                return url
        log.warning("generation %s timed out", job, extra={"stage": self.stage})
        return None


# --------------------------------------------------------------------------- #
# Response shapes
# --------------------------------------------------------------------------- #


def _items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "assets", "hits"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _asset(item: dict, kind: str) -> LibraryAsset | None:
    url = item.get("download_url") or item.get("url") or item.get("asset_url")
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    tags = item.get("tags") or item.get("keywords") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    return LibraryAsset(
        id=str(item.get("id") or url),
        url=url,
        title=str(item.get("title") or ""),
        kind=kind,
        width=int(item.get("width") or 0),
        height=int(item.get("height") or 0),
        duration=float(item.get("duration") or 0.0),
        tags=[str(tag) for tag in tags][:12],
    )


def _extract_url(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload if payload.startswith("http") else None
    if isinstance(payload, dict):
        for key in ("output_url", "url", "video_url", "image_url", "result_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for key in ("output", "result", "data", "asset"):
            found = _extract_url(payload.get(key))
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _extract_url(item)
            if found:
                return found
    return None


def _job_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "job_id", "generation_id", "request_id"):
        value = payload.get(key)
        if isinstance(value, str | int) and str(value):
            return str(value)
    return None
