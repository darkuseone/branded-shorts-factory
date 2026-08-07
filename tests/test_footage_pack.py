"""Tests for dense footage packing / photo motion defaults."""

from __future__ import annotations

from pathlib import Path

from shorts_factory.search.footage_pack import (
    PHOTO_MOTIONS,
    build_dense_cut_list,
    clamp_duration,
    motion_for_photo,
)


def test_photo_duration_clamped_short() -> None:
    assert clamp_duration("photo", 8.0) <= 2.8
    assert clamp_duration("photo", 0.2) >= 1.2


def test_video_duration_clamped() -> None:
    assert 3.6 <= clamp_duration("video", 3.0) <= 5.0
    assert clamp_duration("video", 12.0) <= 5.0


def test_photo_motion_cycles() -> None:
    assert motion_for_photo(0) in PHOTO_MOTIONS
    assert motion_for_photo(1) != motion_for_photo(0) or len(PHOTO_MOTIONS) == 1


def test_dense_cut_list_uses_staged_stock(tmp_path: Path) -> None:
    stock = tmp_path / "stock"
    stock.mkdir()
    # Minimal valid JPEG header + padding past 32KB gate.
    jpeg = b"\xff\xd8\xff\xd9" + b"\x00" * 40_000
    (stock / "ransomware.jpg").write_bytes(jpeg)
    (stock / "ai_server.jpg").write_bytes(jpeg)
    (tmp_path / "news").mkdir()
    (tmp_path / "news" / "news_hf_attack.jpg").write_bytes(jpeg)
    (tmp_path / "top-02.mp4").write_bytes(b"\x00" * 40_000)

    cuts = build_dense_cut_list(broll_dir=tmp_path, duration=20.0, host_windows=[(0.0, 2.0, "host_full"), (16.0, 20.0, "host_full")])
    assert any(c.kind == "host_full" for c in cuts)
    footage = [c for c in cuts if c.kind in {"photo", "video"}]
    assert footage, "expected footage plates between host windows"
    assert any(c.kind == "photo" and c.motion != "none" for c in footage)
    # Photos should stay snappy.
    for c in footage:
        if c.kind == "photo":
            assert c.duration <= 2.8
