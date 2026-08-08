"""Data chips — minimal JetBrains Mono number callouts for sci-pop Shorts.

Extracts a few hard numbers / clinical labels from the narration and places
short panel chips that rise with a UI blip. Never more than three per video;
never in the hook or the CTA. Keeps the frame clean: one fact, one chip, one job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..logging_utils import get_logger
from ..spec import Spec
from .timeline import TRACK_DATA_CHIP, Element, Timeline

log = get_logger("data_chips")

_CHIP_RE = re.compile(
    r"("
    r"\d+(?:[.,]\d+)?\s*(?:%|°C|°|К|K|км/с|км|м/с|м|с|атм|г|кг|т|св\.?\s*лет)?"
    r"|"
    r"(?:ER-?100|OCT4|SOX2|KLF4|OSK|Phase\s*1|NAION)"
    r"|"
    r"(?:девяносто|четыреста|сто|тысяч[аи]?|миллион(?:а|ов)?|миллиард(?:а|ов)?|бесконечн\w*)"
    r"(?:\s+\w+){0,4}"
    r")",
    re.IGNORECASE,
)

# Prefer crisp clinical labels over raw number scraping when present.
_LABEL_ORDER = ("OCT4", "SOX2", "KLF4", "ER-100", "Phase 1", "NAION")

MAX_CHIPS = 4


@dataclass(frozen=True)
class DataChip:
    text: str
    start: float
    duration: float = 2.2


def extract_chips(spec: Spec, *, limit: int = MAX_CHIPS) -> list[DataChip]:
    """Pull punchy clinical labels / numbers from body segments."""
    chips: list[DataChip] = []
    seen: set[str] = set()
    hook_end = spec.hook.end if spec.hook else 0.0
    climax = spec.cta.start if spec.cta else max(0.0, spec.duration_target - 3.5)

    # Pass 1: OSK trio + Phase 1 — real UI over footage, not baked AI text.
    priority = ("OCT4", "SOX2", "KLF4", "Phase 1")
    for segment in spec.all_segments:
        if spec.hook is not None and segment.id == spec.hook.id:
            continue
        if segment.start < hook_end or segment.end > climax - 0.5:
            continue
        labels = [lab for lab in _labels_in(segment.text) if lab in priority]
        for i, label in enumerate(labels):
            key = label.upper()
            if key in seen:
                continue
            start = segment.start + 0.4 + 0.7 * i
            duration = min(1.9, max(1.2, segment.duration - 0.7 * i - 0.3))
            if start + 0.5 > climax:
                continue
            chips.append(DataChip(text=label, start=start, duration=duration))
            seen.add(key)
            if len(chips) >= limit:
                return chips

    # Pass 2: other clinical tokens / numbers.
    for segment in spec.all_segments:
        if spec.hook is not None and segment.id == spec.hook.id:
            continue
        if segment.start < hook_end or segment.end > climax - 0.5:
            continue
        for label in _labels_in(segment.text):
            key = label.upper()
            if key in seen:
                continue
            start = segment.start + 0.45
            duration = min(2.0, max(1.3, segment.duration * 0.4))
            if start + 0.5 > climax:
                continue
            chips.append(DataChip(text=label, start=start, duration=duration))
            seen.add(key)
            if len(chips) >= limit:
                return chips
        match = _CHIP_RE.search(segment.text)
        if not match:
            continue
        label = _clean_chip(match.group(1))
        if len(label) < 2 or len(label) > 42 or label.upper() in seen:
            continue
        start = segment.start + min(0.55, max(0.25, segment.duration * 0.2))
        duration = min(2.4, max(1.6, segment.duration * 0.45))
        if start + duration > climax:
            continue
        chips.append(DataChip(text=label, start=start, duration=duration))
        seen.add(label.upper())
        if len(chips) >= limit:
            break

    return chips


def _labels_in(text: str) -> list[str]:
    upper = text.upper().replace(" ", "")
    found: list[str] = []
    for label in _LABEL_ORDER:
        token = label.upper().replace(" ", "").replace("-", "")
        # Allow ER-100 / ER100
        hay = upper.replace("-", "")
        if token.replace("-", "") in hay:
            found.append(label)
    return found


def _clean_chip(raw: str) -> str:
    text = " ".join(raw.split())
    replacements = (
        ("световых лет", "св. лет"),
        ("атмосфер", "атм"),
        ("градусов", "°"),
    )
    lowered = text.lower()
    for src, dst in replacements:
        if src in lowered:
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
                track=TRACK_DATA_CHIP,
                text=chip.text,
                props={
                    "role": "data_chip",
                    "emphasis": "high",
                },
            )
        )
    if chips:
        log.info("data chips: %s", ", ".join(c.text for c in chips))
    return chips
