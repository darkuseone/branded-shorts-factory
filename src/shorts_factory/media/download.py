"""Asset download with a content-addressed cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..search.candidates import Candidate
from .ffmpeg import MediaInfo, probe

log = get_logger("download")

MIN_VIDEO_BYTES = 20_000
MIN_IMAGE_BYTES = 4_000


@dataclass
class LocalAsset:
    """A candidate that now exists on disk, with its real measurements."""

    candidate: Candidate
    path: Path
    info: MediaInfo | None = None

    @property
    def width(self) -> int:
        return self.info.width if self.info and self.info.width else self.candidate.width

    @property
    def height(self) -> int:
        return self.info.height if self.info and self.info.height else self.candidate.height

    @property
    def duration(self) -> float:
        return self.info.duration if self.info and self.info.duration else self.candidate.duration

    @property
    def is_video(self) -> bool:
        return self.candidate.media_type == "video"

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "source": self.candidate.source,
            # What the library calls this clip. Without it the report can say a
            # slot was filled but not with what, and "why is there a hair salon
            # in my video" has to be answered by watching the video.
            "title": self.candidate.title,
            "license": self.candidate.license,
            "page_url": self.candidate.page_url,
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 2),
            "cost_tokens": self.candidate.cost_tokens,
        }


class Downloader:
    """Fetches candidates into the run's download directory, once each."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = HttpClient(provider="download", timeout=max(settings.http_timeout, 60.0), retries=3)

    def fetch(self, candidate: Candidate, *, subdir: str = "") -> LocalAsset | None:
        """Download a candidate; returns ``None`` if it cannot be retrieved."""
        if self.settings.offline:
            log.info("offline mode: skipping download of %s", candidate.key, extra={"stage": "assets"})
            return None
        if not candidate.download_url:
            return None

        target_dir = self.settings.paths.downloads / subdir if subdir else self.settings.paths.downloads
        target = target_dir / candidate.suggested_filename
        minimum = MIN_VIDEO_BYTES if candidate.media_type == "video" else MIN_IMAGE_BYTES

        try:
            path = self.client.download(candidate.download_url, target, min_bytes=minimum)
        except ProviderError as exc:
            log.warning("could not download %s: %s", candidate.key, exc, extra={"stage": "assets"})
            return None

        info = probe(path, self.settings.ffprobe_cmd)
        if info and info.width and info.height:
            # Trust measured dimensions over provider metadata.
            candidate.width, candidate.height = info.width, info.height
            if info.duration:
                candidate.duration = info.duration
        return LocalAsset(candidate=candidate, path=path, info=info)
