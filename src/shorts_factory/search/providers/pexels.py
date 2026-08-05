"""Pexels — free stock video and photos. https://www.pexels.com/api/documentation/"""

from __future__ import annotations

from collections.abc import Sequence

from ..candidates import Candidate, MediaType
from .base import StockProvider, pick_largest

VIDEO_ENDPOINT = "https://api.pexels.com/videos/search"
PHOTO_ENDPOINT = "https://api.pexels.com/v1/search"


class PexelsProvider(StockProvider):
    name = "pexels"
    media_types = ("video", "image")
    requires_key = True
    min_interval = 0.25

    @property
    def api_key(self) -> str | None:
        return self.settings.credentials.pexels

    def default_headers(self) -> dict[str, str]:
        key = self.settings.credentials.pexels
        return {"Authorization": key} if key else {}

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        params = {
            "query": query,
            "per_page": min(max(limit, 1), 80),
            # Ask for portrait first — it removes the crop step entirely.
            "orientation": "portrait",
            "size": "medium",
        }
        if media_type == "video":
            payload = self.client.get_json(VIDEO_ENDPOINT, params=params) or {}
            return [c for c in (self._video(item) for item in payload.get("videos", [])) if c]
        payload = self.client.get_json(PHOTO_ENDPOINT, params=params) or {}
        return [c for c in (self._photo(item) for item in payload.get("photos", [])) if c]

    def _video(self, item: dict) -> Candidate | None:
        files = [f for f in item.get("video_files", []) if f.get("link")]
        best = pick_largest(files)
        if not best:
            return None
        return Candidate(
            source=self.name,
            external_id=str(item.get("id", "")),
            media_type="video",
            download_url=best["link"],
            page_url=item.get("url", ""),
            preview_url=item.get("image", ""),
            title=item.get("alt") or _slug_title(item.get("url", "")),
            description=item.get("alt", ""),
            tags=[tag for tag in _slug_title(item.get("url", "")).split() if tag],
            width=int(best.get("width") or item.get("width") or 0),
            height=int(best.get("height") or item.get("height") or 0),
            duration=float(item.get("duration") or 0.0),
            license="Pexels License",
            author=(item.get("user") or {}).get("name", ""),
            raw=item,
        )

    def _photo(self, item: dict) -> Candidate | None:
        src = item.get("src") or {}
        url = src.get("original") or src.get("large2x") or src.get("large")
        if not url:
            return None
        alt = item.get("alt", "")
        return Candidate(
            source=self.name,
            external_id=str(item.get("id", "")),
            media_type="image",
            download_url=url,
            page_url=item.get("url", ""),
            preview_url=src.get("medium", ""),
            title=alt or _slug_title(item.get("url", "")),
            description=alt,
            tags=[tag for tag in _slug_title(item.get("url", "")).split() if tag],
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            license="Pexels License",
            author=item.get("photographer", ""),
            raw=item,
        )


def _slug_title(page_url: str) -> str:
    """Pexels puts the human title in the URL slug; recover it for matching."""
    slug = page_url.rstrip("/").rsplit("/", 1)[-1]
    parts = [part for part in slug.split("-") if part and not part.isdigit()]
    return " ".join(parts)
