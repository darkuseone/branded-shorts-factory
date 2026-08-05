"""Thin FFmpeg/FFprobe helpers.

FFmpeg is required for rendering but *optional* for planning, so every function
here degrades to a documented no-op when the binary is missing. That keeps
`validate`/`plan` usable on machines without FFmpeg installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..logging_utils import get_logger

log = get_logger("ffmpeg")

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"})


@dataclass
class MediaInfo:
    path: Path
    width: int = 0
    height: int = 0
    duration: float = 0.0
    has_audio: bool = False
    codec: str = ""

    @property
    def is_vertical(self) -> bool:
        return bool(self.height) and self.width < self.height

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "width": self.width,
            "height": self.height,
            "duration": round(self.duration, 3),
            "has_audio": self.has_audio,
            "codec": self.codec,
        }


def is_available(binary: str) -> bool:
    return shutil.which(binary) is not None


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo | None:
    """Inspect a media file. Returns ``None`` when ffprobe cannot be used."""
    if not path.exists():
        return None
    if not is_available(ffprobe):
        log.debug("ffprobe unavailable; skipping probe of %s", path.name)
        return None

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffprobe failed on %s: %s", path.name, exc)
        return None
    if completed.returncode != 0:
        log.warning("ffprobe rejected %s: %s", path.name, completed.stderr.strip()[:200])
        return None

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return None

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _float((payload.get("format") or {}).get("duration"))
    if not duration and video:
        duration = _float(video.get("duration"))

    return MediaInfo(
        path=path,
        width=int(video.get("width") or 0) if video else 0,
        height=int(video.get("height") or 0) if video else 0,
        duration=duration,
        has_audio=audio is not None,
        codec=str(video.get("codec_name") or "") if video else "",
    )


def extract_keyframes(
    path: Path,
    destination: Path,
    *,
    count: int = 3,
    duration: float | None = None,
    ffmpeg: str = "ffmpeg",
    prefix: str = "frame",
) -> list[Path]:
    """Grab evenly spaced stills from a clip for the vision gate.

    Still images are simply copied through, so callers can treat images and
    video identically.
    """
    destination.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        target = destination / f"{prefix}_00{path.suffix.lower()}"
        if target != path:
            target.write_bytes(path.read_bytes())
        return [target]

    if not is_available(ffmpeg):
        log.warning("ffmpeg unavailable; cannot extract keyframes from %s", path.name)
        return []

    length = duration or 0.0
    if length <= 0:
        info = probe(path, ffmpeg.replace("ffmpeg", "ffprobe"))
        length = info.duration if info else 0.0
    if length <= 0:
        length = 4.0

    # Sample inside the clip, never at the very first/last frame (fades, black).
    offsets = [length * (index + 1) / (count + 1) for index in range(max(1, count))]
    frames: list[Path] = []
    for index, offset in enumerate(offsets):
        target = destination / f"{prefix}_{index:02d}.jpg"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{offset:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-vf",
            "scale='min(720,iw)':-2",
            str(target),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("keyframe extraction failed for %s: %s", path.name, exc)
            continue
        if completed.returncode == 0 and target.exists() and target.stat().st_size > 0:
            frames.append(target)
        else:
            log.debug("no frame at %.2fs of %s: %s", offset, path.name, completed.stderr.strip()[:160])
    return frames


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
