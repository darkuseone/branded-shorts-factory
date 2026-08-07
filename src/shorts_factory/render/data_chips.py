"""Data chips — minimal JetBrains Mono number callouts for sci-pop Shorts.

Extracts a few hard numbers from the narration and places short panel chips
that rise with a UI blip. Never more than three per video; never in the hook
or the CTA. Keeps the frame clean: one fact, one chip, one job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..logging_utils import get_logger
from ..spec import Spec
from .timeline import TRACK_CHIPS, Element, Timeline

log = get_logger("data_chips")

_CHIP_RE = re.compile(
    r"("
    r"\d+(?:[.,]\d+)?\s*(?:%|°C|°|К|K|км/с|км|м/с|м|с|атм|г|кг|т|св\.?\s*лет)?"
    r"|"
    r"(?:девяносто|четыреста|сто|тысяч[аи]?|миллион(?:а|ов)?|миллиард(?:а|ов)?|бесконечн\w*)"
    r"(?:\s+\w+){0,4}"
    r")",
    re.IGNORECASE,
)

MAX_CHIPS = 3


@dataclass(frozen=True)
class DataChip:
    text: str
    start: float
    duration: float = 2.2


def extract_chips(spec: Spec, *, limit: int = MAX_CHIPS) -> list[DataChip]:
    """Pull the most punchy numeric facts from body segments."""
    chips: list[DataChip] = []
    hook_end = spec.hook.end if spec.hook else 0.0
    climax = spec.cta.start if spec.cta else max(0.0, spec.duration_target - 3.5)

    for segment in spec.all_segments:
        if spec.hook is not None and segment.id == spec.hook.id:
            continue
        if segment.start < hook_end:
            continue
        if segment.end > climax - 0.5:
            continue
        match = _CHIP_RE.search(segment.text)
        if not match:
            continue
        label = _clean_chip(match.group(1))
        if len(label) < 2 or len(label) > 42:
            continue
        start = segment.start + min(0.55, max(0.25, segment.duration * 0.2))
        duration = min(2.4, max(1.6, segment.duration * 0.45))
        if start + duration > climax:
            continue
        chips.append(DataChip(text=label, start=start, duration=duration))
        if len(chips) >= limit:
            break

    return chips


def _clean_chip(raw: str) -> str:
    text = " ".join(raw.split())
    # Prefer compact units for the mono panel.
    replacements = (
        ("световых лет", "св. лет"),
        ("атмосфер", "атм"),
        ("градусов", "°"),
    )
    lowered = text.lower()
    for src, dst in replacements:
        if src in lowered:
            # Keep original casing for digits; only swap the unit phrase.
            idx = lowered.index(src)
            text = text[:idx] + dst + text[idx + len(src) :]
            break
    return text.strip(" ,.;:—-")


def add_data_chips(timeline: Timeline, spec: Spec) -> list[DataChip]:
    """Append chip elements to the timeline. Returns what was added."""
    chips = extract_chips(spec)
    for index, chip in enumerate(chips):
        timeline.add(
            Element(
                id=f"data_chip_{index}",
                kind="text",
                start=chip.start,
                duration=chip.duration,
                track=TRACK_CHIPS,
                text=chip.text,
                props={
                    "role": "data_chip",
                    "color": spec.brand.color_primary,
                    "panel": "#14171C",
                    "accent": spec.brand.color_accent,
                },
            )
        )
    if chips:
        log.info(
            "data chips: %s",
            ", ".join(f"{chip.text!r}@{chip.start:g}s" for chip in chips),
            extra={"stage": "timeline"},
        )
    return chips
