"""Magnific-first policy + SFX doppler ban."""

from __future__ import annotations

from pathlib import Path

from shorts_factory.assets.library import LibraryItem, SfxLibrary, _sfx_blacklisted
from shorts_factory.config import Budgets
from shorts_factory.generative.budget import TokenBudget
from shorts_factory.generative.policy import MAGNIFIC_FIRST_TYPES, decide
from shorts_factory.search.aggregator import SearchOutcome
from shorts_factory.search.keywords import QueryPlan
from shorts_factory.spec import Visual


def _outcome(score: float = 0.5) -> SearchOutcome:
    plan = QueryPlan(visual_id="v1", primary="test", queries=["test"])
    return SearchOutcome(visual_id="v1", plan=plan, ranked=[], total_found=0)


def test_infographic_is_magnific_first():
    visual = Visual(
        id="v1",
        type="infographic",
        query="sandbox escape diagram",
        keywords=[],
        start=0,
        duration=4,
        priority="high",
    )
    decision = decide(
        visual,
        _outcome(0.7),
        TokenBudget.from_budgets(Budgets()),
        Budgets(),
        magnific_available=True,
        grok_available=False,
    )
    assert decision.action == "magnific_library"
    assert "magnific-first" in decision.reason
    assert "infographic" in MAGNIFIC_FIRST_TYPES


def test_doppler_file_blacklisted_from_accents(tmp_path: Path):
    path = tmp_path / "passing-car-with-doppler-effect.mp3"
    path.write_bytes(b"\x00" * 32)
    (tmp_path / "index.json").write_text(
        '{"items":[{"file":"passing-car-with-doppler-effect.mp3","tags":["ambience","doppler"],'
        '"analysis":{"shape":"swell","lufs":-6.4}}]}',
        encoding="utf-8",
    )
    item = LibraryItem(
        path=path,
        tags=["ambience", "doppler"],
        analysis={"shape": "swell", "lufs": -6.4},
    )
    assert _sfx_blacklisted(item)
    assert not item.usable_as_accent
    lib = SfxLibrary(tmp_path)
    assert lib.pick("whoosh") is None
