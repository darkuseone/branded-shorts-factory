"""Stage news article stills / OG images into jobs/<id>/broll/.

Used when research.json has claim URLs or photo_hint pointing at press photos.
Offline / cache rebuilds skip this and use pre-staged top-*.mp4 instead.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from ..logging_utils import get_logger

log = get_logger("news_shots")

_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_OG_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.I,
)


def stage_research_shots(
    research_path: Path,
    broll_dir: Path,
    *,
    limit: int = 8,
) -> list[Path]:
    """Download up to ``limit`` article stills named ``news-01.jpg`` … into broll."""
    if not research_path.is_file():
        return []
    try:
        payload = json.loads(research_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    items = payload.get("items") or payload.get("claims") or []
    broll_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    index = 1
    for item in items:
        if index > limit:
            break
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("source_url") or "").strip()
        hint = str(item.get("photo_hint") or item.get("image") or "").strip()
        image_url = hint if hint.startswith("http") else ""
        if not image_url and url:
            image_url = _fetch_og_image(url) or ""
        if not image_url:
            continue
        dest = broll_dir / f"news-{index:02d}.jpg"
        try:
            _download(image_url, dest)
        except Exception as exc:  # noqa: BLE001 — best-effort staging
            log.warning("news shot failed %s: %s", image_url, exc)
            continue
        if dest.exists() and dest.stat().st_size >= 32_768:
            saved.append(dest)
            index += 1
    return saved


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "REDSHIFT-shorts/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        dest.write_bytes(resp.read())


def _fetch_og_image(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "REDSHIFT-shorts/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310
            html = resp.read(200_000).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    match = _OG_RE.search(html) or _OG_RE_ALT.search(html)
    if not match:
        return None
    image = match.group(1).strip()
    if image.startswith("//"):
        image = "https:" + image
    if image.startswith("/"):
        parsed = urlparse(url)
        image = f"{parsed.scheme}://{parsed.netloc}{image}"
    return image if image.startswith("http") else None
