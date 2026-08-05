"""Pixabay — free stock video and photos. https://pixabay.com/api/docs/"""

from __future__ import annotations

from collections.abc import Sequence

from ..candidates import Candidate, MediaType
from .base import StockProvider, pick_largest

IMAGE_ENDPOINT = "https://pixabay.com/api/"
VIDEO_ENDPOINT = "https://pixabay.com/api/videos/"


class PixabayProvider(StockProvider):
    name = "pixabay"
    media_types = ("video", "image")
    requires_key = True
    min_interval = 0.3

    @property
    def api_key(self) -> str | None:
        return self.settings.credentials.pixabay

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        params = {
            "key": self.api_key,
            "q": query,
            "per_page": min(max(limit, 3), 200),
            "safesearch": "true",
        }
        if media_type == "video":
            payload = self.client.get_json(VIDEO_ENDPOINT, params=params) or {}
            return [c for c in (self._video(item) for item in payload.get("hits", [])) if c]
        params["image_type"] = "photo"
        payload = self.client.get_json(IMAGE_ENDPOINT, params=params) or {}
        return [c for c in (self._image(item) for item in payload.get("hits", [])) if c]

    def _video(self, item: dict) -> Candidate | None:
        variants = [
            {**data, "quality": name}
            for name, data in (item.get("videos") or {}).items()
            if isinstance(data, dict) and data.get("url")
        ]
        best = pick_largest(variants)
        if not best:
            return None
        tags = [tag.strip() for tag in (item.get("tags") or "").split(",") if tag.strip()]
        return Candidate(
            source=self.name,
            external_id=str(item.get("id", "")),
            media_type="video",
            download_url=best["url"],
            page_url=item.get("pageURL", ""),
            preview_url=(item.get("videos", {}).get("tiny") or {}).get("thumbnail", ""),
            title=" ".join(tags[:4]),
            description=item.get("pageURL", "").rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
            tags=tags,
            width=int(best.get("width") or 0),
            height=int(best.get("height") or 0),
            duration=float(item.get("duration") or 0.0),
            license="Pixabay Content License",
            author=item.get("user", ""),
            raw=item,
        )

    def _image(self, item: dict) -> Candidate | None:
        url = item.get("largeImageURL") or item.get("fullHDURL") or item.get("webformatURL")
        if not url:
            return None
        tags = [tag.strip() for tag in (item.get("tags") or "").split(",") if tag.strip()]
        return Candidate(
            source=self.name,
            external_id=str(item.get("id", "")),
            media_type="image",
            download_url=url,
            page_url=item.get("pageURL", ""),
            preview_url=item.get("previewURL", ""),
            title=" ".join(tags[:4]),
            description="",
            tags=tags,
            width=int(item.get("imageWidth") or 0),
            height=int(item.get("imageHeight") or 0),
            license="Pixabay Content License",
            author=item.get("user", ""),
            raw=item,
        )
