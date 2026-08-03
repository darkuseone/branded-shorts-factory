"""The normalised shape every provider returns."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

MediaType = Literal["video", "image"]


@dataclass
class Candidate:
    """One asset offered by a provider, normalised across very different APIs."""

    source: str
    external_id: str
    media_type: MediaType
    download_url: str
    page_url: str = ""
    preview_url: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    duration: float = 0.0
    license: str = "unknown"
    author: str = ""
    cost_tokens: int = 0
    generated: bool = False
    matched_queries: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- identity -----------------------------------------------------------

    @property
    def key(self) -> str:
        """Stable dedupe key: same asset from two queries collapses into one."""
        return f"{self.source}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Content-ish fingerprint used for cross-source dedupe and filenames."""
        basis = f"{self.source}|{self.external_id}|{urlparse(self.download_url).path}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    # -- derived facts ------------------------------------------------------

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return bool(self.height) and self.aspect <= 0.85

    @property
    def is_square_ish(self) -> bool:
        return 0.85 < self.aspect < 1.2

    @property
    def is_free(self) -> bool:
        return self.cost_tokens == 0

    @property
    def searchable_text(self) -> str:
        return " ".join(filter(None, [self.title, self.description, " ".join(self.tags)]))

    @property
    def suggested_filename(self) -> str:
        suffix = _suffix_for(self.download_url, self.media_type)
        return f"{self.source}_{self.fingerprint}{suffix}"

    def merge_query(self, query: str) -> None:
        """Record that another query also produced this asset (a relevance signal)."""
        if query and query not in self.matched_queries:
            self.matched_queries.append(query)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "id": self.external_id,
            "media_type": self.media_type,
            "download_url": self.download_url,
            "page_url": self.page_url,
            "title": self.title,
            "tags": self.tags,
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 2),
            "license": self.license,
            "author": self.author,
            "cost_tokens": self.cost_tokens,
            "generated": self.generated,
            "matched_queries": self.matched_queries,
        }


_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".m4v", ".mkv")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")


def _suffix_for(url: str, media_type: MediaType) -> str:
    path = urlparse(url).path.lower()
    known = _VIDEO_SUFFIXES if media_type == "video" else _IMAGE_SUFFIXES
    for suffix in known:
        if path.endswith(suffix):
            return suffix
    return ".mp4" if media_type == "video" else ".jpg"
