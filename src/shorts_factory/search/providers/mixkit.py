"""Mixkit — curated catalogue.

Mixkit publishes no search API, so this provider matches against a local
catalogue file (`assets/catalogs/mixkit.json`) that the team extends by hand
with clips worth reusing. It works offline, costs nothing, and never scrapes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from ...config import Settings
from ..candidates import Candidate, MediaType
from ..keywords import tokenize
from .base import StockProvider, log

CATALOG_RELATIVE = Path("assets") / "catalogs" / "mixkit.json"


class MixkitProvider(StockProvider):
    name = "mixkit"
    media_types = ("video", "image")
    requires_key = False

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._entries: list[dict] | None = None

    @property
    def catalog_path(self) -> Path:
        return self.settings.paths.root / CATALOG_RELATIVE

    def is_available(self) -> bool:
        # Matching is local, so this provider works in offline mode too.
        return self.catalog_path.exists()

    def unavailable_reason(self) -> str:
        return "" if self.catalog_path.exists() else f"no catalogue at {CATALOG_RELATIVE}"

    def _catalog(self) -> list[dict]:
        if self._entries is None:
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                self._entries = data.get("items", []) if isinstance(data, dict) else list(data)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("mixkit catalogue unreadable: %s", exc)
                self._entries = []
        return self._entries

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        wanted = set(tokenize(query))
        if not wanted:
            return []
        scored: list[tuple[float, Candidate]] = []
        for entry in self._catalog():
            if entry.get("media_type", "video") != media_type:
                continue
            haystack = set(tokenize(" ".join([entry.get("title", ""), *entry.get("tags", [])])))
            if not haystack:
                continue
            overlap = len(wanted & haystack) / len(wanted)
            if overlap <= 0:
                continue
            scored.append((overlap, self._candidate(entry, media_type)))
        scored.sort(key=lambda pair: -pair[0])
        return [candidate for _, candidate in scored[:limit]]

    def _candidate(self, entry: dict, media_type: MediaType) -> Candidate:
        return Candidate(
            source=self.name,
            external_id=str(entry.get("id") or entry.get("url", "")),
            media_type=media_type,
            download_url=entry.get("url", ""),
            page_url=entry.get("page_url", ""),
            title=entry.get("title", ""),
            description=entry.get("description", ""),
            tags=list(entry.get("tags", [])),
            width=int(entry.get("width") or 0),
            height=int(entry.get("height") or 0),
            duration=float(entry.get("duration") or 0.0),
            license=entry.get("license", "Mixkit License"),
            author=entry.get("author", "Mixkit"),
            raw=entry,
        )
