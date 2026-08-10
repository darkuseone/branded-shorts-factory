"""Infographic slots are rendered, never sourced.

A stock "data visualization" shows somebody else's numbers and a generated one
invents them. Either way the figure on screen contradicts the voiceover, which
is the one mistake this channel cannot make — so these tests pin the routing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shorts_factory.render.infographic_bridge import card_spec_for, render_card_asset
from shorts_factory.spec import Visual


def card_visual(**card) -> Visual:
    payload = {
        "template": "BIG_NUMBER",
        "title": "ExploitGym",
        "items": [{"value": "~900", "label": "задач"}],
        **card,
    }
    return Visual(
        id="v09",
        type="infographic",
        query="benchmark scale",
        keywords=[],
        start=0.0,
        duration=1.9,
        card=payload,
    )


def test_a_card_becomes_a_render_spec():
    spec = card_spec_for(card_visual(), spec=None)
    assert spec is not None
    assert spec.template == "BIG_NUMBER"
    assert spec.items[0].value == "~900"


def test_a_slot_with_no_card_is_left_to_the_search_path():
    visual = Visual(id="v1", type="infographic", query="chart", keywords=[], start=0.0, duration=2.0)
    assert card_spec_for(visual, spec=None) is None


def test_a_card_on_a_footage_slot_is_ignored():
    visual = card_visual()
    visual.type = "footage"
    assert card_spec_for(visual, spec=None) is None


def test_the_template_is_inferred_when_the_scenario_leaves_it_out():
    """«X взломала Y» is the process-arrow shape whether or not it is named."""
    visual = card_visual(
        template="",
        title="OpenAI взломала Hugging Face",
        items=[{"label": "OpenAI"}, {"label": "Hugging Face"}],
    )
    assert card_spec_for(visual, spec=None).template == "PROCESS_ARROW"


def test_an_unreadable_card_degrades_instead_of_raising():
    # §13.3 caps a card at five units; a sixth must not take the run down.
    visual = card_visual(items=[{"value": str(n)} for n in range(6)])
    assert card_spec_for(visual, spec=None) is None


def test_a_rendered_card_is_free_and_carries_its_own_licence(tmp_path):
    rendered = render_card_asset(card_visual(), spec=None, destination=tmp_path)
    if rendered is None:
        pytest.skip("no headless browser on this machine")
    path, candidate = rendered
    assert path.exists() and path.stat().st_size > 1024
    assert candidate.cost_tokens == 0
    assert candidate.license == "rendered by REDSHIFT"
    assert candidate.source == "redshift_card"


def test_the_rendered_card_is_shorts_sized(tmp_path):
    rendered = render_card_asset(card_visual(), spec=None, destination=tmp_path)
    if rendered is None:
        pytest.skip("no headless browser on this machine")
    _, candidate = rendered
    assert (candidate.width, candidate.height) == (1080, 1920)
    assert candidate.duration > 0.0


def test_a_card_whose_render_fails_returns_none_not_a_broken_asset(monkeypatch, tmp_path):
    import shorts_factory.render.infographic_bridge as bridge

    class Dud:
        image_path = None
        warnings = ["no headless browser"]
        duration_s = 1.6

    monkeypatch.setattr(bridge, "_icon_bank", lambda: None)
    monkeypatch.setattr("redshift.render.infographics.render_card", lambda *a, **k: Dud(), raising=True)
    assert render_card_asset(card_visual(), spec=None, destination=Path(tmp_path)) is None
