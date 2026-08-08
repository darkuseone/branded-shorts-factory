"""End-to-end coverage with every external service faked out.

These tests exercise the real orchestration path — search, escalation, both QA
gates, timeline, composition — and only stub the network boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shorts_factory.cli import main
from shorts_factory.generative.budget import TokenBudget
from shorts_factory.media.download import Downloader, LocalAsset
from shorts_factory.pipeline import Pipeline
from shorts_factory.qa.vision import VisionVerdict
from shorts_factory.render.hyperframes import HyperFramesRunner, RenderResult, StepResult
from shorts_factory.resolver import VisualResolver
from shorts_factory.search.aggregator import SearchAggregator
from shorts_factory.search.candidates import Candidate

from .conftest import EXAMPLE
from .test_search import FakeProvider, make_candidate


class FakeDownloader(Downloader):
    """Writes a placeholder file instead of hitting the network."""

    def __init__(self, settings, fail_for: set[str] | None = None):
        super().__init__(settings)
        self.fail_for = fail_for or set()
        self.fetched: list[str] = []

    def fetch(self, candidate: Candidate, *, subdir: str = "") -> LocalAsset | None:
        if candidate.key in self.fail_for:
            return None
        self.fetched.append(candidate.key)
        target = self.settings.paths.downloads / (subdir or "misc") / candidate.suggested_filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 4096)
        return LocalAsset(candidate=candidate, path=target)


class FakeRunner(HyperFramesRunner):
    """Records the lint/check/render sequence without shelling out to npx."""

    def __init__(self, settings, *, produce: bool = False):
        super().__init__(settings)
        self.produce = produce
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def run_pipeline(self, project_dir: Path, output: Path) -> RenderResult:
        self.calls += ["lint", "check"]
        steps = [StepResult("lint", True, 0), StepResult("check", True, 0)]
        if self.settings.dry_run:
            steps.append(StepResult("render", True, 0, skipped=True))
            return RenderResult(output=None, steps=steps)
        self.calls.append("render")
        if self.produce:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"\x00" * 1024)
        steps.append(StepResult("render", True, 0))
        return RenderResult(output=output if output.exists() else None, steps=steps)


class FakeVision:
    """Stands in for Grok Vision with a scripted verdict per visual."""

    def __init__(self, verdicts: dict[str, VisionVerdict] | None = None, default: str = "pass"):
        self.verdicts = verdicts or {}
        self.default = default
        self.calls: list[str] = []

    def check(self, asset, visual, context) -> VisionVerdict:
        self.calls.append(visual.id)
        return self.verdicts.get(visual.id, VisionVerdict(verdict=self.default, on_topic=0.9, quality=0.85))


def build_resolver(settings, spec, *, vision=None, downloader=None, results=None) -> VisualResolver:
    """A resolver whose stock search always returns one strong candidate."""
    from shorts_factory.search.keywords import build_query_plan

    canned: dict[str, list[Candidate]] = {}
    for visual in spec.visuals:
        plan = build_query_plan(visual, spec)
        for query in plan.queries:
            canned[query] = [
                make_candidate(
                    external_id=f"{visual.id}-a",
                    title=f"{plan.primary} clip",
                    tags=plan.primary.split(),
                    width=1080,
                    height=1920,
                    duration=max(visual.duration + 2, 6.0),
                )
            ]
    canned.update(results or {})

    provider = FakeProvider(settings, canned)
    return VisualResolver(
        settings,
        aggregator=SearchAggregator(settings, providers=[provider]),
        downloader=downloader or FakeDownloader(settings),
        vision=vision or FakeVision(),
        budget=TokenBudget.from_budgets(settings.budgets),
    )


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #


def test_every_visual_is_filled_when_search_and_qa_agree(settings, minimal_spec):
    resolver = build_resolver(settings, minimal_spec)
    resolved = resolver.resolve_all(minimal_spec)
    outcomes = [item.qa.outcome for item in resolved]
    assert outcomes == ["accepted"] * len(minimal_spec.visuals)
    assert all(item.asset is not None for item in resolved)


def test_vision_replace_pushes_the_slot_to_the_next_candidate(settings, minimal_spec):
    vision = FakeVision(
        {"v1": VisionVerdict(verdict="replace", reason="a juice can is in frame")}, default="replace"
    )
    resolver = build_resolver(settings, minimal_spec, vision=vision)
    resolved = resolver.resolve(minimal_spec, minimal_spec.visuals[0])
    assert resolved.qa.outcome == "rejected"
    assert resolved.qa.rejected and resolved.qa.rejected[0]["stage"] == "vision"
    assert "juice can" in resolved.qa.rejected[0]["reason"]


def test_unavailable_vision_holds_the_slot_for_review(settings, minimal_spec):
    vision = FakeVision(default="manual")
    resolver = build_resolver(settings, minimal_spec, vision=vision)
    resolved = resolver.resolve(minimal_spec, minimal_spec.visuals[0])
    assert resolved.qa.outcome == "manual_review"
    assert resolved.asset is not None, "a held slot still keeps its asset for a human to judge"


def test_download_failure_moves_on_to_the_next_candidate(settings, minimal_spec):
    downloader = FakeDownloader(settings, fail_for={"pexels:v1-a"})
    resolver = build_resolver(settings, minimal_spec, downloader=downloader)
    resolved = resolver.resolve(minimal_spec, minimal_spec.visuals[0])
    assert resolved.qa.rejected[0]["stage"] == "download"


def test_offline_run_spends_no_tokens(settings, minimal_spec):
    resolver = build_resolver(settings, minimal_spec)
    resolver.resolve_all(minimal_spec)
    assert resolver.budget.spent == 0, "free stock that passes QA must never cost tokens"


def test_meme_slot_without_a_bank_is_reported_not_faked(settings, minimal_spec, tmp_path):
    from shorts_factory.assets.library import MemeLibrary

    minimal_spec.memes.enabled = True
    minimal_spec.memes.tags = ["shock"]
    minimal_spec.visuals[0].type = "meme"
    minimal_spec.visuals[0].keywords = ["shock"]
    empty_bank = tmp_path / "empty-memes"
    empty_bank.mkdir()
    resolver = build_resolver(settings, minimal_spec)
    resolver.memes = MemeLibrary(empty_bank)
    resolved = resolver.resolve(minimal_spec, minimal_spec.visuals[0])
    assert resolved.qa.outcome == "rejected"
    assert "no meme in the bank" in resolved.qa.notes[0]


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def test_plan_builds_a_composition_ready_timeline(settings, example_spec):
    pipeline = Pipeline(
        settings,
        resolver=build_resolver(settings, example_spec),
        runner=FakeRunner(settings),
    )
    result = pipeline.plan(example_spec)

    assert result.timeline is not None
    assert len(result.qa.accepted) == len(example_spec.visuals)
    assert result.captions, "captions are estimated even without a voice render"
    assert result.report_path and result.report_path.exists()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["qa"]["counts"]["accepted"] == len(example_spec.visuals)


def test_full_run_writes_a_composition_and_stops_at_the_renderer(settings, example_spec):
    """`dry_run` settings stop before the render; everything else is the real path."""
    runner = FakeRunner(settings)
    pipeline = Pipeline(settings, resolver=build_resolver(settings, example_spec), runner=runner)
    result = pipeline.run(example_spec)

    assert runner.calls == ["lint", "check"], "a dry run still lints and checks the composition"

    assert result.composition_dir is not None
    index = Path(result.composition_dir) / "index.html"
    assert index.exists()
    html = index.read_text(encoding="utf-8")
    assert 'data-composition-id="venus_hell"' in html
    assert not result.rendered, "dry runs must not claim a video was produced"
    assert any("narration skipped" in warning for warning in result.warnings)


def test_run_report_records_budget_and_search_trail(settings, example_spec):
    pipeline = Pipeline(
        settings,
        resolver=build_resolver(settings, example_spec),
        runner=FakeRunner(settings),
    )
    result = pipeline.run(example_spec)
    payload = result.to_dict()
    assert payload["budget"]["spent"] == 0
    assert set(payload["search"]) == {visual.id for visual in example_spec.visuals}
    first_id = example_spec.visuals[0].id
    assert payload["search"][first_id]["queries"], "the query fan is part of the audit trail"


def test_non_contiguous_avatar_segments_are_flagged(settings, example_spec):
    """`segments: ["hook", "s6"]` leaves a silent middle — say so, don't hide it."""
    pipeline = Pipeline(
        settings,
        resolver=build_resolver(settings, example_spec),
        runner=FakeRunner(settings),
    )
    result = pipeline.run(example_spec)
    assert result.avatar_start == pytest.approx(example_spec.hook.start)
    assert any("not contiguous" in warning for warning in result.warnings)


