"""Brandbook support.

Precedence:

    brand_elements (per video)  >  brand/brandbook.json  >  built-in defaults

The brandbook also carries channel-wide defaults for avatar look, ElevenLabs
voice, music-by-rubric and meme policy — those are applied onto the Spec in
``apply_brandbook_to_spec``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..spec import BrandElements, CaptionSettings, Spec, VoiceSettings

log = get_logger("brand")

BRANDBOOK_FILENAME = "brandbook.json"


@dataclass
class Brandbook:
    """Studio-wide identity. Every field is optional."""

    name: str = ""
    color_primary: str = ""
    color_accent: str = ""
    color_background: str = ""
    font_family: str = ""
    logo: str = ""
    logo_position: str = ""
    watermark_opacity: float | None = None
    caption_highlight: str = ""
    lower_third: bool | None = None
    outro_card: bool | None = None
    safe_area_margin: int = 96
    music_by_rubric: dict[str, str] = field(default_factory=dict)
    memes: dict[str, Any] = field(default_factory=dict)
    avatar: dict[str, Any] = field(default_factory=dict)
    voice: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path) -> Brandbook | None:
        path = directory / BRANDBOOK_FILENAME
        if not path.exists():
            log.info("no brandbook at %s (using per-video brand_elements)", path)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("brandbook unreadable, ignoring: %s", exc)
            return None
        if not isinstance(data, dict):
            log.warning("brandbook must be a JSON object, ignoring")
            return None

        colors = data.get("colors") if isinstance(data.get("colors"), dict) else {}
        typography = data.get("typography") if isinstance(data.get("typography"), dict) else {}
        music_map = data.get("music_by_rubric") if isinstance(data.get("music_by_rubric"), dict) else {}
        memes = data.get("memes") if isinstance(data.get("memes"), dict) else {}
        avatar = data.get("avatar") if isinstance(data.get("avatar"), dict) else {}
        voice = data.get("voice") if isinstance(data.get("voice"), dict) else {}
        known = {
            "name": str(data.get("name", "")),
            "color_primary": str(colors.get("primary", data.get("color_primary", ""))),
            "color_accent": str(colors.get("accent", data.get("color_accent", ""))),
            "color_background": str(colors.get("background", data.get("color_background", ""))),
            "caption_highlight": str(colors.get("caption_highlight", "")),
            "font_family": str(typography.get("family", data.get("font_family", ""))),
            "logo": str(data.get("logo", "")),
            "logo_position": str(data.get("logo_position", "")),
        }
        opacity = data.get("watermark_opacity")
        lower_third = data.get("lower_third")
        outro_card = data.get("outro_card")
        reserved = {
            "colors",
            "typography",
            "music_by_rubric",
            "memes",
            "avatar",
            "voice",
            "name",
            "logo",
            "logo_position",
            "watermark_opacity",
            "lower_third",
            "outro_card",
            "safe_area_margin",
            "color_primary",
            "color_accent",
            "color_background",
            "font_family",
        }
        log.info("brandbook loaded: %s", known["name"] or path.name)
        return cls(
            **known,
            watermark_opacity=float(opacity) if isinstance(opacity, (int, float)) else None,
            lower_third=lower_third if isinstance(lower_third, bool) else None,
            outro_card=outro_card if isinstance(outro_card, bool) else None,
            safe_area_margin=int(data.get("safe_area_margin", 96) or 96),
            music_by_rubric={str(k): str(v) for k, v in music_map.items()},
            memes=dict(memes),
            avatar=dict(avatar),
            voice=dict(voice),
            extra={k: v for k, v in data.items() if k not in reserved},
        )

    def music_for_rubric(self, rubric: str) -> str:
        """Track stem for a rubric, case-insensitive. Empty when unknown."""
        if not rubric:
            return ""
        wanted = rubric.strip()
        if wanted in self.music_by_rubric:
            return self.music_by_rubric[wanted]
        lowered = {key.lower(): value for key, value in self.music_by_rubric.items()}
        return lowered.get(wanted.lower(), "")


def apply_brandbook(brand: BrandElements, book: Brandbook | None) -> BrandElements:
    """Fill unset per-video fields from the brandbook."""
    if book is None:
        return brand

    defaults = BrandElements()
    updates: dict[str, Any] = {}

    def take(field_name: str, book_value: Any) -> None:
        if not book_value:
            return
        current = getattr(brand, field_name)
        # Only override values the author left at the built-in default.
        if current == getattr(defaults, field_name):
            updates[field_name] = book_value

    take("color_primary", book.color_primary)
    take("color_accent", book.color_accent)
    take("color_background", book.color_background)
    take("font_family", book.font_family)
    take("logo", book.logo)
    take("logo_position", book.logo_position)
    if book.watermark_opacity is not None and brand.watermark_opacity == defaults.watermark_opacity:
        updates["watermark_opacity"] = book.watermark_opacity
    if book.lower_third is not None and brand.lower_third == defaults.lower_third:
        updates["lower_third"] = book.lower_third
    if book.outro_card is not None and brand.outro_card == defaults.outro_card:
        updates["outro_card"] = book.outro_card

    return replace(brand, **updates) if updates else brand


def apply_brandbook_to_spec(spec: Spec, book: Brandbook | None) -> Spec:
    """Merge brandbook into brand, voice, avatar and music when the JSON left them blank."""
    if book is None:
        return spec

    spec.brand = apply_brandbook(spec.brand, book)

    voice_defaults = VoiceSettings()
    voice_updates: dict[str, Any] = {}
    book_voice_id = str(book.voice.get("voice_id") or "")
    if book_voice_id and not spec.voice.voice_id:
        voice_updates["voice_id"] = book_voice_id
    for field_name, key, cast in (
        ("model", "model", str),
        ("stability", "stability", float),
        ("similarity_boost", "similarity_boost", float),
        ("style", "style", float),
        ("speed", "speed", float),
        ("language", "language", str),
    ):
        raw = book.voice.get(key)
        if raw is None or raw == "":
            continue
        current = getattr(spec.voice, field_name)
        if current == getattr(voice_defaults, field_name):
            try:
                voice_updates[field_name] = cast(raw)
            except (TypeError, ValueError):
                continue
    if voice_updates:
        spec.voice = replace(spec.voice, **voice_updates)

    look_id = str(book.avatar.get("look_id") or book.avatar.get("avatar_id") or "")
    if look_id and not spec.avatar.avatar_id:
        spec.avatar = replace(spec.avatar, avatar_id=look_id, enabled=True)

    if not spec.music.track:
        track = book.music_for_rubric(spec.rubric)
        if track:
            spec.music = replace(spec.music, track=track)
            log.info("music track from rubric %r → %s", spec.rubric, track)

    # Channel meme defaults: enable when brandbook says so and the scenario
    # did not explicitly disable (enabled false is the JSON default).
    if book.memes.get("enabled") and not spec.memes.enabled and "memes" not in spec.raw:
        tags = book.memes.get("tags") if isinstance(book.memes.get("tags"), list) else []
        max_duration = book.memes.get("max_duration")
        spec.memes = replace(
            spec.memes,
            enabled=True,
            tags=[str(tag) for tag in tags] if tags else spec.memes.tags,
            max_duration=float(max_duration)
            if isinstance(max_duration, (int, float))
            else spec.memes.max_duration,
        )

    if book.caption_highlight and spec.captions.highlight_color == CaptionSettings().highlight_color:
        spec.captions = replace(spec.captions, highlight_color=book.caption_highlight)

    return spec
