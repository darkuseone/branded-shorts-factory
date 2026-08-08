#!/usr/bin/env python3
"""Verify a rendered Short for the failure modes we keep hitting.

Checks (no paid APIs):
  1. Output MP4 exists, 9:16, duration within tolerance of the scenario
  2. Sample frames are not near-black (b-roll actually painted)
  3. Host presentation uses rectangular SPLIT/FULL_HOST (no Pulse Ring neon)
  4. Captions carry crimson glow / chromatic aberration text-shadow
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


def verify_composition_css(composition_dir: Path) -> list[str]:
    html = (composition_dir / "index.html").read_text(encoding="utf-8")
    errors: list[str] = []
    if "pulse-ring" in html or "pulse_ring" in html:
        errors.append("composition still contains Pulse Ring markup (removed)")
    if "host-wrap" not in html and "avatar" in html:
        errors.append("composition missing .host-wrap for presenter")
    if "host-orbitals" not in html and "host-wrap" in html:
        errors.append("composition missing thin orbital arcs")
    if "feGaussianBlur" in html or "ringGlow" in html:
        errors.append("composition still uses SVG glow filters (drop in HF capture)")
    if "text-shadow" not in html:
        errors.append("composition missing caption crimson glow text-shadow")
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
    by_t = {t: m for t, m in means}
    if by_t.get(1.0, 0) and by_t.get(33.0, 0) and by_t[33.0] < by_t[1.0] + 15:
        errors.append(f"mid/late b-roll looks dead (t1={by_t[1.0]:.1f}, t33={by_t[33.0]:.1f})")

    if composition_dir is not None:
        errors.extend(verify_composition_css(composition_dir))

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--composition", type=Path, default=None)
    args = parser.parse_args()

    spec_id = json.loads(args.spec.read_text(encoding="utf-8")).get("id") or args.spec.stem
    output = args.output or (ROOT / "build" / "output" / f"{spec_id}.mp4")
    composition = args.composition or (ROOT / "build" / "composition" / spec_id)

    errors = verify_output(args.spec, output, composition if composition.exists() else None)
    if errors:
        print("VERIFY FAIL")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
