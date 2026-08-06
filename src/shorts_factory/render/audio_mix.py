"""Pre-mix the soundtrack with FFmpeg before it reaches the renderer.

Doing the mix here — rather than leaving three audio tracks for the renderer to
juggle — buys two things a Short really needs: real sidechain ducking (music
steps back under the voice instead of sitting at a flat level) and a limiter so
the loudness is consistent across videos.

If FFmpeg is missing the pipeline falls back to separate audio elements with
static volumes; the composition writer handles that case.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..logging_utils import get_logger
from ..media.ffmpeg import is_available
from ..spec import MusicSettings
from ..voice.elevenlabs import SfxClip, VoiceClip
from .ffmpeg_utils import escape_path

log = get_logger("audio")

# Ducking that sounds intentional rather than pumped.
DUCK_THRESHOLD = 0.03
DUCK_RATIO = 9
DUCK_ATTACK_MS = 15
DUCK_RELEASE_MS = 350
#: Effects step back under narration so punctuation never covers a word.
SFX_DUCK_DB = -6.0


@dataclass
class MixResult:
    path: Path | None
    used_ffmpeg: bool
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None and self.path.exists()


def build_mix(
    destination: Path,
    *,
    duration: float,
    voice_clips: list[VoiceClip],
    sfx_clips: list[SfxClip],
    music_path: Path | None,
    music: MusicSettings,
    ffmpeg: str = "ffmpeg",
) -> MixResult:
    """Render narration + effects + ducked music into one file."""
    if not (voice_clips or sfx_clips or music_path):
        return MixResult(None, False, "nothing to mix")
    if not is_available(ffmpeg):
        return MixResult(None, False, "ffmpeg unavailable")

    destination.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    filters: list[str] = []
    voice_labels: list[str] = []
    sfx_labels: list[str] = []

    for index, clip in enumerate(voice_clips):
        inputs += ["-i", str(clip.path)]
        label = f"v{index}"
        filters.append(
            f"[{len(inputs) // 2 - 1}:a]aresample=44100,adelay={int(clip.start * 1000)}:all=1[{label}]"
        )
        voice_labels.append(label)

    for index, clip in enumerate(sfx_clips):
        inputs += ["-i", str(clip.path)]
        label = f"s{index}"
        # adelay cannot go negative; anything that would start before t=0 is
        # trimmed off the head of the file instead, so a whoosh whose peak
        # sits 0.8s in still lands on a cut at t=0.2.
        delay_ms = max(0, int(round(clip.start * 1000)))
        head_trim = max(0.0, getattr(clip, "trim_start", 0.0))
        if clip.start < 0:
            head_trim += -clip.start
        chain = f"[{len(inputs) // 2 - 1}:a]aresample=44100"
        if head_trim > 0 or clip.duration > 0:
            end = head_trim + max(clip.duration, 0.05)
            chain += f",atrim={head_trim:.3f}:{end:.3f},asetpts=N/SR/TB"
        chain += f",volume={clip.volume:.3f}"
        if delay_ms > 0:
            chain += f",adelay={delay_ms}:all=1"
        filters.append(chain + f"[{label}]")
        sfx_labels.append(label)

    music_label = ""
    if music_path is not None and music_path.exists():
        inputs += ["-i", str(music_path)]
        music_index = len(inputs) // 2 - 1
        fade_out_start = max(0.0, duration - max(music.fade_out, 0.1))
        chain = (
            # 10 minutes of samples is well past any Shorts length.
            f"[{music_index}:a]aresample=44100,aloop=loop=-1:size=26460000,"
            f"atrim=0:{duration:.3f},asetpts=N/SR/TB,"
            f"volume={music.volume:.3f},"
            f"afade=t=in:st=0:d={max(music.fade_in, 0.01):.3f},"
            f"afade=t=out:st={fade_out_start:.3f}:d={max(music.fade_out, 0.01):.3f}"
        )
        if music.start_at > 0:
            chain += f",adelay={int(music.start_at * 1000)}:all=1"
        filters.append(chain + "[music]")
        music_label = "music"

    stems: list[str] = []

    if voice_labels:
        filters.append(
            f"{''.join(f'[{label}]' for label in voice_labels)}"
            f"amix=inputs={len(voice_labels)}:normalize=0:dropout_transition=0[voiceraw]"
        )
        # Split voice once per sidechain consumer — FFmpeg labels are single-use.
        duck_music = bool(music_label and music.ducking)
        duck_sfx = bool(sfx_labels)
        if duck_music and duck_sfx:
            filters.append("[voiceraw]asplit=3[voice][voicekey_m][voicekey_s]")
        elif duck_music or duck_sfx:
            key = "voicekey_m" if duck_music else "voicekey_s"
            filters.append(f"[voiceraw]asplit=2[voice][{key}]")
        else:
            filters.append("[voiceraw]anull[voice]")

        if duck_music:
            filters.append(
                f"[{music_label}][voicekey_m]sidechaincompress="
                f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
                f"attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[ducked]"
            )
            stems += ["voice", "ducked"]
        elif music_label:
            stems += ["voice", music_label]
        else:
            stems.append("voice")
    elif music_label:
        stems.append(music_label)

    if sfx_labels:
        filters.append(
            f"{''.join(f'[{label}]' for label in sfx_labels)}"
            f"amix=inputs={len(sfx_labels)}:normalize=0:dropout_transition=0[sfxraw]"
        )
        if voice_labels:
            # −6 dB under voice: soft sidechain so hits still read but never bury words.
            filters.append(
                f"[sfxraw][voicekey_s]sidechaincompress="
                f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
                f"attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS},"
                f"volume={10 ** (SFX_DUCK_DB / 20.0):.4f}[sfx]"
            )
        else:
            filters.append("[sfxraw]anull[sfx]")
        stems.append("sfx")

    if not stems:
        return MixResult(None, False, "no usable audio stems")

    if len(stems) == 1:
        filters.append(f"[{stems[0]}]anull[premix]")
    else:
        filters.append(
            f"{''.join(f'[{stem}]' for stem in stems)}"
            f"amix=inputs={len(stems)}:normalize=0:dropout_transition=0[premix]"
        )

    # Trim to the exact target length and keep peaks under control.
    filters.append(
        f"[premix]atrim=0:{duration:.3f},asetpts=N/SR/TB,"
        "alimiter=level_in=1:level_out=0.95:limit=0.95,"
        "loudnorm=I=-14:TP=-1.5:LRA=11[out]"
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        str(destination),
    ]

    log.debug("ffmpeg mix: %s", " ".join(escape_path(part) for part in command))
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return MixResult(None, False, f"ffmpeg failed: {exc}")

    if completed.returncode != 0 or not destination.exists():
        return MixResult(None, False, f"ffmpeg error: {completed.stderr.strip()[:300]}")

    log.info(
        "mixed %d narration + %d fx%s -> %s",
        len(voice_clips),
        len(sfx_clips),
        " + music" if music_label else "",
        destination.name,
        extra={"stage": "audio"},
    )
    return MixResult(destination, True)
