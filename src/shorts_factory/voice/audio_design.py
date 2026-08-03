"""Automatic audio design.

A Short with silent cuts feels amateur. If the author did not hand-place
`audio_fx`, this module proposes a restrained set: an accent under the hook, a
transition on each visual cut, a pop when the CTA lands. Author-provided effects
always win — this only fills an empty array.
"""

from __future__ import annotations

from ..logging_utils import get_logger
from ..spec import AudioFx, Spec

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
