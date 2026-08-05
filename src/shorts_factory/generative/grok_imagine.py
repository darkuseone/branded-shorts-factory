"""Grok Imagine — the reserve generator.

Only reached when free stock was weak *and* Magnific could not deliver (no key,
budget exhausted, or generation failed). Kept intentionally thin.
"""

from __future__ import annotations

import os
from typing import Any

from ..config import Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..search.candidates import Candidate, MediaType

log = get_logger("grok_imagine")

DEFAULT_BASE = "https://api.x.ai/v1"


class GrokImagineClient:
    name = "grok_imagine"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base = os.environ.get("GROK_API_BASE", DEFAULT_BASE).rstrip("/")
        self.model = os.environ.get("GROK_IMAGE_MODEL", "grok-2-image")
        key = settings.credentials.grok
        self.client = HttpClient(
            provider=self.name,
            timeout=settings.http_timeout,
            min_interval=0.5,
            default_headers={"Authorization": f"Bearer {key}"} if key else {},
        )

    @property
    def is_available(self) -> bool:
        return bool(self.settings.credentials.grok) and not self.settings.offline

    def generate_image(self, visual_id: str, prompt: str) -> Candidate | None:
        if not self.is_available:
            return None
        try:
            payload = self.client.post_json(
                f"{self.base}/images/generations",
                {"model": self.model, "prompt": prompt, "n": 1, "response_format": "url"},
            )
        except ProviderError as exc:
            log.warning("%s: generation failed: %s", visual_id, exc, extra={"stage": "grok"})
            return None

        url = _first_url(payload)
        if not url:
            return None
        return Candidate(
            source="grok_imagine",
            external_id=visual_id,
            media_type="image",
            download_url=url,
            title=prompt[:120],
            description=prompt,
            width=1080,
            height=1920,
            license="Grok Imagine generated",
            generated=True,
            raw={"model": self.model},
        )

    def generate(self, visual_id: str, prompt: str, media_type: MediaType) -> Candidate | None:
        """Images only for now; a video request falls back to a still."""
        if media_type == "video":
            log.info(
                "%s: no video model wired for Grok Imagine, generating a still instead",
                visual_id,
                extra={"stage": "grok"},
            )
        return self.generate_image(visual_id, prompt)


def _first_url(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url.startswith("http"):
                    return url
    url = payload.get("url")
    return url if isinstance(url, str) and url.startswith("http") else None
