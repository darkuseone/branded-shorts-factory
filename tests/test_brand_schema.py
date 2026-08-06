"""Schema / brandbook wiring: rubric, music pick, external avatar."""

from __future__ import annotations

from pathlib import Path

from shorts_factory.assets.brand import Brandbook, apply_brandbook_to_spec
from shorts_factory.spec import parse_spec
from shorts_factory.voice.heygen import HeyGenClient

from .conftest import minimal_document


def test_rubric_and_sfx_policy_parse():
    spec, _ = parse_spec(
        minimal_document(
            rubric="космос",
            sfx={"policy": {"max_effects": 5, "min_gap": 1.5}},
            ring={"enabled": True, "anchor": "bottom_center", "diameter_ratio": 0.28},
        )
    )
    assert spec.rubric == "космос"
    assert spec.sfx_policy.max_effects == 5
    assert spec.sfx_policy.min_gap == 1.5
    assert spec.ring.anchor == "bottom_center"
    assert spec.ring.diameter_ratio == 0.28


def test_brandbook_fills_music_from_rubric(tmp_path: Path):
    brand_dir = tmp_path / "brand"
    brand_dir.mkdir()
    (brand_dir / "brandbook.json").write_text(
        '{"name":"REDSHIFT","music_by_rubric":{"космос":"Orbital Drift"},'
        '"avatar":{"look_id":"look-123"},"voice":{"voice_id":"voice-abc"},'
        '"colors":{"primary":"#FF2A3C","accent":"#37E4FF","background":"#0A0C10"}}',
        encoding="utf-8",
    )
    book = Brandbook.load(brand_dir)
    assert book is not None
    spec, _ = parse_spec(minimal_document(rubric="космос", music={"track": ""}))
    apply_brandbook_to_spec(spec, book)
    assert spec.music.track == "Orbital Drift"
    assert spec.avatar.avatar_id == "look-123"
    assert spec.voice.voice_id == "voice-abc"
    assert spec.brand.color_primary == "#FF2A3C"


def test_external_avatar_loads_without_api(tmp_path: Path):
    from shorts_factory.config import Budgets, Paths, Settings

    root = tmp_path / "repo"
    (root / "jobs" / "test-short").mkdir(parents=True)
    clip = root / "jobs" / "test-short" / "avatar.mp4"
    clip.write_bytes(b"\x00" * 2048)

    settings = Settings(
        paths=Paths(root=root, workdir=tmp_path / "build").ensure(),
        budgets=Budgets(),
        offline=True,
        dry_run=True,
    )
    spec, _ = parse_spec(minimal_document(id="test-short", avatar={"enabled": True, "provider": "external"}))
    client = HeyGenClient(settings)
    assert client.prefers_external(spec.id, spec.avatar)
    loaded = client.load_external(spec.id, spec.avatar, duration_hint=12.0)
    assert loaded is not None
    assert loaded.source == "external"
    assert loaded.path == clip
    assert loaded.duration == 12.0
