"""Internet Archive — keyless deep archive, best for historical footage.

Quality varies wildly here, so results are only ever a fallback: the ranker sees
them last in the source-priority list and the vision gate filters the rest.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...errors import ProviderError
from ..candidates import Candidate, MediaType
from .base import StockProvider, log

SEARCH_ENDPOINT = "https://archive.org/advancedsearch.php"
METADATA_ENDPOINT = "https://archive.org/metadata"
DOWNLOAD_BASE = "https://archive.org/download"

MAX_METADATA_LOOKUPS = 5

_VIDEO_FORMATS = ("h.264", "512kb mpeg4", "mpeg4", "hd mp4")
_IMAGE_FORMATS = ("jpeg", "jpg", "png", "item image")


class InternetArchiveProvider(StockProvider):
    name = "internet_archive"
    media_types = ("video", "image")
    requires_key = False
    min_interval = 0.5

    def search(self, query: str, media_type: MediaType, limit: int = 10) -> Sequence[Candidate]:
        mediatype = "movies" if media_type == "video" else "image"
        payload = (
            self.client.get_json(
                SEARCH_ENDPOINT,
                params={
                    "q": f"({query}) AND mediatype:({mediatype})",
                    "fl[]": ["identifier", "title", "description", "subject", "licenseurl"],
                    "rows": min(max(limit, 1), 25),
                    "page": 1,
                    "output": "json",
                },
            )
            or {}
        )
        docs = (payload.get("response") or {}).get("docs") or []
        results: list[Candidate] = []
        for doc in docs[:MAX_METADATA_LOOKUPS]:
            candidate = self._candidate(doc, media_type)
            if candidate:
                results.append(candidate)
        return results

    def _candidate(self, doc: dict, media_type: MediaType) -> Candidate | None:
        identifier = doc.get("identifier")
        if not identifier:
            return None
        try:
            metadata = self.client.get_json(f"{METADATA_ENDPOINT}/{identifier}") or {}
        except ProviderError as exc:
            log.debug("archive metadata %s failed: %s", identifier, exc)
            return None

        formats = _VIDEO_FORMATS if media_type == "video" else _IMAGE_FORMATS
        best = _pick_file(metadata.get("files") or [], formats)
        if not best:
            return None

        subject = doc.get("subject") or []
        tags = subject if isinstance(subject, list) else [str(subject)]
        return Candidate(
            source=self.name,
            external_id=str(identifier),
            media_type=media_type,
            download_url=f"{DOWNLOAD_BASE}/{identifier}/{best['name']}",
            page_url=f"https://archive.org/details/{identifier}",
            title=str(doc.get("title", "")),
            description=str(doc.get("description", ""))[:400],
            tags=[str(tag) for tag in tags][:12],
            width=int(float(best.get("width") or 0)),
            height=int(float(best.get("height") or 0)),
            duration=_seconds(best.get("length")),
            license=str(doc.get("licenseurl", "") or "see item page"),
            author="Internet Archive",
            raw={"identifier": identifier, "file": best.get("name")},
        )


def _pick_file(files: Sequence[dict], formats: Sequence[str]) -> dict | None:
    usable = [f for f in files if isinstance(f, dict) and f.get("name")]
    for wanted in formats:
        matches = [f for f in usable if str(f.get("format", "")).lower() == wanted]
        if matches:
            return max(matches, key=lambda f: float(f.get("size") or 0))
    return None


def _seconds(value: object) -> float:
    """Archive durations come as seconds or as ``HH:MM:SS``."""
    if value is None:
        return 0.0
    text = str(value)
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return 0.0
