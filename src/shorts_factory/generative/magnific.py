"""Magnific — the premium visual tier, used deliberately and sparingly.

Three tiers are tried in order, and the first one that returns something usable
wins:

1. ``library``   — stock included with the subscription. Costs nothing.
2. ``unlimited`` — models the subscription runs without metering. Costs nothing.
3. ``premium``   — metered models. Costs tokens, so it is gated by `TokenBudget`
   and only reachable for hero slots or when everything cheaper has failed.

Magnific's REST surface differs by account tier, so every endpoint and model id
is overridable through environment variables; the defaults below match the
documented v1 shape. Unknown response fields are ignored on purpose.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..config import Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..search.candidates import Candidate, MediaType
from .budget import TokenBudget

log = get_logger("magnific")

Tier = Literal["library", "unlimited", "premium"]

DEFAULT_BASE = "https://api.magnific.ai/v1"
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 420.0

#: Token cost per premium generation. Video is materially more expensive than
#: a still, which is why the escalation policy prefers stills where it can.
PREMIUM_COST = {"image": 1, "video": 2}


@dataclass
class GenerationRequest:
    visual_id: str
    prompt: str
    media_type: MediaType
    duration: float = 4.0
    aspect_ratio: str = "9:16"
    style: str = "cinematic"
    negative_prompt: str = ""
    reference_url: str = ""


class MagnificClient:
    """Tiered client. Every method degrades to ``[]`` / ``None`` when unusable."""

    name = "magnific"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = os.environ.get("MAGNIFIC_API_BASE", DEFAULT_BASE).rstrip("/")
        self.client = HttpClient(
            provider=self.name,
            timeout=settings.http_timeout,
            min_interval=0.4,
            default_headers=self._headers(),
        )
        self.unlimited_image_model = os.environ.get("MAGNIFIC_UNLIMITED_IMAGE_MODEL", "flux-schnell")
        self.unlimited_video_model = os.environ.get("MAGNIFIC_UNLIMITED_VIDEO_MODEL", "")
        self.premium_image_model = os.environ.get("MAGNIFIC_PREMIUM_IMAGE_MODEL", "magnific-illusion-v2")
        self.premium_video_model = os.environ.get("MAGNIFIC_PREMIUM_VIDEO_MODEL", "magnific-motion-v1")

    # -- capability ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key = self.settings.credentials.magnific
        return {"Authorization": f"Bearer {key}"} if key else {}

    @property
    def is_available(self) -> bool:
        return bool(self.settings.credentials.magnific) and not self.settings.offline

    def unavailable_reason(self) -> str:
        if self.settings.offline:
            return "offline mode"
        if not self.settings.credentials.magnific:
            return "MAGNIFIC_API_KEY not set"
        return ""

    # -- tier 1: included stock --------------------------------------------

    def search_library(self, query: str, media_type: MediaType, limit: int = 8) -> list[Candidate]:
        """Search the stock library bundled with the subscription (free)."""
        if not self.is_available:
            return []
        try:
            payload = self.client.get_json(
                f"{self.base}/library/search",
                params={"query": query, "type": media_type, "limit": limit, "orientation": "portrait"},
            )
        except ProviderError as exc:
            log.warning("library search failed: %s", exc, extra={"stage": "magnific"})
            return []
        return [c for c in (self._library_candidate(item, media_type) for item in _items(payload)) if c]

    def _library_candidate(self, item: dict, media_type: MediaType) -> Candidate | None:
        url = item.get("download_url") or item.get("url") or item.get("asset_url")
        if not isinstance(url, str) or not url.startswith("http"):
            return None
        tags = item.get("tags") or item.get("keywords") or []
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        return Candidate(
            source="magnific_library",
            external_id=str(item.get("id") or url),
            media_type=media_type,
            download_url=url,
            page_url=str(item.get("page_url") or ""),
            preview_url=str(item.get("preview_url") or ""),
            title=str(item.get("title") or ""),
            description=str(item.get("description") or "")[:400],
            tags=[str(tag) for tag in tags][:12],
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            duration=float(item.get("duration") or 0.0),
            license="Magnific subscription library",
            cost_tokens=0,
            raw=item,
        )

    # -- tiers 2 & 3: generation -------------------------------------------

    def generate(
        self,
        request: GenerationRequest,
        budget: TokenBudget,
        *,
        hero: bool = False,
        allow_premium: bool = True,
    ) -> Candidate | None:
        """Generate an asset, cheapest usable tier first."""
        if not self.is_available:
            log.info("skipped: %s", self.unavailable_reason(), extra={"stage": "magnific"})
            return None

        unlimited_model = (
            self.unlimited_video_model if request.media_type == "video" else self.unlimited_image_model
        )
        if unlimited_model:
            candidate = self._submit(request, unlimited_model, tier="unlimited")
            if candidate:
                budget.record_free(request.visual_id, "magnific:unlimited", f"model={unlimited_model}")
                return candidate
            log.info(
                "%s: unlimited tier produced nothing, considering premium",
                request.visual_id,
                extra={"stage": "magnific"},
            )

        if not allow_premium:
            return None

        cost = PREMIUM_COST.get(request.media_type, 1)
        allowed, why = budget.can_spend(request.visual_id, cost, hero=hero)
        if not allowed:
            log.warning("%s: premium skipped — %s", request.visual_id, why, extra={"stage": "magnific"})
            return None

        model = self.premium_video_model if request.media_type == "video" else self.premium_image_model
        candidate = self._submit(request, model, tier="premium")
        if candidate is None:
            return None
        budget.spend(
            request.visual_id,
            cost,
            "magnific:premium",
            f"model={model} prompt={request.prompt[:60]!r}",
            hero=hero,
        )
        candidate.cost_tokens = cost
        return candidate

    def _submit(self, request: GenerationRequest, model: str, *, tier: Tier) -> Candidate | None:
        endpoint = f"{self.base}/generations"
        body: dict[str, Any] = {
            "model": model,
            "type": request.media_type,
            "prompt": request.prompt,
            "aspect_ratio": request.aspect_ratio,
            "style": request.style,
        }
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        if request.reference_url:
            body["reference_url"] = request.reference_url
        if request.media_type == "video":
            body["duration"] = max(2.0, min(request.duration, 12.0))

        try:
            response = self.client.post_json(endpoint, body)
        except ProviderError as exc:
            log.warning(
                "%s: %s generation failed: %s", request.visual_id, tier, exc, extra={"stage": "magnific"}
            )
            return None

        url = self._resolve(response)
        if not url:
            return None

        return Candidate(
            source=f"magnific_{tier}",
            external_id=str((response or {}).get("id") or request.visual_id),
            media_type=request.media_type,
            download_url=url,
            title=request.prompt[:120],
            description=request.prompt,
            tags=[word for word in request.prompt.lower().split() if len(word) > 3][:12],
            width=1080,
            height=1920,
            duration=request.duration if request.media_type == "video" else 0.0,
            license="Magnific generated",
            generated=True,
            raw={"model": model, "tier": tier},
        )

    def _resolve(self, response: Any) -> str | None:
        """Return a media URL, polling the job endpoint when the API is async."""
        url = _extract_url(response)
        if url:
            return url

        job_id = (response or {}).get("id") if isinstance(response, dict) else None
        if not job_id:
            return None

        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                status = self.client.get_json(f"{self.base}/generations/{job_id}")
            except ProviderError as exc:
                log.warning("poll %s failed: %s", job_id, exc, extra={"stage": "magnific"})
                return None
            state = str((status or {}).get("status", "")).lower()
            if state in {"failed", "error", "cancelled"}:
                log.warning(
                    "generation %s ended as %s: %s",
                    job_id,
                    state,
                    (status or {}).get("error", ""),
                    extra={"stage": "magnific"},
                )
                return None
            url = _extract_url(status)
            if url:
                return url
        log.warning("generation %s timed out after %.0fs", job_id, POLL_TIMEOUT, extra={"stage": "magnific"})
        return None


def _items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "assets"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_url(payload: Any) -> str | None:
    """Pull the first plausible media URL out of a generation response."""
    if not isinstance(payload, dict):
        return None
    for key in ("output_url", "url", "video_url", "image_url", "result_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for key in ("output", "result", "data", "assets"):
        nested = payload.get(key)
        if isinstance(nested, str) and nested.startswith("http"):
            return nested
        if isinstance(nested, dict):
            found = _extract_url(nested)
            if found:
                return found
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, str) and item.startswith("http"):
                    return item
                found = _extract_url(item)
                if found:
                    return found
    return None


def prompt_for(visual_query: str, context: str, style_hint: str, avoid: Sequence[str] = ()) -> str:
    """Compose a generation prompt that stays anchored to the narration."""
    parts = [visual_query.strip()]
    if context:
        parts.append(f"illustrating: {context.strip()[:180]}")
    parts.append(style_hint)
    parts.append("vertical 9:16 composition, centred subject, clean negative space for captions")
    if avoid:
        parts.append("avoid: " + ", ".join(avoid))
    return ", ".join(part for part in parts if part)
