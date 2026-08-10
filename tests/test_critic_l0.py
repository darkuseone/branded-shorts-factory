"""L0 critic (§10.1). DoD: it rejects obviously bad metadata for free."""

from __future__ import annotations

import pytest

from redshift.core.config import load_config
from redshift.core.schemas import Candidate, CriticStats, Shot, Shotlist
from redshift.pipeline.s05_critic import (
    accept_threshold,
    build_report,
    check_l0,
    combine_opinions,
    filter_l0,
    needs_arbitration,
    parse_l1,
)


@pytest.fixture(scope="module")
def config():
    return load_config()


def make_candidate(**kwargs) -> Candidate:
    defaults = dict(
        sha256="a" * 64,
        source="nasa",
        license="public-domain",
        w=2160,
        h=3840,
        duration=8.0,
        motion=0.30,
        sharpness=310.0,
        phash="0f1e2d3c4b5a6978",
    )
    defaults.update(kwargs)
    return Candidate(**defaults)  # type: ignore[arg-type]


def make_shot(**kwargs) -> Shot:
    defaults = dict(
        id="s001",
        beat_id="b01",
        t_start=10.0,
        t_end=12.0,
        mode="MODE_B",
        visual_intent="COSMIC_HERO",
        entity="JWST",
        priority=2,
    )
    defaults.update(kwargs)
    return Shot(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Hard rejects
# --------------------------------------------------------------------------- #


def test_a_good_candidate_passes(config):
    result = check_l0(make_candidate(), make_shot(), config)
    assert result.passed
    assert result.score > 8.0


def test_low_resolution_video_is_rejected(config):
    result = check_l0(make_candidate(w=640, h=360), make_shot(), config)
    assert not result.passed
    assert "short side" in result.rejected_because


def test_photos_are_held_to_a_higher_resolution_bar(config):
    """A still gets ken-burnsed, so it needs more pixels than a video (§10.1)."""
    photo = make_candidate(duration=0.0, motion=0.0, w=1200, h=1600)
    assert not check_l0(photo, make_shot(), config).passed


def test_watermarked_candidate_is_rejected(config):
    result = check_l0(make_candidate(watermark_suspect=True), make_shot(), config)
    assert not result.passed
    assert "watermark" in result.rejected_because


def test_blurry_candidate_is_rejected(config):
    result = check_l0(make_candidate(sharpness=12.0), make_shot(), config)
    assert not result.passed
    assert "sharpness" in result.rejected_because


def test_unstated_rights_are_rejected(config):
    """An archive item with no explicit right is the classic strike risk."""
    assert not check_l0(make_candidate(license=""), make_shot(), config).passed


def test_noncommercial_licence_is_rejected(config):
    assert not check_l0(make_candidate(license="CC-BY-NC"), make_shot(), config).passed


def test_short_busy_clip_cannot_fill_a_long_shot(config):
    busy = make_candidate(duration=0.8, motion=0.9)
    result = check_l0(busy, make_shot(t_start=0.0, t_end=2.5), config)
    assert not result.passed
    assert "cannot fill" in result.rejected_because


def test_short_calm_clip_may_be_looped(config):
    calm = make_candidate(duration=1.5, motion=0.05)
    assert check_l0(calm, make_shot(t_start=0.0, t_end=2.5), config).passed


def test_duplicate_of_an_already_chosen_shot_is_rejected(config):
    """Visual self-repeat inside one video kills the sense of pace (§9.6)."""
    candidate = make_candidate(phash="0f1e2d3c4b5a6978")
    result = check_l0(candidate, make_shot(), config, chosen_phashes=["0f1e2d3c4b5a6979"])
    assert not result.passed
    assert "near-duplicate" in result.rejected_because


# --------------------------------------------------------------------------- #
# Soft penalties
# --------------------------------------------------------------------------- #


def test_static_frame_is_penalised_in_a_dynamic_mode(config):
    static = make_candidate(motion=0.0)
    moving = make_candidate(motion=0.4)
    assert (
        check_l0(static, make_shot(mode="MODE_B"), config).score
        < check_l0(moving, make_shot(mode="MODE_B"), config).score
    )


def test_acid_palette_is_penalised(config):
    acid = make_candidate(l0_flags=["acid:0.42"])
    assert check_l0(acid, make_shot(), config).score < check_l0(make_candidate(), make_shot(), config).score


def test_reuse_from_recent_videos_is_penalised_not_banned(config):
    """A signature frame may come back; it just has to earn it (§9.6)."""
    result = check_l0(make_candidate(), make_shot(), config, history_phashes=["0f1e2d3c4b5a6978"])
    assert result.passed
    assert result.score < 10.0


def test_faces_are_penalised_only_where_they_do_not_belong(config):
    with_faces = make_candidate(faces=2)
    cosmic = check_l0(with_faces, make_shot(visual_intent="COSMIC_HERO"), config)
    human = check_l0(with_faces, make_shot(visual_intent="HUMAN_SCALE"), config)
    assert cosmic.score < human.score


def test_filter_splits_and_ranks(config):
    good = make_candidate(sha256="b" * 64, motion=0.4)
    bad = make_candidate(sha256="c" * 64, watermark_suspect=True)
    weak = make_candidate(sha256="d" * 64, motion=0.0)
    survivors, rejects = filter_l0([weak, bad, good], make_shot(), config)
    assert [item.candidate.sha256 for item in survivors] == ["b" * 64, "d" * 64]
    assert len(rejects) == 1


# --------------------------------------------------------------------------- #
# L1 wire format
# --------------------------------------------------------------------------- #


def test_short_keys_are_expanded(config):
    """§19.E6 asks the model for one-letter keys to save output tokens."""
    reply = '[{"i":1,"r":8,"f":9,"b":7,"w":6,"s":10,"v":"keep","why":"точное попадание","q":null}]'
    verdicts = parse_l1(reply, config)
    assert len(verdicts) == 1
    assert verdicts[0].scores["relevance"] == 8.0
    assert verdicts[0].scores["factual_fit"] == 9.0
    assert verdicts[0].keep


def test_reject_carries_a_better_query(config):
    reply = (
        '[{"i":2,"r":3,"f":1,"b":5,"w":4,"s":10,"v":"reject","why":"это Hubble","q":"JWST golden mirror"}]'
    )
    verdict = parse_l1(reply, config)[0]
    assert not verdict.keep
    assert verdict.better_query == "JWST golden mirror"


def test_weighted_score_uses_the_configured_weights(config):
    reply = '[{"i":1,"r":10,"f":10,"b":10,"w":10,"s":10,"v":"keep"}]'
    weights = dict(config.critic("weights"))
    assert parse_l1(reply, config)[0].weighted(weights) == pytest.approx(10.0)


def test_malformed_reply_yields_no_verdicts_rather_than_guesses(config):
    assert parse_l1("the images look fine to me", config) == []


def test_priority_one_shots_face_a_higher_bar(config):
    assert accept_threshold(make_shot(priority=1), config) > accept_threshold(make_shot(priority=2), config)
    assert accept_threshold(make_shot(), config, fallback=True) < accept_threshold(make_shot(), config)


# --------------------------------------------------------------------------- #
# Gemini arbitration (§10.3) — spend a second opinion only where it pays
# --------------------------------------------------------------------------- #


def test_grey_zone_on_a_hero_shot_gets_arbitration(config):
    verdict = parse_l1('[{"i":1,"r":7,"f":7,"b":7,"w":7,"s":7,"v":"keep"}]', config)[0]
    assert needs_arbitration(verdict, make_shot(priority=1), config)


def test_shaky_factual_fit_on_a_named_entity_gets_arbitration(config):
    """Saying JWST while showing Hubble is the costliest mistake here."""
    verdict = parse_l1('[{"i":1,"r":9,"f":6,"b":9,"w":9,"s":10,"v":"keep"}]', config)[0]
    assert needs_arbitration(verdict, make_shot(entity="JWST", priority=3), config)


def test_a_confident_ordinary_shot_does_not(config):
    verdict = parse_l1('[{"i":1,"r":9,"f":9,"b":9,"w":9,"s":10,"v":"keep"}]', config)[0]
    ordinary = make_shot(entity="", priority=3, t_start=20.0, t_end=22.0)
    assert not needs_arbitration(verdict, ordinary, config)


def test_close_opinions_are_averaged(config):
    assert combine_opinions(8.0, 7.0, config) == pytest.approx(7.5)


def test_contested_candidates_are_dropped(config):
    assert combine_opinions(9.0, 4.0, config) is None, "a contested candidate is not a good one"


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def test_report_marks_unfilled_shots_for_refill():
    shotlist = Shotlist(shots=[make_shot(id="s001"), make_shot(id="s002")], total_duration_s=4.0)
    report = build_report(
        shotlist,
        {
            "s001": {"winner": "sha256:abc", "score": 8.6},
            "s002": {"better_queries": ["martian dust storm 4k"], "reason": "all low quality"},
        },
        CriticStats(l0_rejected=41, l1_evaluated=27, grok_vision_calls=4),
        run_id="r1",
    )
    assert report.refill_needed() == ["s002"]
    assert report.shots[0].needs_refill is False
    assert report.shots[1].better_queries == ["martian dust storm 4k"]
    assert report.stats.grok_vision_calls == 4


def test_report_round_trips(tmp_path):
    shotlist = Shotlist(shots=[make_shot()], total_duration_s=2.0)
    report = build_report(shotlist, {}, CriticStats())
    path = report.save(tmp_path / "critic_report.json")
    from redshift.core.schemas import CriticReport

    assert CriticReport.load(path).shots[0].needs_refill is True
