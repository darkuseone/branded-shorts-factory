"""Command line entry point.

    python -m shorts_factory validate jobs/my-short.json
    python -m shorts_factory plan     jobs/my-short.json
    python -m shorts_factory build    jobs/my-short.json
    python -m shorts_factory sfxscan
    python -m shorts_factory doctor

Exit codes: 0 success, 1 recoverable failure (render failed, slots unfilled),
2 invalid scenario JSON, 3 bad usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import Settings
from .errors import ShortsFactoryError, SpecError
from .logging_utils import get_logger, setup_logging
from .media.ffmpeg import is_available
from .pipeline import Pipeline, RunResult
from .render.hyperframes import SKILL_INSTALL_HINT, HyperFramesRunner
from .search.keywords import build_query_plan
from .search.providers import build_providers
from .spec import Spec, load_spec
from .voice.sfx_analysis import REQUIRED_ROLES, coverage, scan, write_index

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_SPEC = 2
EXIT_USAGE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shorts-factory",
        description="Build a branded 9:16 YouTube Short from one scenario JSON.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--workdir", type=Path, help="build directory (default: ./build)")
    parser.add_argument("--offline", action="store_true", help="never touch the network")
    parser.add_argument("--dry-run", action="store_true", help="do everything except the final render")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("validate", "check a scenario JSON and report every issue"),
        ("plan", "search, QA and lay out the timeline without rendering"),
        ("build", "the full pipeline, ending in an MP4"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("spec", type=Path, help="path to the scenario JSON")
        if name != "validate":
            command.add_argument("--json", action="store_true", help="print the run report as JSON")

    scan = sub.add_parser("sfxscan", help="measure the sound bank and write assets/sfx/index.json")
    scan.add_argument(
        "--check",
        action="store_true",
        help="report the bank without writing index.json (exit 1 if a role has no sound)",
    )

    sub.add_parser("doctor", help="check credentials and external tooling")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = setup_logging(verbose=args.verbose)

    settings = Settings.from_env(workdir=args.workdir)
    if args.offline:
        settings = _replace(settings, offline=True)
    if getattr(args, "dry_run", False):
        settings = _replace(settings, dry_run=True)

    try:
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "sfxscan":
            return _sfxscan(settings, check_only=args.check)

        spec, issues = load_spec(args.spec)
        for issue in issues:
            level = {"error": log.error, "warning": log.warning}.get(issue.level, log.info)
            level("%s", issue)

        if args.command == "validate":
            return _validate(spec, issues)

        pipeline = Pipeline(settings)
        result = pipeline.plan(spec, issues) if args.command == "plan" else pipeline.run(spec, issues)

        if getattr(args, "json", False):
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_summary(result, args.command)
        return EXIT_OK if _succeeded(result, args.command) else EXIT_FAILED

    except SpecError as exc:
        log.error("%s", exc)
        return EXIT_BAD_SPEC
    except ShortsFactoryError as exc:
        log.error("%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        log.warning("interrupted")
        return EXIT_FAILED


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _validate(spec: Spec, issues: list) -> int:
    log = get_logger("validate")
    warnings = [issue for issue in issues if issue.level == "warning"]
    log.info(
        "%s — %.0fs, %d visual(s), %d narration segment(s), %.0f%% visual coverage",
        spec.title,
        spec.duration_target,
        len(spec.visuals),
        len(spec.all_segments),
        spec.coverage() * 100,
    )
    for visual in spec.visuals:
        plan = build_query_plan(visual, spec)
        log.info("  %s [%s] → %s", visual.id, visual.type, " | ".join(plan.queries))
    if warnings:
        log.warning("%d warning(s) — the scenario is usable but check them", len(warnings))
    else:
        log.info("no warnings")
    return EXIT_OK


def _sfxscan(settings: Settings, *, check_only: bool) -> int:
    """Measure every sound in the bank and record what each one is good for.

    Run it after dropping new files in. Filenames are not evidence — this is
    what decides whether `35913__altemark__bd` is an impact or a music bed.
    """
    log = get_logger("sfxscan")
    directory = settings.paths.sfx_library
    profiles = scan(directory, ffmpeg=settings.ffmpeg_cmd, ffprobe=settings.ffprobe_cmd)
    if not profiles:
        log.error("nothing measured in %s (is ffmpeg installed?)", directory)
        return EXIT_FAILED

    accents = [profile for profile in profiles if profile.usable_as_accent]
    log.info("%d file(s) measured, %d usable as accents", len(profiles), len(accents))

    by_shape: dict[str, int] = {}
    for profile in profiles:
        by_shape[profile.shape] = by_shape.get(profile.shape, 0) + 1
    log.info("shapes: %s", ", ".join(f"{shape} {count}" for shape, count in sorted(by_shape.items())))

    for profile in profiles:
        if not profile.usable_as_accent:
            log.warning(
                "%s is a %s (%.1fs) — excluded from accents%s",
                profile.path.name,
                profile.shape,
                profile.duration,
                "; move it to assets/music/" if profile.shape == "bed" else "",
            )

    gaps = [role for role, count in coverage(profiles, REQUIRED_ROLES).items() if count == 0]
    if gaps:
        log.warning("no sound covers: %s — these will be generated on demand", ", ".join(gaps))
    else:
        log.info("every role the montage asks for is covered by your own bank")

    if check_only:
        return EXIT_FAILED if gaps else EXIT_OK

    index = write_index(directory, profiles)
    log.info("wrote %s", index)
    return EXIT_OK


def _doctor(settings: Settings) -> int:
    log = get_logger("doctor")
    credentials = settings.credentials
    rows = [
        ("HeyGen (avatar)", bool(credentials.heygen), "HEYGEN_API_KEY"),
        ("ElevenLabs (voice + sfx)", bool(credentials.elevenlabs), "ELEVENLABS_API_KEY"),
        ("Magnific (premium visuals)", bool(credentials.magnific), "MAGNIFIC_API_KEY"),
        ("Grok (vision QA + reserve)", bool(credentials.grok), "GROK_API_KEY"),
        ("Pexels", bool(credentials.pexels), "PEXELS_API_KEY"),
        ("Pixabay", bool(credentials.pixabay), "PIXABAY_API_KEY"),
        ("Pond5", bool(credentials.pond5), "POND5_API_KEY"),
    ]
    for label, present, env_name in rows:
        log.info("%s %s (%s)", "✓" if present else "✗", label, env_name)

    keyless = [p.name for p in build_providers(settings) if not p.requires_key and p.is_available()]
    log.info("keyless stock sources ready: %s", ", ".join(keyless) or "none")

    log.info("%s ffmpeg", "✓" if is_available(settings.ffmpeg_cmd) else "✗")
    log.info("%s ffprobe", "✓" if is_available(settings.ffprobe_cmd) else "✗")

    runner = HyperFramesRunner(settings)
    if runner.is_available():
        log.info("✓ hyperframes runner (%s)", " ".join(runner.command))
        step = runner.doctor()
        if step.tail:
            log.info("hyperframes doctor: %s", step.tail[:400])
    else:
        log.warning("✗ hyperframes runner not found — install Node 22+ and `%s`", SKILL_INSTALL_HINT)

    log.info("music library: %s", settings.paths.music_library)
    log.info("meme library: %s", settings.paths.meme_library)
    log.info("brandbook dir: %s", settings.paths.brand_dir)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def _succeeded(result: RunResult, command: str) -> bool:
    if command == "plan":
        return not result.qa.rejected
    return result.rendered and not result.qa.rejected


def _print_summary(result: RunResult, command: str) -> None:
    log = get_logger("summary")
    log.info("QA: %s", result.qa.summary())
    if result.budget:
        log.info("Magnific tokens: %d/%d used", result.budget.spent, result.budget.total)
    for warning in result.warnings:
        log.warning("%s", warning)
    for item in result.qa.manual_review:
        log.warning(
            "review %s: %s",
            item.visual_id,
            (item.vision.reason if item.vision else "vision gate unavailable"),
        )
    if result.report_path:
        log.info("report: %s", result.report_path)
    if command != "plan":
        if result.rendered:
            log.info("output: %s", result.output)
        else:
            log.error("no video was produced")
    elif result.composition_dir:
        log.info("composition: %s", result.composition_dir)


def _replace(settings: Settings, **changes: object) -> Settings:
    from dataclasses import replace

    return replace(settings, **changes)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
