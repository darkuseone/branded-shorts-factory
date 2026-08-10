from __future__ import annotations

import pytest

from shorts_factory.config import Settings
from shorts_factory.errors import ProviderError
from shorts_factory.search.aggregator import SearchAggregator, media_type_for
from shorts_factory.search.candidates import Candidate
from shorts_factory.search.keywords import (
    MAX_QUERIES_PER_VISUAL,
    build_query_plan,
    is_cyrillic,
    keywords_from_text,
    translate_term,
)
from shorts_factory.search.providers.base import StockProvider, pick_largest
from shorts_factory.search.ranking import rank_candidates
from shorts_factory.spec import Visual


def make_candidate(**kwargs) -> Candidate:
    defaults = dict(
        source="pexels",
        external_id="1",
        media_type="video",
        download_url="https://example.com/clip.mp4",
        title="planet venus in space",
        tags=["planet", "venus", "space"],
        width=1080,
        height=1920,
        duration=12.0,
    )
    defaults.update(kwargs)
    return Candidate(**defaults)  # type: ignore[arg-type]


def make_visual(**kwargs) -> Visual:
    defaults = dict(
        id="v1",
        type="footage",
        query="venus planet",
        keywords=["venus surface"],
        start=0.0,
        duration=5.0,
    )
    defaults.update(kwargs)
    return Visual(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Query expansion
# --------------------------------------------------------------------------- #


def test_query_plan_fans_out_without_duplicates(example_spec):
    visual = example_spec.visuals[0]
    plan = build_query_plan(visual, example_spec)
    assert len(plan.queries) >= 3
    assert len(plan.queries) <= MAX_QUERIES_PER_VISUAL
    assert len(plan.queries) == len({query.lower() for query in plan.queries})
    assert plan.queries[0] == "venus planet"


def test_query_plan_adds_type_specific_modifiers():
    plan = build_query_plan(make_visual(type="infographic", query="market growth", keywords=[]))
    assert any("data visualization" in query or "chart animation" in query for query in plan.queries)


def test_cyrillic_queries_are_translated_for_english_first_apis():
    plan = build_query_plan(make_visual(query="космос планета", keywords=[]))
    assert plan.primary == "space planet"
    assert not plan.warnings


def test_untranslatable_cyrillic_query_warns():
    plan = build_query_plan(make_visual(query="абракадабра штуковина", keywords=[]))
    assert plan.warnings
    assert "could not translate" in plan.warnings[0] or "no English query" in plan.warnings[0]


def test_translate_term_handles_inflections():
    assert translate_term("планеты") == "planets"
    assert translate_term("телескопом") == "telescope"
    assert translate_term("unknownword") == "unknownword"
    assert translate_term("щщщ") is None


def test_is_cyrillic():
    assert is_cyrillic("планета")
    assert not is_cyrillic("planet")


def test_keywords_from_text_drops_stopwords():
    words = keywords_from_text("The telescope and the galaxy are in the sky", limit=3)
    assert "the" not in words
    assert "telescope" in words


def test_must_include_terms_reach_the_plan():
    plan = build_query_plan(make_visual(must_include=["temperature"]))
    assert any("temperature" in query for query in plan.queries)
    assert plan.must_include == ["temperature"]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_vertical_hd_candidate_outranks_small_landscape():
    visual = make_visual()
    plan = build_query_plan(visual)
    good = make_candidate(external_id="good", width=1080, height=1920)
    weak = make_candidate(external_id="weak", width=640, height=360, title="unrelated office desk", tags=[])
    ranked = rank_candidates([weak, good], visual, plan)
    assert ranked[0].candidate.external_id == "good"
    assert ranked[0].score > ranked[1].score


def test_multi_query_matches_boost_relevance():
    visual = make_visual()
    plan = build_query_plan(visual)
    single = make_candidate(external_id="single", matched_queries=["venus planet"])
    multi = make_candidate(
        external_id="multi", matched_queries=["venus planet", "venus surface", "solar system"]
    )
    ranked = rank_candidates([single, multi], visual, plan)
    assert ranked[0].candidate.external_id == "multi"


def test_banned_term_sinks_a_candidate():
    visual = make_visual(must_avoid=["cartoon"])
    plan = build_query_plan(visual)
    clean = make_candidate(external_id="clean")
    banned = make_candidate(external_id="banned", title="cartoon planet venus in space")
    ranked = rank_candidates([banned, clean], visual, plan)
    assert ranked[0].candidate.external_id == "clean"


def test_short_clip_is_penalised_for_a_long_slot():
    visual = make_visual(duration=10.0)
    plan = build_query_plan(visual)
    long_clip = make_candidate(external_id="long", duration=12.0)
    short_clip = make_candidate(external_id="short", duration=2.0)
    ranked = rank_candidates([short_clip, long_clip], visual, plan)
    assert ranked[0].candidate.external_id == "long"


def test_ranking_is_deterministic_for_equal_scores():
    visual = make_visual()
    plan = build_query_plan(visual)
    a = make_candidate(external_id="aaa")
    b = make_candidate(external_id="bbb")
    first = [item.candidate.key for item in rank_candidates([a, b], visual, plan)]
    second = [item.candidate.key for item in rank_candidates([b, a], visual, plan)]
    assert first == second


def test_pick_largest_prefers_vertical():
    variants = [
        {"width": 1920, "height": 1080, "url": "landscape"},
        {"width": 1080, "height": 1920, "url": "portrait"},
    ]
    assert pick_largest(variants)["url"] == "portrait"


def test_media_type_follows_visual_type():
    assert media_type_for(make_visual(type="footage")) == "video"
    assert media_type_for(make_visual(type="infographic")) == "image"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


class FakeProvider(StockProvider):
    name = "pexels"
    media_types = ("video", "image")

    def __init__(self, settings: Settings, results: dict[str, list[Candidate]], fail: bool = False):
        super().__init__(settings)
        self.results = results
        self.fail = fail
        self.queries: list[str] = []

    def is_available(self) -> bool:
        return True

    def search(self, query, media_type, limit=10):
        self.queries.append(query)
        if self.fail:
            raise ProviderError(self.name, "boom")
        return self.results.get(query, [])


class BrokenProvider(FakeProvider):
    name = "pixabay"

    def search(self, query, media_type, limit=10):
        raise ValueError("malformed payload")


def test_aggregator_searches_every_query_and_dedupes(settings):
    visual = make_visual()
    plan = build_query_plan(visual)
    shared = make_candidate(external_id="shared")
    provider = FakeProvider(
        settings, {query: [make_candidate(external_id="shared")] for query in plan.queries}
    )
    aggregator = SearchAggregator(settings, providers=[provider])

    outcome = aggregator.search_visual(visual)

    assert sorted(provider.queries) == sorted(plan.queries)
    assert outcome.total_found == len(plan.queries)
    assert len(outcome.ranked) == 1, "the same asset from several queries collapses into one"
    assert len(outcome.ranked[0].candidate.matched_queries) == len(plan.queries)
    assert shared.key == outcome.ranked[0].candidate.key


def test_aggregator_survives_a_failing_provider(settings):
    visual = make_visual()
    good = FakeProvider(settings, {query: [make_candidate()] for query in build_query_plan(visual).queries})
    bad = BrokenProvider(settings, {})
    aggregator = SearchAggregator(settings, providers=[bad, good])

    outcome = aggregator.search_visual(visual)

    assert outcome.ranked, "a broken provider must not empty the shortlist"
    assert "pixabay" in outcome.provider_errors


def test_unavailable_providers_are_recorded(settings):
    visual = make_visual()
    aggregator = SearchAggregator(settings)  # offline settings -> nothing available
    outcome = aggregator.search_visual(visual)
    assert outcome.ranked == []
    assert outcome.skipped_providers


def test_source_hint_restricts_providers(settings):
    visual = make_visual(source_hint="pixabay")
    provider = FakeProvider(settings, {})
    aggregator = SearchAggregator(settings, providers=[provider])
    outcome = aggregator.search_visual(visual)
    assert provider.queries == []
    assert outcome.total_found == 0


@pytest.mark.parametrize("media_type", ["video", "image"])
def test_candidate_filename_matches_media_type(media_type):
    candidate = make_candidate(media_type=media_type, download_url="https://example.com/asset")
    suffix = ".mp4" if media_type == "video" else ".jpg"
    assert candidate.suggested_filename.endswith(suffix)


# --------------------------------------------------------------------------- #
# Which rendition we actually download
# --------------------------------------------------------------------------- #


def test_an_8k_master_is_never_downloaded_for_a_1080_short():
    """Filling slots took 65 of a 115-minute budget, almost all of it moving
    pixels that the downscale throws away."""
    from shorts_factory.search.providers.base import pick_best_fit

    variants = [
        {"width": 1280, "height": 720},
        {"width": 3840, "height": 2160},
        {"width": 7680, "height": 4320},
    ]
    assert pick_best_fit(variants)["height"] == 2160


def test_a_vertical_source_wins_over_a_bigger_landscape_one():
    from shorts_factory.search.providers.base import pick_best_fit

    variants = [{"width": 3840, "height": 2160}, {"width": 1080, "height": 1920}]
    best = pick_best_fit(variants)
    assert (best["width"], best["height"]) == (1080, 1920), "a 9:16 source needs no crop at all"


def test_the_tallest_is_taken_when_nothing_reaches_the_target():
    from shorts_factory.search.providers.base import pick_best_fit

    variants = [{"width": 640, "height": 360}, {"width": 1280, "height": 720}]
    assert pick_best_fit(variants)["height"] == 720


def test_no_variants_is_not_a_crash():
    from shorts_factory.search.providers.base import pick_best_fit

    assert pick_best_fit([]) is None
    assert pick_best_fit([{"link": "x"}]) is None
