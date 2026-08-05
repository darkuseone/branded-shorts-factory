"""Wikimedia Commons — keyless, enormous, and strong on science imagery."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..candidates import Candidate, MediaType
from .base import StockProvider

ENDPOINT = "https://commons.wikimedia.org/w/api.php"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Commons hosts plenty of exotic containers; only offer what a browser renderer
# and FFmpeg both handle without a transcode surprise.
_ALLOWED_MIME = ("image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm")


class WikimediaProvider(StockProvider):
    name = "wikimedia"
    media_types = ("video", "image")
    requires_key = False
    min_interval = 0.4

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        filetype = "video" if media_type == "video" else "bitmap"
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": f"filetype:{filetype} {query}",
            "gsrnamespace": "6",
            "gsrlimit": min(max(limit, 1), 50),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "1600",
        }
        payload = self.client.get_json(ENDPOINT, params=params) or {}
        pages = (payload.get("query") or {}).get("pages") or []
        results: list[Candidate] = []
        for page in pages:
            candidate = self._candidate(page, media_type)
            if candidate:
                results.append(candidate)
        return results

    def _candidate(self, page: dict, media_type: MediaType) -> Candidate | None:
        infos = page.get("imageinfo") or []
        if not infos:
            return None
        info = infos[0]
        mime = (info.get("mime") or "").lower()
        if not mime.startswith(("image/", "video/")) or mime not in _ALLOWED_MIME:
            return None
        url = info.get("url")
        if not url:
            return None

        meta = info.get("extmetadata") or {}
        description = _plain(_meta(meta, "ImageDescription"))
        categories = _plain(_meta(meta, "Categories")).replace("|", " ")
        title = str(page.get("title", "")).removeprefix("File:")

        return Candidate(
            source=self.name,
            external_id=str(page.get("pageid", title)),
            media_type=media_type,
            download_url=url,
            page_url=info.get("descriptionurl", ""),
            preview_url=info.get("thumburl", ""),
            title=_filename_words(title),
            description=description[:400],
            tags=[word for word in categories.split() if len(word) > 2][:12],
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            duration=float(info.get("duration") or 0.0),
            license=_plain(_meta(meta, "LicenseShortName")) or "see Commons page",
            author=_plain(_meta(meta, "Artist"))[:120],
            raw={"pageid": page.get("pageid"), "title": title},
        )


def _meta(meta: dict, key: str) -> str:
    entry = meta.get(key)
    if isinstance(entry, dict):
        return str(entry.get("value", ""))
    return ""


def _plain(html: str) -> str:
    return " ".join(_HTML_TAG_RE.sub(" ", html).split())


def _filename_words(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return " ".join(part for part in re.split(r"[_\-\s]+", stem) if part)
