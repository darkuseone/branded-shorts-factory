"""Automatic audio design.

A Short with silent cuts feels amateur. Two jobs live here:

* **Placement** — if the author did not hand-place `audio_fx`, propose a
  restrained set: an accent under the hook, a transition on each visual cut, a
  pop when the CTA lands. Author-provided effects always win.
* **Sourcing** — decide where each sound comes from. Your own `assets/sfx/`
  bank is always preferred: it is free, instant, and reusing the same handful
  of sounds is what makes a channel recognisable. Generation only covers what
  the bank cannot answer.
"""

from __future__ import annotations

from ..assets.library import SfxLibrary
from ..logging_utils import get_logger
from ..spec import AudioFx, Spec
from .elevenlabs import SfxClip

log = get_logger("audio_design")

#: Ceiling on auto-generated effects. Past this it stops being design and
#: starts being noise (and burns ElevenLabs credits for nothing).
MAX_AUTO_FX = 8
#: Cuts closer together than this share one effect.
MIN_GAP = 1.2


def suggest_audio_fx(spec: Spec) -> list[AudioFx]:
    """Propose effects for a spec that has none."""
    if spec.audio_fx:
        return list(spec.audio_fx)

    suggestions: list[AudioFx] = []

    if spec.hook:
        # Land an impact right as the hook lands, not at t=0 over silence.
        suggestions.append(
            AudioFx(type="impact", at=max(0.0, spec.hook.start + 0.05), intensity=0.7, duration=1.0)
        )

    cuts = sorted({round(visual.start, 2) for visual in spec.visuals if visual.start > 0.4})
    last = -MIN_GAP
    for cut in cuts:
        if cut - last < MIN_GAP:
            continue
        suggestions.append(AudioFx(type="whoosh", at=max(0.0, cut - 0.12), intensity=0.5, duration=0.7))
        last = cut

    if spec.cta:
        suggestions.append(AudioFx(type="pop", at=spec.cta.start, intensity=0.55, duration=0.6))

    suggestions.sort(key=lambda fx: fx.at)
    trimmed = suggestions[:MAX_AUTO_FX]
    if trimmed:
        log.info(
            "proposed %d audio effects (%s)",
            len(trimmed),
            ", ".join(f"{fx.type}@{fx.at:g}s" for fx in trimmed),
            extra={"stage": "audio"},
        )
    return trimmed


def fx_volume(intensity: float) -> float:
    """Peak level for one effect. Kept under the narration by construction."""
    return round(0.15 + 0.30 * max(0.0, min(intensity, 1.0)), 3)


def resolve_from_library(effects: list[AudioFx], library: SfxLibrary) -> tuple[list[SfxClip], list[AudioFx]]:
    """Split effects into "your bank has this" and "this needs generating".

    Returns ``(clips, still_needed)``. The second list is what a generator has
    to cover; when it is empty the whole audio-design layer costs nothing and
    works with no API key at all.
    """
    clips: list[SfxClip] = []
    missing: list[AudioFx] = []

    for fx in effects:
        # An explicit prompt means the author wants a specific sound; let the
        # words in it steer the match too.
        extra = [word for word in fx.prompt.lower().split() if len(word) > 2][:4]
        item = library.pick(fx.type, extra)
        if item is None:
            missing.append(fx)
            continue
        clips.append(
            SfxClip(
                fx_id=fx.id,
                path=item.path,
                start=fx.at,
                duration=item.duration or fx.duration,
                volume=fx_volume(fx.intensity),
                source="library",
            )
        )

    if clips:
        log.info(
            "%d of %d effect(s) came from your sfx bank (%s)",
            len(clips),
            len(effects),
            ", ".join(sorted({clip.path.stem for clip in clips})),
            extra={"stage": "audio"},
        )
    return clips, missing
