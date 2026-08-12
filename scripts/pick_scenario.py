#!/usr/bin/env python3
"""Decide which scenario a workflow run should render.

This used to be fifty lines of shell inside render-short.yml, and it failed
on ordinary pushes: it inferred the scenario from the diff with
`--diff-filter=d`, so a commit that deleted one job while editing another
found nothing and the run died before installing anything. A render that
cannot start is indistinguishable, in the Actions UI, from a render that
crashed.

The rules, in order:

1. An explicit `--spec` wins. This is what `workflow_dispatch` passes and it
   is never second-guessed.
2. Otherwise, the scenario touched by the push — either its JSON changed, or
   something under `jobs/<id>/` did.
3. Otherwise, the only scenario in `jobs/`. With one scenario there is
   nothing to disambiguate, and refusing to render it helps nobody.
4. Otherwise, fail and say exactly what was found.

With `--require-avatar`, whatever is chosen must also have a committed avatar
clip beside it. Render Short passes it, because it renders with an external
avatar and never calls HeyGen; Prepare Short does not, because it runs to
produce the material that avatar is made from.

Usage:
    python3 scripts/pick_scenario.py [--spec PATH] [--before SHA] [--sha SHA]
                                     [--require-avatar]

Writes `spec=` and `id=` to $GITHUB_OUTPUT when that is set, and prints them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "jobs"
AVATAR_NAMES = ("avatar.mp4", "avatar.webm")


def _git(*args: str) -> list[str]:
    """Run git, returning stdout lines. A failure is an empty result."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return [line for line in done.stdout.splitlines() if line.strip()]


def _commit_exists(sha: str) -> bool:
    """`git cat-file -e` prints nothing, so its exit code is the whole answer."""
    if not sha or sha.strip("0") == "":
        return False
    try:
        done = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def changed_paths(before: str, sha: str) -> list[str]:
    """Paths touched by this push, deletions included.

    Deletions matter: removing `jobs/x/broll/old.mp4` is a change to job `x`,
    and filtering them out is what made the old shell version give up.
    """
    if before and sha and _commit_exists(before):
        paths = _git("diff", "--name-only", before, sha)
        if paths:
            return paths
    if sha:
        return _git("show", "--name-only", "--pretty=", sha)
    return []


def scenario_ids() -> list[str]:
    return sorted(path.stem for path in JOBS.glob("*.json"))


def ids_from_paths(paths: list[str]) -> list[str]:
    """Scenario ids implicated by a list of repository paths."""
    known = set(scenario_ids())
    found: list[str] = []
    for raw in paths:
        parts = Path(raw).parts
        if len(parts) < 2 or parts[0] != "jobs":
            continue
        name = parts[1]
        candidate = name[:-5] if name.endswith(".json") else name
        if candidate in known and candidate not in found:
            found.append(candidate)
    return found


def avatar_for(scenario_id: str) -> Path | None:
    for name in AVATAR_NAMES:
        path = JOBS / scenario_id / name
        if path.is_file() and path.stat().st_size > 4096:
            return path
    return None


def resolve(
    spec: str = "", before: str = "", sha: str = "", *, require_avatar: bool = False
) -> tuple[Path, str]:
    """Return the scenario file and its id, or raise SystemExit with a reason."""
    if spec:
        path = ROOT / spec if not Path(spec).is_absolute() else Path(spec)
        if not path.is_file():
            raise SystemExit(f"::error::scenario not found: {spec}")
        return path, json.loads(path.read_text(encoding="utf-8"))["id"]

    available = scenario_ids()
    if not available:
        raise SystemExit("::error::no scenarios in jobs/*.json")

    # A workflow that renders with a committed avatar can only ever run a
    # scenario that has one. Choosing another is not a near miss — the run
    # fails at the first step with nothing to show, which is what happened
    # when a branch that deletes old scenarios was built on a merge ref: the
    # deletions counted as "touched", and a scenario with no avatar won.
    eligible = [name for name in available if avatar_for(name)] if require_avatar else available

    touched = ids_from_paths(changed_paths(before, sha))
    # A push may touch several jobs; prefer one that can actually be rendered,
    # then one this workflow is allowed to consider at all, then anything.
    with_avatar = [name for name in touched if name in eligible and avatar_for(name)]
    order = with_avatar or [name for name in touched if name in eligible]
    if not order and not require_avatar:
        order = touched
    chosen = order[0] if order else ""

    if not chosen:
        if len(eligible) == 1:
            chosen = eligible[0]
        elif not eligible:
            listing = ", ".join(available)
            raise SystemExit(
                "::error::no scenario in jobs/ has a committed avatar clip beside "
                f"it, and this workflow renders with one ({listing})"
            )
        else:
            listing = ", ".join(eligible)
            raise SystemExit(
                "::error::this push touched no renderable scenario and jobs/ holds "
                f"several ({listing}) — dispatch the workflow with an explicit spec"
            )

    path = JOBS / f"{chosen}.json"
    if not path.is_file():
        raise SystemExit(f"::error::scenario not found: jobs/{chosen}.json")
    return path, chosen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default="")
    parser.add_argument("--before", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument(
        "--require-avatar",
        action="store_true",
        help="fail unless a committed avatar clip sits beside the scenario "
        "(Render needs one; Prepare runs before it exists)",
    )
    args = parser.parse_args()

    path, scenario_id = resolve(
        args.spec.strip(),
        args.before.strip(),
        args.sha.strip(),
        require_avatar=args.require_avatar,
    )

    if args.require_avatar and avatar_for(scenario_id) is None:
        raise SystemExit(
            f"::error::jobs/{scenario_id}/avatar.mp4 is missing — this workflow "
            "renders with an external avatar, so the clip has to be committed"
        )

    relative = path.relative_to(ROOT).as_posix()
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"spec={relative}\nid={scenario_id}\n")
    print(f"spec={relative}")
    print(f"id={scenario_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