def test_wet_run_reports_the_rendered_file(settings, minimal_spec):
    from dataclasses import replace

    wet = replace(settings, dry_run=False)
    runner = FakeRunner(wet, produce=True)
    pipeline = Pipeline(wet, resolver=build_resolver(wet, minimal_spec), runner=runner)
    result = pipeline.run(minimal_spec)

    assert runner.calls == ["lint", "check", "render"]
    assert result.rendered
    assert result.output.name == f"{minimal_spec.id}.mp4"


def test_render_failure_is_reported_without_losing_the_composition(settings, minimal_spec):
    from dataclasses import replace

    from shorts_factory.errors import RenderError

    class FailingRunner(FakeRunner):
        def run_pipeline(self, project_dir, output):
            raise RenderError("hyperframes lint failed:\nunclosed clip element")

    wet = replace(settings, dry_run=False)
    pipeline = Pipeline(wet, resolver=build_resolver(wet, minimal_spec), runner=FailingRunner(wet))
    result = pipeline.run(minimal_spec)

    assert not result.rendered
    assert any("unclosed clip element" in warning for warning in result.warnings)
    assert result.composition_dir and (Path(result.composition_dir) / "index.html").exists()


def test_unfilled_visual_surfaces_as_a_warning(settings, minimal_spec):
    vision = FakeVision(default="replace")
    pipeline = Pipeline(
        settings,
        resolver=build_resolver(settings, minimal_spec, vision=vision),
        runner=FakeRunner(settings),
    )
    result = pipeline.run(minimal_spec)
    assert result.qa.rejected
    assert any("could not be filled" in warning for warning in result.warnings)
    assert result.needs_review


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_validate_accepts_the_example():
    assert main(["validate", str(EXAMPLE)]) == 0


def test_cli_rejects_a_broken_scenario(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"title": "no id"}), encoding="utf-8")
    assert main(["validate", str(broken)]) == 2


def test_cli_doctor_runs_without_credentials(monkeypatch, tmp_path):
    for name in ("HEYGEN_API_KEY", "ELEVENLABS_API_KEY", "MAGNIFIC_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    # Point the runner at a binary that does not exist so doctor stays offline.
    monkeypatch.setenv("HYPERFRAMES_CMD", "hyperframes-not-installed")
    assert main(["--workdir", str(tmp_path), "doctor"]) == 0


@pytest.mark.parametrize("command", ["validate", "plan", "build"])
def test_cli_help_lists_every_command(command, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([command, "--help"])
    assert excinfo.value.code == 0
