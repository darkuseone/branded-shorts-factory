"""Host presentation modes (SPLIT / FULL_HOST / FULL_FOOTAGE) — no Pulse Ring."""

from __future__ import annotations

from shorts_factory.render.composition import CompositionWriter
from shorts_factory.render.host_presence import (
    HOST_LOWER_HEIGHT,
    HOST_LOWER_TOP,
    host_lower_layout,
    orbital_arcs_svg,
    plan_host,
)
from shorts_factory.render.timeline import Timeline, build_timeline
from shorts_factory.spec import parse_spec


def _spec(**extra):
    base = {
        "id": "host-modes-test",
        "title": "Host modes",
        "duration_target": 20,
        "hook": {
            "id": "hook",
            "text": "Хук на весь экран.",
            "start": 0,
            "duration": 4.0,
            "mode": "full_host",
            "emphasis": "high",
        },
        "script": [
            {
                "id": "s1",
                "text": "Разделение кадра и футаж сверху.",
                "start": 4.0,
                "duration": 5.0,
                "mode": "split",
            },
            {
                "id": "s2",
                "text": "Только красивый футаж без ведущего.",
                "start": 9.0,
                "duration": 5.0,
                "mode": "full_footage",
            },
            {
                "id": "s3",
                "text": "Снова сплит перед финалом.",
                "start": 14.0,
                "duration": 5.0,
                "mode": "split",
            },
        ],
        "avatar": {"enabled": True, "avatar_id": "look-1"},
        "visuals": [
            {
                "id": "v1",
                "query": "cells",
                "start": 0,
                "duration": 2.0,
                "segment_ref": "hook",
                "position": "auto",
            },
            {
                "id": "v2",
                "query": "dna",
                "start": 4.0,
                "duration": 2.0,
                "segment_ref": "s1",
                "position": "auto",
            },
            {
                "id": "v3",
                "query": "eye",
                "start": 9.0,
                "duration": 2.0,
                "segment_ref": "s2",
                "position": "fullscreen",
            },
            {
                "id": "v4",
                "query": "clock",
                "start": 14.0,
                "duration": 2.0,
                "segment_ref": "s3",
                "position": "auto",
            },
        ],
    }
    base.update(extra)
    spec, _ = parse_spec(base)
    return spec


def test_plan_host_windows_follow_modes():
    plan = plan_host(_spec())
    assert plan.visible
    modes = [w.mode for w in plan.windows]
    assert modes == ["full_host", "split", "full_footage", "split"]
    assert len(plan.present) == 2  # gap at full_footage


def test_avatar_segments_autofilled_from_modes():
    spec = _spec()
    assert "hook" in spec.avatar.segments
    assert "s1" in spec.avatar.segments
    assert "s2" not in spec.avatar.segments
    assert "s3" in spec.avatar.segments


def test_split_lower_band_geometry():
    layout = host_lower_layout()
    assert layout["top"] == HOST_LOWER_TOP
    assert layout["height"] == HOST_LOWER_HEIGHT
    assert layout["width"] == 1080


def test_orbitals_are_thin_not_neon_ring():
    svg = orbital_arcs_svg(primary="#E11D48")
    assert "host-orbitals" in svg
    assert "pulse-ring" not in svg
    assert "stroke-width=\"1" in svg or "stroke-width=\"1.5" in svg


def test_composition_has_host_wrap_no_pulse_ring(tmp_path):
    spec = _spec()
    timeline = Timeline(composition_id="t", title="t", duration=20.0)
    # Minimal empty timeline still writes host CSS when plan is visible.
    writer = CompositionWriter(spec, timeline, tmp_path / "comp", host=plan_host(spec))
    # Inject a fake avatar element so markup path is exercised via write body.
    from shorts_factory.render.timeline import TRACK_AVATAR, Element

    fake = tmp_path / "avatar.mp4"
    fake.write_bytes(b"fake")
    timeline.add(
        Element(
            id="avatar",
            kind="avatar",
            start=0,
            duration=20,
            track=TRACK_AVATAR,
            src=fake,
            props={"layout": {"anchor": "split"}},
        )
    )
    result = writer.write()
    html = result.index_html.read_text(encoding="utf-8")
    assert "host-wrap" in html
    assert "host-orbitals" in html
    assert "pulse-ring" not in html
    assert "pulse_ring" not in html
    assert "text-shadow" in html


def test_mode_stretch_over_12s_errors():
    bad = {
        "id": "too-long-mode",
        "title": "Too long",
        "duration_target": 30,
        "script": [
            {
                "id": "s1",
                "text": "Длинный кусок в одном режиме без смены.",
                "start": 0,
                "duration": 14.0,
                "mode": "split",
            }
        ],
        "visuals": [{"id": "v1", "query": "x", "start": 0, "duration": 2.0}],
        "avatar": {"enabled": True, "avatar_id": "x"},
    }
    try:
        parse_spec(bad)
        raise AssertionError("expected SpecError")
    except Exception as exc:  # SpecError
        text = str(exc)
        assert "12" in text or "mode" in text.lower()


def test_build_timeline_maps_split_to_upper_band():
    from shorts_factory.render.timeline import _resolve_visual_position

    spec = _spec()
    visual = spec.visuals[1]
    assert _resolve_visual_position(spec, visual) == "split_upper"
