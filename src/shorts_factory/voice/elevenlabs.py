"""ElevenLabs v3 — narration and the audio-design layer.

Two jobs live here:

* **Narration** — one file per script segment. The timestamped endpoint is used
  when available so captions get real word timings instead of estimates.
* **Audio design** — whooshes, risers, impacts and UI ticks generated from the
  `audio_fx` array. These are what make a Short feel produced rather than
  assembled, and they are cheap.

`stability` is authored on a 0–100 scale in the JSON (40–55 is the Shorts
window) and converted to the API's 0–1 scale here, in one place.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..errors import ProviderError
from ..http import HttpClient
from ..logging_utils import get_logger
from ..spec import AudioFx, ScriptSegment, VoiceSettings

log = get_logger("elevenlabs")

API_BASE = "https://api.elevenlabs.io/v1"
OUTPUT_FORMAT = "mp3_44100_128"


@dataclass
class WordTiming:
    word: str
    start: float
    end: float


@dataclass
class VoiceClip:
    """One rendered narration file, positioned on the master timeline."""

    segment_id: str
    path: Path
    start: float
    duration: float
    text: str
    words: list[WordTiming] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "path": str(self.path),
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "words": len(self.words),
        }


@dataclass
class SfxClip:
    fx_id: str
    path: Path
    start: float
    duration: float
    volume: float
    #: "library" for a sound you supplied, "elevenlabs" for a generated one.
    source: str = "elevenlabs"
    #: Seconds to skip from the start of the source file. Used to cut long
    #: cinematic hits down to the useful part around their measured peak.
    trim_start: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "fx_id": self.fx_id,
            "path": str(self.path),
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "volume": round(self.volume, 3),
            "source": self.source,
            "trim_start": round(self.trim_start, 3),
        }


#: Prompts for the audio-design layer. Short, concrete and mix-ready.
FX_PROMPTS = {
    "whoosh": "fast clean whoosh transition, tight, no reverb tail",
    "impact": "punchy cinematic impact hit, deep but short",
    "riser": "short rising tension riser building to a cut",
    "swoosh": "light swoosh for a UI element sliding in",
    "click": "crisp UI click, dry",
    "pop": "soft bubble pop for a text element appearing",
    "glitch": "short digital glitch stutter",
    "sub_drop": "deep sub bass drop, very short",
    "transition": "modern short-form video transition sound, snappy",
    "ui": "subtle interface confirmation blip",
    "thump": "soft low thump pulse, short, dry, not aggressive",
    "power_down": "short quiet power-down tick, soft electronic fade",
    "power_up": "short quiet power-up tick, soft electronic chirp",
}


class ElevenLabsClient:
    name = "elevenlabs"

    def __init__(self, settings: Settings):
        self.settings = settings
        key = settings.credentials.elevenlabs
        self.client = HttpClient(
            provider=self.name,
            timeout=max(settings.http_timeout, 90.0),
            min_interval=0.2,
            default_headers={"xi-api-key": key} if key else {},
        )

    @property
    def is_available(self) -> bool:
        return bool(self.settings.credentials.elevenlabs) and not self.settings.offline

    def unavailable_reason(self) -> str:
        if self.settings.offline:
            return "offline mode"
        if not self.settings.credentials.elevenlabs:
            return "ELEVENLABS_API_KEY not set"
        return ""

    # -- narration ----------------------------------------------------------

    def synthesize_segment(self, segment: ScriptSegment, voice: VoiceSettings) -> VoiceClip | None:
        """Render one script segment to disk, with word timings when possible."""
        if not self.is_available:
            log.info("skipped: %s", self.unavailable_reason(), extra={"stage": "voice"})
            return None
        if not voice.voice_id:
            log.warning("voice_settings.voice_id is empty; narration skipped", extra={"stage": "voice"})
            return None

        target = self.settings.paths.voice / f"{segment.id}.mp3"
        body = {
            "text": segment.text,
            "model_id": voice.model,
            "voice_settings": {
                "stability": round(voice.stability / 100.0, 3),
                "similarity_boost": round(voice.similarity_boost, 3),
                "style": round(voice.style, 3),
                "use_speaker_boost": True,
                "speed": round(voice.speed, 3),
            },
        }
        params = {"output_format": OUTPUT_FORMAT}
        url = f"{API_BASE}/text-to-speech/{voice.voice_id}/with-timestamps"

        words: list[WordTiming] = []
        try:
            response = self.client.request("POST", url, json_body=body, params=params)
            payload = json.loads(response.body.decode("utf-8"))
            audio = base64.b64decode(payload.get("audio_base64", ""))
            words = _words_from_alignment(payload.get("alignment") or payload.get("normalized_alignment"))
        except (ProviderError, ValueError, json.JSONDecodeError) as exc:
            log.info(
                "%s: timestamped synthesis unavailable (%s), falling back to plain TTS",
                segment.id,
                exc,
                extra={"stage": "voice"},
            )
            try:
                response = self.client.request(
                    "POST", f"{API_BASE}/text-to-speech/{voice.voice_id}", json_body=body, params=params
                )
                audio = response.body
            except ProviderError as exc2:
                log.error("%s: synthesis failed: %s", segment.id, exc2, extra={"stage": "voice"})
                return None

        if not audio:
            log.error("%s: empty audio returned", segment.id, extra={"stage": "voice"})
            return None

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio)

        duration = words[-1].end if words else segment.duration
        return VoiceClip(
            segment_id=segment.id,
            path=target,
            start=segment.start,
            duration=duration,
            text=segment.text,
            words=words,
        )

    def synthesize_script(self, segments: list[ScriptSegment], voice: VoiceSettings) -> list[VoiceClip]:
        clips: list[VoiceClip] = []
        for segment in segments:
            clip = self.synthesize_segment(segment, voice)
            if clip:
                clips.append(clip)
        return clips

    # -- audio design -------------------------------------------------------

    def generate_fx(self, fx: AudioFx) -> SfxClip | None:
        """Generate one sound effect from the `audio_fx` array."""
        if not self.is_available:
            return None
        prompt = fx.prompt or FX_PROMPTS.get(fx.type, FX_PROMPTS["transition"])
        target = self.settings.paths.voice / f"{fx.id}.mp3"
        try:
            response = self.client.request(
                "POST",
                f"{API_BASE}/sound-generation",
                json_body={
                    "text": prompt,
                    "duration_seconds": round(max(0.5, min(fx.duration, 5.0)), 2),
                    "prompt_influence": 0.6,
                },
                params={"output_format": OUTPUT_FORMAT},
            )
        except ProviderError as exc:
            log.warning("fx %s failed: %s", fx.id, exc, extra={"stage": "voice"})
            return None
        if not response.body:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.body)
        return SfxClip(
            fx_id=fx.id,
            path=target,
            start=fx.at,
            duration=fx.duration,
            # Keep effects under the narration; 0.45 peak is a safe ceiling.
            volume=round(0.15 + 0.30 * fx.intensity, 3),
        )

    def generate_all_fx(self, effects: list[AudioFx]) -> list[SfxClip]:
        return [clip for clip in (self.generate_fx(fx) for fx in effects) if clip]

    def cache_fx_to_bank(self, clip: SfxClip, bank_dir: Path) -> Path | None:
        """Copy a generated effect into ``assets/sfx/`` so the bank grows.

        Filenames are tagged with the fx type so later ``sfxscan`` + pick()
        can reuse them. Index.json is left for ``sfxscan`` to refresh.
        """
        if not clip.path.exists():
            return None
        bank_dir.mkdir(parents=True, exist_ok=True)
        # fx_id looks like "fx_whoosh_3" — keep type in the filename.
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in clip.fx_id)
        dest = bank_dir / f"generated-{safe}{clip.path.suffix or '.mp3'}"
        if dest.exists():
            return dest
        try:
            dest.write_bytes(clip.path.read_bytes())
        except OSError as exc:
            log.warning("could not cache fx %s into bank: %s", clip.fx_id, exc, extra={"stage": "voice"})
            return None
        log.info("cached generated fx → %s", dest.name, extra={"stage": "voice"})
        return dest


def _words_from_alignment(alignment: object) -> list[WordTiming]:
    """Fold ElevenLabs' character-level alignment into word timings."""
    if not isinstance(alignment, dict):
        return []
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not (isinstance(chars, list) and isinstance(starts, list) and isinstance(ends, list)):
        return []
    if not (len(chars) == len(starts) == len(ends)):
        return []

    words: list[WordTiming] = []
    buffer = ""
    start = 0.0
    end = 0.0
    for char, char_start, char_end in zip(chars, starts, ends, strict=True):
        if str(char).isspace():
            if buffer:
                words.append(WordTiming(buffer, start, end))
                buffer = ""
            continue
        if not buffer:
            start = float(char_start)
        buffer += str(char)
        end = float(char_end)
    if buffer:
        words.append(WordTiming(buffer, start, end))
    return words
