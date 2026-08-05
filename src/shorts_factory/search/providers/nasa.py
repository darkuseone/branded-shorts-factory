"""NASA Image and Video Library — keyless, unmatched for space footage."""

from __future__ import annotations

from collections.abc import Sequence

from ...errors import ProviderError
from ..candidates import Candidate, MediaType
from .base import StockProvider, log

SEARCH_ENDPOINT = "https://images-api.nasa.gov/search"

# NASA returns a per-item collection manifest rather than direct media links, so
# each hit costs a second request. Resolve only the most promising few.
MAX_ASSET_LOOKUPS = 6

_VIDEO_PREFERENCE = ("~orig.mp4", "~large.mp4", "mobile.mp4", ".mp4")
_IMAGE_PREFERENCE = ("~orig.jpg", "~large.jpg", ".jpg", ".png")


class NasaProvider(StockProvider):
    name = "nasa"
    media_types = ("video", "image")
    requires_key = False
    min_interval = 0.35

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        payload = (
            self.client.get_json(
                SEARCH_ENDPOINT,
                params={"q": query, "media_type": media_type, "page_size": min(max(limit, 1), 100)},
            )
            or {}
        )
        items = (payload.get("collection") or {}).get("items") or []
        results: list[Candidate] = []
        for item in items[:MAX_ASSET_LOOKUPS]:
            candidate = self._candidate(item, media_type)
            if candidate:
                results.append(candidate)
        return results

    def _candidate(self, item: dict, media_type: MediaType) -> Candidate | None:
        data_list = item.get("data") or []
        if not data_list:
            return None
        data = data_list[0]
        nasa_id = data.get("nasa_id")
        manifest = item.get("href")
        if not nasa_id or not manifest:
            return None

        try:
            files = self.client.get_json(manifest) or []
        except ProviderError as exc:
            log.debug("nasa manifest %s failed: %s", nasa_id, exc)
            return None
        if not isinstance(files, list):
            return None

        preference = _VIDEO_PREFERENCE if media_type == "video" else _IMAGE_PREFERENCE
        url = _preferred(files, preference)
        if not url:
            return None

        previews = [link.get("href") for link in item.get("links") or [] if link.get("href")]
        return Candidate(
            source=self.name,
            external_id=str(nasa_id),
            media_type=media_type,
            download_url=url,
            page_url=f"https://images.nasa.gov/details/{nasa_id}",
            preview_url=previews[0] if previews else "",
            title=data.get("title", ""),
            description=(data.get("description") or "")[:400],
            tags=[str(keyword) for keyword in (data.get("keywords") or [])][:12],
            # The manifest carries no dimensions; NASA masters are ≥1080p in
            # practice, and the vision gate is the real arbiter of quality.
            width=1920,
            height=1080,
            license="NASA media usage guidelines (generally public domain)",
            author=data.get("center", "NASA"),
            raw={"nasa_id": nasa_id},
        )


def _preferred(urls: Sequence[str], preference: Sequence[str]) -> str | None:
    https = [url for url in urls if isinstance(url, str) and url.startswith("http")]
    for suffix in preference:
        for url in https:
            if url.lower().endswith(suffix):
                return url
    return None
