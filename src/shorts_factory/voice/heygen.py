"""HeyGen Avatar 5 — the presenter track.

Preferred path: narration is synthesised locally by ElevenLabs, uploaded to
HeyGen as an audio asset and used to drive the avatar. That keeps one voice
render as the single source of truth for both the mix and the caption timings.

Fallback path: hand HeyGen the text and a voice id and let it speak. Used when
the upload endpoint is unavailable or no local narration exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..config import VIDEO_HEIGHT, VIDEO_WIDTH, Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..spec import AvatarSettings, VoiceSettings

log = get_logger("heygen")

API_BASE = "https://api.heygen.com"
UPLOAD_BASE = "https://upload.heygen.com"
POLL_INTERVAL = 10.0
POLL_TIMEOUT = 900.0


@dataclass
class AvatarClip:
    path: Path
    duration: float
    width: int = VIDEO_WIDTH
    height: int = VIDEO_HEIGHT
    transparent: bool = False
    video_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 2),
            "transparent": self.transparent,
            "video_id": self.video_id,
        }


class HeyGenClient:
    name = "heygen"

    def __init__(self, settings: Settings):
        self.settings = settings
        key = settings.credentials.heygen
        headers = {"X-Api-Key": key} if key else {}
        self.client = HttpClient(
            provider=self.name,
            timeout=max(settings.http_timeout, 60.0),
            min_interval=0.5,
            default_headers=headers,
        )

    @property
    def is_available(self) -> bool:
        return bool(self.settings.credentials.heygen) and not self.settings.offline

    def unavailable_reason(self) -> str:
        if self.settings.offline:
            return "offline mode"
        if not self.settings.credentials.heygen:
            return "HEYGEN_API_KEY not set"
        return ""

    # -- assets -------------------------------------------------------------

    def upload_audio(self, path: Path) -> str | None:
        """Upload a local audio file; returns the asset URL HeyGen can read."""
        if not self.is_available or not path.exists():
            return None
        content_type = "audio/mpeg" if path.suffix.lower() in {".mp3", ".mpeg"} else "audio/wav"
        try:
            response = self.client.request(
                "POST",
                f"{UPLOAD_BASE}/v1/asset",
                data=path.read_bytes(),
                headers={"Content-Type": content_type},
                timeout=180.0,
            )
            payload = response.json() or {}
        except (ProviderError, ValueError) as exc:
            log.warning("audio upload failed: %s", exc, extra={"stage": "avatar"})
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            url = data.get("url") or data.get("asset_url")
            if isinstance(url, str) and url.startswith("http"):
                return url
        log.warning("audio upload returned no URL: %s", str(payload)[:200], extra={"stage": "avatar"})
        return None

    # -- generation ---------------------------------------------------------

    def generate(
        self,
        avatar: AvatarSettings,
        voice: VoiceSettings,
        *,
        text: str = "",
        audio_path: Path | None = None,
        duration_hint: float = 0.0,
    ) -> AvatarClip | None:
        """Render the avatar clip and download it. ``None`` when unavailable."""
        if not avatar.enabled:
            return None
        if not self.is_available:
            log.info("skipped: %s", self.unavailable_reason(), extra={"stage": "avatar"})
            return None
        if not avatar.avatar_id:
            log.warning("avatar.avatar_id missing; skipping", extra={"stage": "avatar"})
            return None

        voice_input: dict[str, object] | None = None
        if audio_path is not None:
            audio_url = self.upload_audio(audio_path)
            if audio_url:
                voice_input = {"type": "audio", "audio_url": audio_url}
        if voice_input is None:
            if not text or not voice.voice_id:
                log.warning(
                    "no audio asset and no text/voice_id for the avatar; skipping",
                    extra={"stage": "avatar"},
                )
                return None
            voice_input = {"type": "text", "input_text": text, "voice_id": voice.voice_id}
            log.info("driving the avatar from text (audio upload unavailable)", extra={"stage": "avatar"})

        body = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": avatar.avatar_id,
                        "avatar_style": "normal",
                    },
                    "voice": voice_input,
                    "background": (
                        {"type": "transparent"}
                        if avatar.background == "transparent"
                        else {"type": "color", "value": avatar.background}
                    ),
                }
            ],
            "dimension": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
        }

        try:
            payload = self.client.post_json(f"{API_BASE}/v2/video/generate", body)
        except ProviderError as exc:
            log.error("generation request failed: %s", exc, extra={"stage": "avatar"})
            return None

        video_id = _video_id(payload)
        if not video_id:
            log.error("no video_id in response: %s", str(payload)[:200], extra={"stage": "avatar"})
            return None
        log.info("job %s queued", video_id, extra={"stage": "avatar"})

        url = self._wait_for(video_id)
        if not url:
            return None

        suffix = ".webm" if avatar.background == "transparent" else ".mp4"
        target = self.settings.paths.avatar / f"avatar_{video_id}{suffix}"
        try:
            self.client.download(url, target, min_bytes=50_000)
        except ProviderError as exc:
            log.error("avatar download failed: %s", exc, extra={"stage": "avatar"})
            return None

        from ..media.ffmpeg import probe  # local import keeps media optional here

        info = probe(target, self.settings.ffprobe_cmd)
        return AvatarClip(
            path=target,
            duration=(info.duration if info and info.duration else duration_hint),
            width=info.width if info and info.width else VIDEO_WIDTH,
            height=info.height if info and info.height else VIDEO_HEIGHT,
            transparent=avatar.background == "transparent",
            video_id=video_id,
        )

    def _wait_for(self, video_id: str) -> str | None:
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                payload = self.client.get_json(
                    f"{API_BASE}/v1/video_status.get", params={"video_id": video_id}
                )
            except ProviderError as exc:
                log.warning("status poll failed: %s", exc, extra={"stage": "avatar"})
                time.sleep(POLL_INTERVAL)
                continue

            data = (payload or {}).get("data") or {}
            status = str(data.get("status", "")).lower()
            if status == "completed":
                url = data.get("video_url")
                if isinstance(url, str) and url.startswith("http"):
                    return url
                log.error("completed without a video_url", extra={"stage": "avatar"})
                return None
            if status in {"failed", "error"}:
                log.error(
                    "job %s failed: %s", video_id, data.get("error") or "unknown", extra={"stage": "avatar"}
                )
                return None
            log.debug("job %s: %s", video_id, status or "pending", extra={"stage": "avatar"})
            time.sleep(POLL_INTERVAL)

        log.error("job %s timed out after %.0fs", video_id, POLL_TIMEOUT, extra={"stage": "avatar"})
        return None


def _video_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("video_id", "id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    value = payload.get("video_id")
    return value if isinstance(value, str) and value else None
