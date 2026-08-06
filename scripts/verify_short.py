#!/usr/bin/env python3
"""Verify a rendered Short for the failure modes we keep hitting.

Checks (no paid APIs):
  1. Output MP4 exists, 9:16, duration within tolerance of the scenario
  2. Sample frames are not near-black (b-roll actually painted)
  3. Pulse Ring red bloom is present near the avatar anchor
  4. Composition CSS uses CSS neon (box-shadow) + face_zoom, not SVG blur
  5. Optional: re-run until pass with ``--render`` (uses existing local assets)

Exit 0 on pass, 1 on failure. Designed for CI and agent render loops.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ffprobe(path: Path) -> dict[str, float | int]:
    raw = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    payload = json.loads(raw)
    stream = payload["streams"][0]
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "duration": float(payload["format"]["duration"]),
    }


def _frame_rgb(path: Path, t: float) -> bytes:
    return subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )


def _mean(rgb: bytes) -> float:
    return sum(rgb) / max(len(rgb), 1)


def _red_hits(rgb: bytes, width: int, height: int, x0: int, x1: int, y0: int, y1: int) -> int:
    hits = 0
    for y in range(y0, y1, 4):
        for x in range(x0, x1, 4):
            i = (y * width + x) * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if r > 140 and r > g * 1.35 and r > b * 1.35:
                hits += 1
    return hits


def verify_composition_css(composition_dir: Path) -> list[str]:
    html = (composition_dir / "index.html").read_text(encoding="utf-8")
    errors: list[str] = []
    if "pulse-ring-neon" not in html or "pulse-ring-halo" not in html:
        errors.append("composition missing dedicated neon/halo ring layers")
    if "box-shadow" not in html:
        errors.append("composition missing CSS neon box-shadow")
    if "feGaussianBlur" in html or "ringGlow" in html:
        errors.append("composition still uses SVG glow filters (drop in HF capture)")
    if "object-position" not in html:
        errors.append("composition missing object-position for face crop")
    # face_zoom from brandbook should be >1.2 for the close crop
    if "scale(1." not in html and "scale(1," not in html:
        errors.append("composition missing face zoom scale(...)")
    return errors


def verify_output(spec_path: Path, output: Path, composition_dir: Path | None) -> list[str]:
    errors: list[str] = []
    if not output.exists():
        return [f"missing output: {output}"]

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    target = float(spec.get("duration_target") or 0)
    info = _ffprobe(output)
    if info["width"] != 1080 or info["height"] != 1920:
        errors.append(f"expected 1080x1920, got {info['width']}x{info['height']}")
    if target and abs(info["duration"] - target) > 1.5:
        errors.append(f"duration {info['duration']:.2f}s vs target {target:.2f}s")

    # Brightness samples — catch the "only first clip painted" regression.
    samples = [1.0, 5.5, 12.0, 22.0, 33.0, 42.0]
    means = []
    for t in samples:
        if t >= info["duration"] - 0.2:
            continue
        m = _mean(_frame_rgb(output, t))
        means.append((t, m))
    if means and max(m for _, m in means) < 25:
        errors.append(f"all sampled frames near-black: {means}")
    # After the hook, at least one mid sample should be clearly brighter than t≈1
    # when b-roll master is working (AI/SOC slots).
    by_t = {t: m for t, m in means}
    if by_t.get(1.0, 0) and by_t.get(33.0, 0):
        if by_t[33.0] < by_t[1.0] + 15:
            errors.append(
                f"mid/late b-roll looks dead (t1={by_t[1.0]:.1f}, t33={by_t[33.0]:.1f})"
            )

    # Pulse Ring bloom near bottom-right avatar anchor.
    t_ring = 12.0 if info["duration"] > 13 else max(0.5, info["duration"] / 3)
    rgb = _frame_rgb(output, t_ring)
    w, h = int(info["width"]), int(info["height"])
    # Avatar wrap ~ left 408 top 1050 size 576 → sample ring band
    hits = _red_hits(rgb, w, h, 400, 1000, 1050, 1650)
    if hits < 55:
        errors.append(f"pulse ring neon too weak at t={t_ring}s (red hits={hits})")

    # Soft bloom just outside the stroke — flat matte rings fail this.
    bloom = _red_hits(rgb, w, h, 360, 1040, 1000, 1700)
    if bloom < hits:
        # bloom ROI is larger; require meaningful red outside the tight clip band
        pass
    outer = _red_hits(rgb, w, h, 350, 450, 1100, 1600) + _red_hits(rgb, w, h, 950, 1050, 1100, 1600)
    if outer < 8:
        errors.append(f"pulse ring outer bloom missing at t={t_ring}s (outer hits={outer})")

    if composition_dir and composition_dir.exists():
        errors.extend(verify_composition_css(composition_dir))

    return errors


def maybe_render(spec_path: Path) -> int:
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "HYPERFRAMES_DOCKER": "0",
        "HYPERFRAMES_RENDER_ARGS": "--low-memory-mode --workers 1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    # Strip paid keys so we never accidentally burn them in the verify loop.
    for key in (
        "ELEVENLABS_API_KEY",
        "ELEVEN_API_KEY",
        "PEXELS_API_KEY",
        "PIXABAY_API_KEY",
        "GROK_API_KEY",
        "MAGNIFIC_API_KEY",
        "HEYGEN_API_KEY",
    ):
        env.pop(key, None)

    cmd = [
        sys.executable,
        "-m",
        "shorts_factory",
        "render",
        str(spec_path),
    ]
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="scenario JSON")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="rendered mp4 (default: build/output/<id>.mp4)",
    )
    parser.add_argument(
        "--composition",
        type=Path,
        default=None,
        help="composition dir (default: build/composition/<id>)",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="run render first (local assets only; paid API keys stripped)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="with --render, retry verify+render this many times",
    )
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    spec_id = spec["id"]
    output = args.output or (ROOT / "build" / "output" / f"{spec_id}.mp4")
    composition = args.composition or (ROOT / "build" / "composition" / spec_id)

    attempts = args.max_attempts if args.render else 1
    last_errors: list[str] = []
    for attempt in range(1, attempts + 1):
        if args.render:
            print(f"== render attempt {attempt}/{attempts} ==", flush=True)
            code = maybe_render(args.spec)
            if code != 0:
                last_errors = [f"render exited {code}"]
                print("FAIL:", last_errors[0], flush=True)
                continue
        last_errors = verify_output(args.spec, output, composition)
        if not last_errors:
            print(f"OK  {output}")
            return 0
        print(f"FAIL attempt {attempt}:")
        for err in last_errors:
            print(f"  - {err}")
        if not args.render:
            break

    return 1


if __name__ == "__main__":
    sys.exit(main())
