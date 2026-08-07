"""Pulse Ring state machine — brand-book states driven by the scenario."""

from __future__ import annotations

from shorts_factory.render.ring import (
    RingConfig,
    layout_box,
    plan_ring,
    ring_css,
    ring_svg,
)
from shorts_factory.spec import (
    AvatarSettings,
    BrandElements,
    CaptionSettings,
    Constraints,
    MemeSettings,
    MusicSettings,
    ScriptSegment,
    Spec,
    Visual,
    VoiceSettings,
)


def _spec(*, avatar_segments=None, duration=40.0) -> Spec:
    hook = ScriptSegment(id="hook", text="хук", start=0.0, duration=3.0, emphasis="high")
    body = [
        ScriptSegment(id="s1", text="контекст", start=3.0, duration=5.0),
        ScriptSegment(id="s2", text="ядро", start=8.0, duration=5.0, emphasis="high"),
        ScriptSegment(id="s3", text="развитие", start=13.0, duration=6.0),
        ScriptSegment(id="s4", text="вывод", start=25.0, duration=5.0, emphasis="high"),
    ]
    visuals = [
        Visual(id="v1", type="footage", query="a", keywords=[], start=0.0, duration=3.0, priority="critical"),
        Visual(id="v2", type="footage", query="b", keywords=[], start=3.0, duration=5.0),
        Visual(
            id="v3",
            type="motion_graphics",
            query="c",
            keywords=[],
            start=8.0,
            duration=5.0,
            priority="high",
        ),
        Visual(id="v4", type="footage", query="d", keywords=[], start=13.0, duration=12.0),
        Visual(
            id="v5",
            type="infographic",
            query="e",
            keywords=[],
            start=25.0,
            duration=5.0,
            priority="high",
        ),
    ]
    return Spec(
        id="ring-test",
        title="Ring",
        duration_target=duration,
        language="ru",
        topic="space",
        hook=hook,
        script=body,
        voice=VoiceSettings(),
        avatar=AvatarSettings(enabled=True, segments=avatar_segments or ["hook", "s4"]),
        visuals=visuals,
        music=MusicSettings(),
        audio_fx=[],
        captions=CaptionSettings(),
        brand=BrandElements(),
        cta=None,
        memes=MemeSettings(),
        constraints=Constraints(),
    )


def test_ring_windows_follow_avatar_segments():
    plan = plan_ring(_spec(avatar_segments=["hook", "s4"]))
    assert plan.visible
    assert len(plan.windows) == 2
    assert plan.windows[0][0] == 0.0
    assert plan.windows[1][0] == 25.0


def test_exit_and_return_events_bridge_the_gap():
    plan = plan_ring(_spec(avatar_segments=["hook", "s4"]))
    states = {event.state for event in plan.events}
    assert "exit" in states
    assert "return_" in states


def test_emphasis_pulses_only_while_the_ring_is_visible():
    plan = plan_ring(_spec(avatar_segments=["hook", "s4"]))
    emphasis = [event for event in plan.events if event.state == "emphasis"]
    # hook + s4 are visible; s2 at 8s falls in the hidden gap and must not pulse.
    assert all(event.at < 4 or event.at >= 25 for event in emphasis)
    assert any(event.at < 4 for event in emphasis)


def test_transition_on_topic_change_while_visible():
    # Avatar covers the whole run so the motion_graphics cut at 8s earns a transition.
    plan = plan_ring(_spec(avatar_segments=None))
    transitions = [event for event in plan.events if event.state == "transition"]
    assert transitions


def test_layout_box_respects_safe_margins():
    box = layout_box(RingConfig(diameter_ratio=0.30))
    assert box["size"] == 576  # 0.30 * 1920
    # Extra right margin so neon bloom isn't clipped by the stage edge.
    assert box["left"] + box["size"] <= 1080 - 140
    assert box["left"] >= 96


def test_svg_contains_signal_red_stroke():
    svg = ring_svg(RingConfig(stroke="#FF2A3C"), size=200)
    assert "#FF2A3C" in svg
    assert "#B4001E" in svg  # Deep Red shadow rim
    assert "pulse-ring-stroke" in svg
    assert "pulse-ring-shadow" in svg
    # Neon bloom is CSS box-shadow — SVG filters drop out in HF capture.
    assert "feGaussianBlur" not in svg
    assert "ringGlow" not in svg


def test_css_keyframes_span_the_composition():
    plan = plan_ring(_spec())
    css = ring_css(plan, duration=40.0)
    assert "pulse_ring_life" in css
    assert "40.000s" in css


def test_gap_keyframes_stay_hidden_after_exit():
    """Transition/emphasis must not leave scale(1) across an avatar gap.

    Regression: a topic-change transition at the EXIT boundary used to overwrite
    the hide stop, so linear interpolation kept the ring visible for the gap.
    """
    plan = plan_ring(_spec(avatar_segments=["hook", "s4"]))
    assert plan.windows == [(0.0, 3.0), (25.0, 30.0)]
    css = ring_css(plan, duration=40.0)
    import re

    match = re.search(r"@keyframes pulse_ring_life\{(.*)\}\n\.pulse-ring-wrap", css, re.S)
    assert match
    stops = [(float(p), body) for p, body in re.findall(r"([\d.]+)%\{([^}]*)\}", match.group(1))]
    # Mid-gap (~14s → 35%): must be fully hidden.
    mid_pct = 35.0
    before = [s for s in stops if s[0] <= mid_pct]
    after = [s for s in stops if s[0] >= mid_pct]
    assert before and after
    assert "scale(0.000)" in before[-1][1]
    assert "scale(0.000)" in after[0][1]
    # No scale(1) stop strictly inside the gap (3.25s .. 24.75s → 8.125% .. 61.875%).
    gap_stops = [body for pct, body in stops if 9.0 < pct < 61.0]
    assert gap_stops
    assert all("scale(0.000)" in body for body in gap_stops)


def test_transition_skipped_when_it_would_spill_past_exit():
    # Avatar only on hook; motion_graphics cut at 8s is in the gap → no transition.
    plan = plan_ring(_spec(avatar_segments=["hook"]))
    transitions = [event for event in plan.events if event.state == "transition"]
    assert not transitions


def test_disabled_avatar_yields_no_ring():
    spec = _spec()
    spec.avatar.enabled = False
    assert not plan_ring(spec).visible


def test_brandbook_config_overrides_defaults():
    cfg = RingConfig.from_brandbook(
        {
            "diameter_ratio": 0.32,
            "stroke": "#FF2A3C",
            "face_zoom": 1.25,
            "face_position": "center 28%",
            "scale_y": 1.15,
            "lightning": True,
            "states": {"idle": {"cycle_s": 2.2}},
        }
    )
    assert cfg.diameter_ratio == 0.32
    assert cfg.face_zoom == 1.25
    assert cfg.face_position == "center 28%"
    assert cfg.scale_y == 1.15
    assert cfg.lightning is True
    assert cfg.idle_cycle_s == 2.2


def test_lightning_overlay_uses_safe_class():
    from shorts_factory.render.ring import lightning_overlay_svg

    svg = lightning_overlay_svg(RingConfig(lightning=True), size=200)
    assert "ring-lightning" in svg
    assert "ring-network" not in svg
    assert "#FF2A3C" in svg
