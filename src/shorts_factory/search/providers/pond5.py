"""Pond5 — free-tier clips only.

Pond5's search API is key-gated and its response shape varies by account tier,
so parsing here is deliberately defensive: unknown fields are ignored and
anything that is not explicitly free is dropped rather than risking a paid
asset ending up in a render.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from ..candidates import Candidate, MediaType
from .base import StockProvider

DEFAULT_ENDPOINT = "https://api.pond5.com/v2/search"

_FREE_MARKERS = ("free", "public domain", "cc0")


class Pond5FreeProvider(StockProvider):
    name = "pond5_free"
    media_types = ("video",)
    requires_key = True
    min_interval = 0.5

    @property
    def api_key(self) -> str | None:
        return self.settings.credentials.pond5

    @property
    def endpoint(self) -> str:
        return os.environ.get("POND5_SEARCH_ENDPOINT", DEFAULT_ENDPOINT)

    def default_headers(self) -> dict[str, str]:
        key = self.settings.credentials.pond5
        return {"Authorization": f"Bearer {key}"} if key else {}

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        if media_type != "video":
            return []
        payload = (
            self.client.get_json(
                self.endpoint,
                params={"q": query, "kind": "video", "free": "1", "limit": min(max(limit, 1), 50)},
            )
            or {}
        )
        items = _items(payload)
        results: list[Candidate] = []
        for item in items:
            candidate = self._candidate(item)
            if candidate:
                results.append(candidate)
        return results

    def _candidate(self, item: dict) -> Candidate | None:
        if not _looks_free(item):
            return None
        url = item.get("download_url") or item.get("preview_url") or item.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            return None
        keywords = item.get("keywords") or item.get("tags") or []
        if isinstance(keywords, str):
            keywords = [part.strip() for part in keywords.split(",") if part.strip()]
        return Candidate(
            source=self.name,
            external_id=str(item.get("id") or url),
            media_type="video",
            download_url=url,
            page_url=str(item.get("page_url") or item.get("link") or ""),
            title=str(item.get("title") or ""),
            description=str(item.get("description") or "")[:400],
            tags=[str(k) for k in keywords][:12],
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            duration=float(item.get("duration") or 0.0),
            license="Pond5 free tier",
            author=str(item.get("artist") or ""),
            raw=item,
        )


def _items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "hits"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _looks_free(item: dict) -> bool:
    if item.get("free") is True or item.get("is_free") is True:
        return True
    price = item.get("price")
    if isinstance(price, (int, float)) and price == 0:
        return True
    license_text = str(item.get("license") or item.get("license_type") or "").lower()
    return any(marker in license_text for marker in _FREE_MARKERS)
