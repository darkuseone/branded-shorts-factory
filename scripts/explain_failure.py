#!/usr/bin/env python3
"""Print why a run produced no video, as the last thing in the job log.

Diagnosing the previous failures meant fetching the log tail repeatedly and
getting artifact-upload boilerplate every time — the actual error sat two
hundred lines further up, behind two `upload-artifact` steps that each emit
forty lines of nothing. The tail is what tooling reads, so the tail is where
the diagnosis belongs.

Usage: python3 scripts/explain_failure.py <scenario-id> [--log build/render.log]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_TAIL_LINES = 60


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def explain(scenario_id: str, log_path: Path) -> None:
    report_path = ROOT / "build" / "reports" / f"{scenario_id}.json"

    _section("what came out")
    output = ROOT / "build" / "output"
    produced = sorted(output.glob("*.mp4")) if output.is_dir() else []
    print(f"videos: {[p.name for p in produced] or 'none'}")
    composition = ROOT / "build" / "composition" / "index.html"
    print(f"composition: {'written' if composition.exists() else 'missing'}")

    if not report_path.exists():
        print(f"\nno report at {report_path.relative_to(ROOT)} — the run died before it wrote one")
    else:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"\nreport is not valid JSON: {exc}")
            report = {}

        counts = (report.get("qa") or {}).get("counts") or {}
        if counts:
            _section("visual slots")
            print(
                f"accepted={counts.get('accepted', 0)} "
                f"manual_review={counts.get('manual_review', 0)} "
                f"unfilled={counts.get('rejected', 0)} "
                f"vision_checked={counts.get('vision_checked', 0)}"
            )

        render = report.get("render") or {}
        if render:
            _section("renderer")
            for key in ("ok", "step", "returncode", "message", "command"):
                if key in render:
                    print(f"{key}: {render[key]}")
            for key in ("stderr", "stdout"):
                text = str(render.get(key) or "").strip()
                if text:
                    print(f"--- {key} ---")
                    print("\n".join(text.splitlines()[-40:]))

        warnings = report.get("warnings") or []
        if warnings:
            _section(f"warnings ({len(warnings)})")
            for warning in warnings[:40]:
                print(f"  - {warning}")

    if log_path.exists():
        _section(f"last {LOG_TAIL_LINES} lines of the render")
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-LOG_TAIL_LINES:]:
            print(line)
    else:
        print(f"\nno render log at {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario_id")
    parser.add_argument("--log", default="build/render.log")
    args = parser.parse_args()
    explain(args.scenario_id, ROOT / args.log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
