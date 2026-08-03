"""Brandbook support.

The brandbook is not available yet, so this module defines its shape and merges
whatever exists on disk over the per-video `brand_elements`. Precedence is:

    brand_elements (per video)  >  brand/brandbook.json  >  built-in defaults

That way the day the brandbook arrives, dropping the file in is the whole
integration — no code change, and individual videos can still override.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..spec import BrandElements

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
        log.info("brandbook loaded: %s", known["name"] or path.name)
        return cls(
            **known,
            watermark_opacity=float(opacity) if isinstance(opacity, (int, float)) else None,
            lower_third=lower_third if isinstance(lower_third, bool) else None,
            outro_card=outro_card if isinstance(outro_card, bool) else None,
            safe_area_margin=int(data.get("safe_area_margin", 96) or 96),
            extra={k: v for k, v in data.items() if k not in {"colors", "typography"}},
        )


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
