#!/usr/bin/env python3
"""Long write → run → diagnose → fix-hint → continue loop for Short renders.

Designed for Cloud Agents and CI:

  1. RENDER  — local assets only (paid API keys stripped)
  2. VERIFY  — ``scripts/verify_short.py`` checks
  3. DIAGNOSE — map failures to known root causes + next fix
  4. REPORT  — write ``build/reports/<id>.loop.json`` for the agent
  5. RETRY   — up to ``--max-attempts`` (agent edits code between attempts)

Agents should: read the loop report → patch code → re-run this script.
Do not burn Magnific / HeyGen / ElevenLabs / stock API keys in this loop.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PAID_KEYS = (
    "ELEVENLABS_API_KEY",
    "ELEVEN_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "GROK_API_KEY",
    "MAGNIFIC_API_KEY",
    "HEYGEN_API_KEY",
    "POND5_API_KEY",
)

# Failure substring → (category, fix hint for the next agent turn)
DIAGNOSIS: list[tuple[str, str, str]] = [
    (
        "left-edge red ghost",
        "logo_stretch",
        "Logo img used height:auto — HF stretches orbital arc full-height. "
        "Pin logo to width/height 160px + object-fit:contain. Re-verify left strip.",
    ),
    (
        "ring-network SVG",
        "network_svg_ghost",
        "Do not mount ring-network SVG (display:none still paints in HF). "
        "Remove from composition HTML entirely.",
    ),
    (
        "feGaussianBlur",
        "svg_glow_dropped",
        "SVG filters die in HF capture. Use CSS box-shadow / radial-gradient neon "
        "on .pulse-ring-neon + .pulse-ring-halo only.",
    ),
    (
        "neon too weak",
        "flat_ring",
        "Strengthen .pulse-ring-neon box-shadow + halo; keep ring ≥140px from stage "
        "right edge so bloom is not clipped.",
    ),
    (
        "outer bloom missing",
        "flat_ring",
        "Neon corona clipped or missing. Check right margin and halo inset.",
    ),
    (
        "face zoom",
        "video_transform_ignored",
        "HF ignores CSS transform on <video>. Prefer jobs/<id>/avatar_close.mp4 "
        "(ffmpeg crop) and/or .avatar-face wrapper around the video.",
    ),
    (
        "object-position",
        "video_transform_ignored",
        "Missing face crop CSS. Ensure .avatar-face + object-position in composition.",
    ),
    (
        "overlapping_clips_same_track",
        "timeline_overlap",
        "HyperFrames forbids same-track overlap. Keep top tablets on TRACK_OVERLAY "
        "and data chips on TRACK_CHIPS; memes stay on TRACK_MEME.",
    ),
    (
        "SystemMemory",
        "system_memory_noise",
        "cgroup SystemMemory stderr alone is not a real lint failure — runner should "
        "ignore it. If render still fails, look for overlapping_clips or real lint ✗.",
    ),
    (
        "near-black",
        "dead_broll",
        "B-roll not painting past first slot. Bake a single jobs/<id>/broll/broll.mp4 "
        "master track and reference one visual id.",
    ),
    (
        "b-roll looks dead",
        "dead_broll",
        "Mid/late frames as dark as hook — sequential fullscreen clips bug. Use baked master.",
    ),
    (
        "missing output",
        "render_failed",
        "HyperFrames did not produce MP4. Check lint/check logs; try "
        "HYPERFRAMES_DOCKER=0 and --low-memory-mode --workers 1.",
    ),
    (
        "render exited",
        "render_failed",
        "Render process crashed. Inspect build/composition and hyperframes stderr.",
    ),
    (
        "expected 1080x1920",
        "wrong_canvas",
        "Output aspect wrong — check VIDEO_WIDTH/HEIGHT and HF window size.",
    ),
    (
        "duration",
        "duration_mismatch",
        "Output length ≠ scenario duration_target. Check avatar/voice trim and timeline.",
    ),
]


def _strip_paid(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    for key in PAID_KEYS:
        out.pop(key, None)
    out["HYPERFRAMES_DOCKER"] = out.get("HYPERFRAMES_DOCKER") or "0"
    out.setdefault("HYPERFRAMES_RENDER_ARGS", "--low-memory-mode --workers 1")
    out["PYTHONPATH"] = str(ROOT / "src")
    return out


def _diagnose(errors: list[str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    seen: set[str] = set()
    for err in errors:
        matched = False
        for needle, category, hint in DIAGNOSIS:
            if needle.lower() in err.lower():
                if category not in seen:
                    findings.append({"category": category, "error": err, "fix": hint})
                    seen.add(category)
                matched = True
                break
        if not matched:
            findings.append(
                {
                    "category": "unknown",
                    "error": err,
                    "fix": "Read composition HTML + sample frames; fix root cause; re-run loop.",
                }
            )
    return findings


def _run_verify(spec: Path, *, render: bool, max_attempts: int) -> tuple[int, list[str]]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "verify_short.py"),
        str(spec),
        "--max-attempts",
        str(max_attempts),
    ]
    if render:
        cmd.append("--render")
    env = _strip_paid(dict(os.environ))
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    errors: list[str] = []
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        line = line.strip()
        if line.startswith("- "):
            errors.append(line[2:])
        elif line.startswith("FAIL:") or line.startswith("FAIL "):
            errors.append(line.split(":", 1)[-1].strip() if ":" in line else line)
    return proc.returncode, errors


def _write_report(
    path: Path,
    *,
    spec_id: str,
    attempt: int,
    max_attempts: int,
    ok: bool,
    errors: list[str],
    findings: list[dict[str, str]],
    elapsed_s: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": spec_id,
        "ok": ok,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "elapsed_s": round(elapsed_s, 1),
        "errors": errors,
        "findings": findings,
        "next_steps": [
            "1. Apply the fix hints in findings[].fix (code only — no paid APIs).",
            "2. Commit the patch.",
            "3. Re-run: python3 scripts/render_fix_loop.py <spec> --render --max-attempts N",
            "4. Repeat until ok=true or attempts exhausted.",
        ]
        if not ok
        else ["Verify passed. Copy build/output/<id>.mp4 to artifacts and update the PR."],
        "paid_apis": "stripped",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"loop report → {path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="scenario JSON")
    parser.add_argument(
        "--render",
        action="store_true",
        help="render before verify (local assets; paid keys stripped)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="verify_short render retries inside one loop invocation",
    )
    parser.add_argument(
        "--outer-attempts",
        type=int,
        default=1,
        help="how many times THIS script invokes verify (agent usually owns outer loop)",
    )
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    spec_id = spec["id"]
    report_path = ROOT / "build" / "reports" / f"{spec_id}.loop.json"

    last_errors: list[str] = []
    last_findings: list[dict[str, str]] = []
    started = time.time()

    for outer in range(1, args.outer_attempts + 1):
        print(f"== loop outer {outer}/{args.outer_attempts} ==", flush=True)
        code, errors = _run_verify(
            args.spec, render=args.render, max_attempts=args.max_attempts
        )
        findings = _diagnose(errors) if code != 0 else []
        last_errors, last_findings = errors, findings
        _write_report(
            report_path,
            spec_id=spec_id,
            attempt=outer,
            max_attempts=args.outer_attempts,
            ok=(code == 0),
            errors=errors,
            findings=findings,
            elapsed_s=time.time() - started,
        )
        if code == 0:
            print("LOOP OK", flush=True)
            return 0
        print("LOOP FAIL — diagnosis:", flush=True)
        for item in findings:
            print(f"  [{item['category']}] {item['fix']}", flush=True)
        # Outer retries without code changes only help flaky renders.
        if not args.render:
            break

    print(
        f"LOOP STOPPED after failures. Agent: patch from {report_path} then re-run.",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
