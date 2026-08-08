#!/usr/bin/env python3
"""Bake a dense Aleko-style master bed: host + stock/news + Ken Burns photos.

Usage:
  PYTHONPATH=src python3 scripts/bake_aleko_master.py jobs/openai-huggingface-hack
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from shorts_factory.search.footage_pack import FootageCut, build_dense_cut_list, cuts_to_json

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 1080, 1920, 30


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def make_news_cards(out_dir: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    cards = [
        (
            "news_hf_attack.jpg",
            "BREAKING",
            "Hugging Face: 5 дней\nслепой обороны",
            "Внешний ИИ-агент? Источник неизвестен.",
        ),
        (
            "news_openai_admit.jpg",
            "OPENAI",
            "Это были наши модели\nвключая GPT-5.6 Sol",
            "ExploitGym cyber evaluation · July 2026",
        ),
        (
            "news_zeroday.jpg",
            "ZERO-DAY",
            "Proxy escape →\nпубличный интернет",
            "Песочница ExploitGym пробита",
        ),
        (
            "news_cheat.jpg",
            "DATASET",
            "Читерская логика:\nответы на Hugging Face",
            "Тысячи коротких действий в проде",
        ),
    ]
    for name, badge, title, sub in cards:
        img = Image.new("RGB", (W, H), (8, 10, 14))
        draw = ImageDraw.Draw(img)
        # Atmosphere bars
        draw.rectangle([0, 0, W, 160], fill=(180, 20, 36))
        draw.rectangle([0, H - 220, W, H], fill=(12, 16, 22))
        try:
            font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
            font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except OSError:
            font_badge = font_title = font_sub = ImageFont.load_default()
        draw.text((72, 55), badge, fill=(255, 255, 255), font=font_badge)
        draw.multiline_text((72, 420), title, fill=(255, 255, 255), font=font_title, spacing=18)
        draw.text((72, H - 140), sub, fill=(180, 190, 200), font=font_sub)
        # Fake article body lines
        y = 720
        for _ in range(8):
            draw.rectangle([72, y, W - 72, y + 18], fill=(32, 38, 48))
            y += 46
        img.save(out_dir / name, quality=92)


def host_vf(layout: str) -> str:
    # Crop letterboxed avatar_close into a filled 9:16 / bottom half.
    if layout == "host_full":
        return (
            "crop=1080:1080:0:420,"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={FPS},setsar=1"
        )
    return (
        "crop=1080:1080:0:420,"
        "scale=1400:1400,"
        f"crop={W}:{H // 2}:160:120,fps={FPS},setsar=1"
    )


def photo_vf(motion: str, duration: float) -> str:
    frames = max(2, int(duration * FPS))
    # zoompan on a covered canvas
    base = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},"
    )
    if motion == "parallax":
        z = f"z='1.08+0.06*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)-20*on/{frames}'"
    elif motion == "zoom_in":
        z = f"z='1.0+0.18*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif motion == "pan_left":
        z = f"z='1.14':x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'"
    elif motion == "pan_right":
        z = f"z='1.14':x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'"
    else:  # kenburns
        z = f"z='1.04+0.12*on/{frames}':x='iw/2-(iw/zoom/2)-12*on/{frames}':y='ih/2-(ih/zoom/2)-8*on/{frames}'"
    return base + f"zoompan={z}:d={frames}:s={W}x{H}:fps={FPS},setsar=1"


def render_cut(cut: FootageCut, avatar: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if cut.kind in {"host_full", "host_split"}:
        vf = host_vf("host_full" if cut.kind == "host_full" else "split")
        # For split host-only half we still output full frame later with top; here host_full segments
        # are true fullscreen. host_split placeholders are replaced in bake_split_window.
        run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{cut.start:.3f}",
                "-i",
                str(avatar),
                "-t",
                f"{cut.duration:.3f}",
                "-vf",
                vf if cut.kind == "host_full" else host_vf("host_full"),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(dest),
            ]
        )
        return

    src = Path(cut.path)
    if cut.kind == "photo":
        vf = photo_vf(cut.motion, cut.duration)
        run(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(src),
                "-t",
                f"{cut.duration:.3f}",
                "-vf",
                vf,
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(dest),
            ]
        )
        return

    # video — loop if shorter than slot
    run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(src),
            "-t",
            f"{cut.duration:.3f}",
            "-vf",
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ]
    )


def bake_split_segment(
    *,
    avatar: Path,
    top_src: Path,
    top_is_photo: bool,
    motion: str,
    host_ss: float,
    duration: float,
    dest: Path,
    work: Path,
) -> None:
    top = work / f"_split_top_{dest.stem}.mp4"
    bottom = work / f"_split_host_{dest.stem}.mp4"
    if top_is_photo:
        run(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(top_src),
                "-t",
                f"{duration:.3f}",
                "-vf",
                photo_half_vf(motion, duration),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(top),
            ]
        )
    else:
        run(
            [
                "ffmpeg",
                "-y",
                "-stream_loop",
                "-1",
                "-i",
                str(top_src),
                "-t",
                f"{duration:.3f}",
                "-vf",
                f"scale={W}:{H // 2}:force_original_aspect_ratio=increase,crop={W}:{H // 2},fps={FPS},setsar=1",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(top),
            ]
        )
    run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{host_ss:.3f}",
            "-i",
            str(avatar),
            "-t",
            f"{duration:.3f}",
            "-vf",
            host_vf("split"),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(bottom),
        ]
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(top),
            "-i",
            str(bottom),
            "-filter_complex",
            "[0:v][1:v]vstack=inputs=2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ]
    )


def photo_half_vf(motion: str, duration: float) -> str:
    frames = max(2, int(duration * FPS))
    base = f"scale={W}:{H // 2}:force_original_aspect_ratio=increase,crop={W}:{H // 2},"
    if motion == "parallax":
        z = f"z='1.08+0.05*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif motion == "zoom_in":
        z = f"z='1.0+0.14*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    else:
        z = f"z='1.04+0.10*on/{frames}':x='iw/2-(iw/zoom/2)-8*on/{frames}':y='ih/2-(ih/zoom/2)'"
    return base + f"zoompan={z}:d={frames}:s={W}x{H // 2}:fps={FPS},setsar=1"


def main() -> int:
    job = Path(sys.argv[1] if len(sys.argv) > 1 else "jobs/openai-huggingface-hack")
    if not job.is_absolute():
        job = ROOT / job
    broll = job / "broll"
    avatar = job / "avatar_close.mp4"
    work = Path("/tmp/aleko-dense")
    work.mkdir(parents=True, exist_ok=True)

    make_news_cards(broll / "news")
    cuts = build_dense_cut_list(broll_dir=broll, duration=42.0)
    (work / "cuts.json").write_text(json.dumps(cuts_to_json(cuts), indent=2), encoding="utf-8")
    print(f"cuts={len(cuts)}")

    # Expand split host windows into short top plates + host bottom.
    from shorts_factory.search.footage_pack import IMAGE_EXTS, list_staged_assets, pick_for_themes

    assets = list_staged_assets(broll)
    segment_files: list[Path] = []
    used: set[str] = set()
    idx = 0
    for cut in cuts:
        dest = work / f"seg_{idx:03d}.mp4"
        if cut.kind == "host_split":
            # Rapid 2–3 top plates across the split window.
            t = 0.0
            while t < cut.duration - 0.2:
                piece = min(2.2, cut.duration - t)
                prefer = "photo" if (idx + int(t)) % 2 == 0 else "video"
                asset = pick_for_themes(
                    assets,
                    ["ai", "hacker", "access", "neon", "network", "ransomware"],
                    used=used,
                    prefer=prefer,
                ) or pick_for_themes(assets, ["cyber", "ai"], used=used)
                if asset is None:
                    break
                used.add(str(asset))
                piece_dest = work / f"seg_{idx:03d}.mp4"
                bake_split_segment(
                    avatar=avatar,
                    top_src=asset,
                    top_is_photo=asset.suffix.lower() in IMAGE_EXTS,
                    motion="kenburns",
                    host_ss=cut.start + t,
                    duration=piece,
                    dest=piece_dest,
                    work=work,
                )
                segment_files.append(piece_dest)
                t += piece
                idx += 1
            continue

        render_cut(cut, avatar, dest)
        segment_files.append(dest)
        idx += 1

    concat = work / "list.txt"
    concat.write_text("".join(f"file '{p}'\n" for p in segment_files), encoding="utf-8")
    out = broll / "aleko-master.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            "-an",
            str(out),
        ]
    )
    # Trim / pad to exactly 42s
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        text=True,
    ).strip()
    dur = float(probe or 0.0)
    if abs(dur - 42.0) > 0.15:
        padded = work / "aleko-master-42.mp4"
        if dur < 42.0:
            pad = 42.0 - dur
            vf = f"tpad=stop_mode=clone:stop_duration={pad:.3f}"
        else:
            vf = "trim=duration=42,setpts=PTS-STARTPTS"
        run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(out),
                "-vf",
                vf,
                "-t",
                "42",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(FPS),
                str(padded),
            ]
        )
        padded.replace(out)
        probe = "42.000000"
    print("baked", out, "duration", probe, "segments", len(segment_files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
