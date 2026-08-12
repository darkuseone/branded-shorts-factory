"""Render infographic slots instead of searching for them.

A slot of `type: infographic` used to go to the stock providers with a query
like "data visualization animation". Whatever comes back is a chart of
somebody else's numbers — decorative at best, and actively misleading when the
voiceover is saying "almost nine hundred tasks" over a bar chart that says
something else. Generating one is worse: models are confident and wrong about
figures, and a wrong number on screen is the one mistake this channel cannot
make.

So a card carries its own content in the scenario, and this module turns it
into a real asset: HTML on brand tokens, screenshotted and animated by
`redshift.render.infographics`, handed back as a local file the timeline
places like any other clip.

Cost: zero. No search, no tokens, no credits.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from ..search.candidates import Candidate
from ..spec import Spec, Visual

log = get_logger("infographic")

#: Emitted as the licence so the credits list stays honest about origin.
LICENCE = "rendered by REDSHIFT"


def _icon_bank() -> Any:
    """An icon bank that reaches the free Magnific library when it can.

    Without a key — the ordinary case on a developer's machine — the bank is
    local-only and every capsule falls back to a drawn monogram. That is a
    difference in polish, never in whether the card renders.
    """
    from redshift.clients.magnific import DEFAULT_DAILY_FREE_DOWNLOADS, DailyQuota, MagnificClient
    from redshift.core.cache import Cache
    from redshift.render.icons import IconBank, magnific_fetcher

    root = Path.cwd()
    if not os.environ.get("MAGNIFIC_API_KEY"):
        return IconBank(root)
    try:
        client = MagnificClient(
            cache=Cache(root / ".cache"),
            stage="s08_infographic",
            quota=DailyQuota(root / ".state" / "magnific-downloads.json", DEFAULT_DAILY_FREE_DOWNLOADS),
        )
    except Exception as exc:
        log.warning("magnific icon lookup unavailable (%s)", exc, extra={"stage": "infographic"})
        return IconBank(root)
    return IconBank(root, fetch=magnific_fetcher(client))


def card_spec_for(visual: Visual, spec: Spec) -> Any | None:
    """Build a `redshift` InfographicSpec from a scenario slot, or None."""
    if visual.type != "infographic" or not visual.card:
        return None
    try:
        from redshift.core.schemas import InfographicItem, InfographicSpec
        from redshift.render.infographics import choose_template
    except ImportError:  # pragma: no cover - both packages ship together
        log.warning("redshift render package unavailable", extra={"stage": "infographic"})
        return None

    card = visual.card
    items = [
        InfographicItem(
            label=str(entry.get("label") or ""),
            value=str(entry.get("value") or ""),
            icon=str(entry.get("icon") or ""),
            note=str(entry.get("note") or ""),
        )
        for entry in card.get("items", [])
    ]
    title = str(card.get("title") or "")
    template = str(card.get("template") or "").strip().upper()
    if not template:
        # The scenario may leave the shape to us; the engine picks from the
        # wording, which is how "X взломал Y" becomes a process arrow.
        template = choose_template(title or visual.query, [entry for entry in card.get("items", [])])

    try:
        return InfographicSpec(
            shot_id=visual.id,
            template=template,
            title=title,
            items=items,
            source=str(card.get("source") or ""),
            accent_word=str(card.get("accent_word") or ""),
            duration_s=float(card.get("duration_s") or 0.0),
        )
    except Exception as exc:  # pydantic validation — a bad card degrades to search
        log.warning("%s: card rejected (%s)", visual.id, exc, extra={"stage": "infographic"})
        return None


def render_card_asset(visual: Visual, spec: Spec, destination: Path) -> tuple[Path, Candidate] | None:
    """Render the slot's card. Returns the file and a candidate describing it.

    Prefers the animated WebM — the arrow should draw itself, the bars should
    grow — and falls back to the still PNG when ffmpeg or a browser is absent.
    Returns None when nothing renderable came out, so the caller can fall back
    to the ordinary search path rather than leaving a hole.
    """
    card = card_spec_for(visual, spec)
    if card is None:
        return None

    try:
        from redshift.core.config import load_config
        from redshift.render.infographics import render_animation, render_card
    except ImportError:  # pragma: no cover
        return None

    try:
        config = load_config()
    except Exception as exc:
        log.warning("%s: brand config unavailable (%s)", visual.id, exc, extra={"stage": "infographic"})
        return None

    rendered = render_card(card, config, destination, icons=_icon_bank())
    path = None
    if rendered.image_path is not None:
        animated = render_animation(rendered, destination)
        path = animated or rendered.image_path
    if path is None or not path.exists():
        for warning in rendered.warnings:
            log.warning("%s: %s", visual.id, warning, extra={"stage": "infographic"})
        return None

    log.info(
        "%s: %s card rendered locally (%s)",
        visual.id,
        card.template,
        path.name,
        extra={"stage": "infographic"},
    )
    candidate = Candidate(
        source="redshift_card",
        external_id=f"card:{visual.id}",
        media_type="video" if path.suffix == ".webm" else "image",
        download_url="",
        page_url="",
        preview_url="",
        title=card.title or visual.query,
        description=f"{card.template} card rendered from the scenario",
        tags=[card.template.lower()],
        width=1080,
        height=1920,
        duration=rendered.duration_s,
        license=LICENCE,
        cost_tokens=0,
    )
    return path, candidate
