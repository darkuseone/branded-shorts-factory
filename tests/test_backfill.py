"""Filling a slot QA emptied with footage the video already earned.

The degradation ladder promises the video always ships. Its bottom rung was
the brand backdrop, and when eight slots hit it at once the result was sixteen
of forty-six seconds of plain dark background — which reads as the video having
broken, not as a design. These tests describe the rung above it.
"""

from __future__ import annotations

from pathlib import Path

from shorts_factory.media.download import LocalAsset
from shorts_factory.qa.gate import VisualQA
from shorts_factory.render.backfill import MAX_APPEARANCES, backfill_empty_slots
from shorts_factory.resolver import ResolvedVisual

from .test_search import make_candidate, make_visual


def slot(name: str, query: str, *, filled: bool, path: str = "", type_: str = "footage") -> ResolvedVisual:
    visual = make_visual(id=name, query=query, type=type_)
    if not filled:
        return ResolvedVisual(visual=visual, qa=VisualQA(visual_id=name, outcome="rejected"), search=None)
    asset = LocalAsset(candidate=make_candidate(external_id=name), path=Path(path or f"/tmp/{name}.mp4"))
    return ResolvedVisual(
        visual=visual, qa=VisualQA(visual_id=name, outcome="accepted", asset=asset), search=None
    )


def test_an_empty_slot_takes_footage_over_a_blank_plate():
    resolved = [
        slot("v1", "data center servers", filled=True, path="/tmp/a.mp4"),
        slot("v2", "security operations center", filled=False),
        slot("v3", "code on a screen", filled=True, path="/tmp/b.mp4"),
        slot("v4", "server racks humming", filled=True, path="/tmp/c.mp4"),
        slot("v5", "network cables", filled=True, path="/tmp/d.mp4"),
    ]

    notes = backfill_empty_slots(resolved)

    assert resolved[1].usable, "the slot is still empty"
    assert notes and "v2" in notes[0]
    assert any("reusing" in note for note in resolved[1].qa.notes)


def test_a_repeat_never_lands_next_to_itself():
    """Two identical shots back to back is a loop, not an edit."""
    resolved = [
        slot("v1", "servers", filled=True, path="/tmp/a.mp4"),
        slot("v2", "servers", filled=False),
        slot("v3", "code", filled=True, path="/tmp/b.mp4"),
        slot("v4", "cables", filled=True, path="/tmp/c.mp4"),
        slot("v5", "racks", filled=True, path="/tmp/d.mp4"),
    ]

    backfill_empty_slots(resolved)

    assert resolved[1].asset is not None
    neighbours = {str(resolved[0].asset.path), str(resolved[2].asset.path)}
    assert str(resolved[1].asset.path) not in neighbours


def test_a_gap_between_the_only_two_shots_stays_a_gap():
    """When every candidate touches the hole, the backdrop is the right answer.

    Filling it would put the same footage on both sides of a cut, which reads
    as a stutter in the video rather than as an edit. A blank beat is honest;
    a glitch is not.
    """
    resolved = [
        slot("v1", "servers", filled=True, path="/tmp/a.mp4"),
        slot("v2", "servers", filled=False),
        slot("v3", "code", filled=True, path="/tmp/b.mp4"),
    ]

    assert backfill_empty_slots(resolved) == []
    assert not resolved[1].usable


def test_no_shot_appears_more_than_twice():
    resolved = [slot("v1", "servers", filled=True, path="/tmp/a.mp4")]
    resolved += [slot(f"e{i}", "servers", filled=False) for i in range(5)]

    backfill_empty_slots(resolved)

    used = [str(item.asset.path) for item in resolved if item.asset is not None]
    assert used.count("/tmp/a.mp4") <= MAX_APPEARANCES, used


def test_the_repeat_is_cut_differently():
    """A second appearance has to read as a new shot, not the same one again."""
    from shorts_factory.render.backfill import REUSE_MOTION

    resolved = [
        slot("v1", "servers", filled=True, path="/tmp/a.mp4"),
        slot("v2", "network", filled=True, path="/tmp/b.mp4"),
        slot("v3", "cables", filled=True, path="/tmp/c.mp4"),
        slot("v4", "servers", filled=False),
    ]

    backfill_empty_slots(resolved)

    assert resolved[3].usable
    assert resolved[3].visual.motion == REUSE_MOTION


def test_an_empty_card_is_not_papered_over_with_footage():
    """A card that failed to render is a different failure.

    Showing b-roll where a chart belongs hides a broken renderer behind
    something that looks fine, and the numbers the card carried are simply
    gone from the video.
    """
    resolved = [
        slot("v1", "servers", filled=True),
        slot("v2", "breach timeline", filled=False, type_="infographic"),
    ]

    backfill_empty_slots(resolved)

    assert not resolved[1].usable, "an unrendered card was replaced with footage"


def test_nothing_to_reuse_leaves_the_slot_alone():
    resolved = [slot("v1", "servers", filled=False), slot("v2", "code", filled=False)]

    assert backfill_empty_slots(resolved) == []
    assert not any(item.usable for item in resolved)


def test_the_closest_subject_wins():
    resolved = [
        slot("v1", "kitchen table", filled=True, path="/tmp/far.mp4"),
        slot("v2", "server room racks", filled=True, path="/tmp/near.mp4"),
        slot("v3", "spacer", filled=True, path="/tmp/spacer.mp4"),
        slot("v4", "spacer two", filled=True, path="/tmp/spacer2.mp4"),
        slot("v5", "server room", filled=False),
    ]

    backfill_empty_slots(resolved)

    assert str(resolved[4].asset.path) == "/tmp/near.mp4"
