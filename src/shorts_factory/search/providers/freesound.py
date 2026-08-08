"""Freesound — online SFX fill when the local bank has no match.

Search is real (API token). Acceptance is local: duration, sample rate,
license preference (CC0 > CC-BY), and the same envelope classifier used by
``sfxscan``. Drones/beds never become accents. Accepted files are cached into
``assets/sfx/`` so the bank grows and the next run costs nothing.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from ...config import Settings
from ...http import HttpClient
from ...logging_utils import get_logger
from ...voice.sfx_analysis import analyze

log = get_logger("freesound")

API_BASE = "https://freesound.org/apiv2"
ROLE_QUERIES: dict[str, str] = {
    "impact": "impact hit bang oneshot",
    "whoosh": "whoosh swoosh transition",
    "thump": "soft thump punch kick",
    "riser": "riser build tension",
    "ui": "ui click blip interface",
    "pop": "pop click ui",
    "power_down": "power down shut tick soft",
    "power_up": "power up boot tick soft",
    "transition": "whoosh transition sweep",
    "swoosh": "swoosh whoosh",
    "click": "click ui",
    "glitch": "glitch digital short",
}


class FreesoundClient:
    """Thin search + download client. Missing key → unavailable, never fatal."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = HttpClient(provider="freesound", timeout=45.0, min_interval=0.35)
        self._token = getattr(settings.credentials, "freesound", None)

    def is_available(self) -> bool:
        return bool(self._token)

    def unavailable_reason(self) -> str:
        return "" if self._token else "FREESOUND_API_KEY not set"

    def find_for_role(self, role: str, *, max_candidates: int = 8) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        query = ROLE_QUERIES.get(role, role)
        # Prefer short, high-rate, CC0 first.
        params = {
            "query": query,
            "page_size": max_candidates,
            "fields": "id,name,duration,license,previews,filesize,samplerate,type,tags,username",
            "filter": "duration:[0.1 TO 4.5] samplerate:[44100 TO 192000]",
            "sort": "rating_desc",
            "token": self._token,
        }
        try:
            payload = self.http.get_json(f"{API_BASE}/search/text/", params=params) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("freesound search failed: %s", exc)
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        return list(results or [])

    def download_preview(self, entry: dict[str, Any], destination: Path) -> Path | None:
        previews = entry.get("previews") or {}
        url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
        if not url:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.http.download(url, destination)
        except Exception as exc:  # noqa: BLE001
            log.warning("freesound download failed: %s", exc)
            return None
        return destination if destination.exists() else None


def accept_for_role(path: Path, role: str, *, ffmpeg: str = "ffmpeg") -> bool:
    """Run sfxscan-style analysis; reject drones/beds and role mismatches."""
    profile = analyze(path, ffmpeg=ffmpeg)
    if profile is None:
        return False
    if not profile.usable_as_accent:
        return False
    if profile.duration > 4.5 and role not in {"riser"}:
        return False
    if role and role not in profile.roles:
        # Allow close aliases.
        aliases = {
            "whoosh": {"swoosh", "transition"},
            "swoosh": {"whoosh", "transition"},
            "ui": {"click", "pop", "glitch"},
            "thump": {"impact"},
            "impact": {"thump"},
            "power_down": {"click", "ui"},
            "power_up": {"click", "ui"},
        }
        if not (aliases.get(role, set()) & set(profile.roles)):
            return False
    return True


_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def cache_name(role: str, entry: dict[str, Any]) -> str:
    raw = f"freesound-{entry.get('id')}-{entry.get('name') or role}"
    stem = _SAFE_NAME.sub("-", raw).strip("-").lower()[:80]
    return f"{stem}.mp3"


def fill_from_freesound(
    role: str,
    *,
    client: FreesoundClient,
    bank_dir: Path,
    ffmpeg: str = "ffmpeg",
) -> Path | None:
    """Search → download → analyze → keep in bank, or None."""
    if not client.is_available():
        return None
    for entry in client.find_for_role(role):
        license_text = str(entry.get("license") or "").lower()
        # Prefer CC0; still accept CC-BY.
        if (
            "creative commons 0" not in license_text
            and "cc0" not in license_text
            and "attribution" not in license_text
            and "by" not in license_text
        ):
            continue
        dest = bank_dir / cache_name(role, entry)
        if dest.exists() and accept_for_role(dest, role, ffmpeg=ffmpeg):
            return dest
        downloaded = client.download_preview(entry, dest)
        if downloaded is None:
            continue
        if accept_for_role(downloaded, role, ffmpeg=ffmpeg):
            log.info("freesound accepted %s for role=%s", downloaded.name, role)
            return downloaded
        with contextlib.suppress(OSError):
            downloaded.unlink(missing_ok=True)
    return None


def load_pixabay_catalog(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        return list(data.get("items") or [])
    return list(data)


def fill_from_pixabay_catalog(
    role: str,
    *,
    catalog: list[dict[str, Any]],
    bank_dir: Path,
    http: HttpClient,
    ffmpeg: str = "ffmpeg",
) -> Path | None:
    """Manual Pixabay links curated in assets/catalogs/pixabay-sfx.json."""
    matches = [
        entry
        for entry in catalog
        if role in (entry.get("roles") or entry.get("tags") or [])
        or role in str(entry.get("title") or "").lower()
    ]
    for entry in matches:
        url = entry.get("url") or entry.get("download_url")
        if not url:
            continue
        name = _SAFE_NAME.sub("-", str(entry.get("id") or entry.get("title") or role)).lower()
        dest = bank_dir / f"pixabay-{name}.mp3"
        if dest.exists() and accept_for_role(dest, role, ffmpeg=ffmpeg):
            return dest
        try:
            http.download(str(url), dest)
        except Exception as exc:  # noqa: BLE001
            log.warning("pixabay-sfx download failed: %s", exc)
            continue
        if accept_for_role(dest, role, ffmpeg=ffmpeg):
            return dest
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
    return None
