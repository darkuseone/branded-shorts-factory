"""The orchestrator: one JSON in, one finished MP4 out.

Stages run in a fixed order and each one degrades rather than aborts, so a
missing credential costs you a track — not the run. What actually happened is
always written to `build/reports/<id>.json`.

    validate → brand → voice → avatar → visuals (search + 2× QA)
            → captions → music → mix → timeline → composition → render
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .assets.brand import Brandbook, apply_brandbook
from .assets.library import MemeLibrary, MusicLibrary
from .config import Settings
from .errors import RenderError
from .generative.budget import TokenBudget
from .logging_utils import get_logger, stage
from .qa.gate import QAReport
from .render.audio_mix import build_mix
from .render.composition import CompositionWriter
from .render.hyperframes import HyperFramesRunner, RenderResult
from .render.timeline import Timeline, build_timeline, use_mixed_audio
from .resolver import ResolvedVisual, VisualResolver
from .spec import Spec, SpecIssue
from .voice.audio_design import suggest_audio_fx
from .voice.captions import CaptionCue, build_cues
from .voice.elevenlabs import ElevenLabsClient, SfxClip, VoiceClip
from .voice.heygen import AvatarClip, HeyGenClient

log = get_logger("pipeline")


@dataclass
class RunResult:
    """Everything one run produced, successful or not."""

    spec_id: str
    output: Path | None = None
    composition_dir: Path | None = None
    report_path: Path | None = None
    timeline: Timeline | None = None
    qa: QAReport = field(default_factory=QAReport)
    resolved: list[ResolvedVisual] = field(default_factory=list)
    voice_clips: list[VoiceClip] = field(default_factory=list)
    sfx_clips: list[SfxClip] = field(default_factory=list)
    captions: list[CaptionCue] = field(default_factory=list)
    avatar: AvatarClip | None = None
    avatar_start: float = 0.0
    music: Path | None = None
    render: RenderResult | None = None
    budget: TokenBudget | None = None
    spec_issues: list[SpecIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rendered(self) -> bool:
        return self.output is not None and self.output.exists()

    @property
    def needs_review(self) -> bool:
        return bool(self.qa.manual_review) or bool(self.qa.rejected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "output": str(self.output) if self.output else None,
            "composition_dir": str(self.composition_dir) if self.composition_dir else None,
            "rendered": self.rendered,
            "needs_review": self.needs_review,
            "spec_issues": [str(issue) for issue in self.spec_issues],
            "warnings": self.warnings,
            "qa": self.qa.to_dict(),
            "budget": self.budget.to_dict() if self.budget else None,
            "search": {item.visual.id: item.search.to_dict() for item in self.resolved if item.search},
            "voice": {
                "clips": [clip.to_dict() for clip in self.voice_clips],
                "sfx": [clip.to_dict() for clip in self.sfx_clips],
                "captions": len(self.captions),
                "avatar": self.avatar.to_dict() if self.avatar else None,
                "music": str(self.music) if self.music else None,
            },
            "timeline": self.timeline.to_dict() if self.timeline else None,
            "render": self.render.to_dict() if self.render else None,
        }


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        resolver: VisualResolver | None = None,
        runner: HyperFramesRunner | None = None,
    ):
        settings.paths.ensure()
        self.settings = settings
        self.budget = TokenBudget.from_budgets(settings.budgets)
        self.resolver = resolver or VisualResolver(settings, budget=self.budget)
        self.voice_client = ElevenLabsClient(settings)
        self.avatar_client = HeyGenClient(settings)
        self.runner = runner or HyperFramesRunner(settings)
        self.music_library = MusicLibrary(settings.paths.music_library)
        self.meme_library = MemeLibrary(settings.paths.meme_library)

    # -- entry points -------------------------------------------------------

    def plan(self, spec: Spec, issues: list[SpecIssue] | None = None) -> RunResult:
        """Everything except audio, avatar and rendering — a cheap dry pass."""
        result = RunResult(spec_id=spec.id, spec_issues=list(issues or []), budget=self.budget)
        self._apply_brand(spec, result)
        self._resolve_visuals(spec, result)
        result.captions = build_cues(spec.all_segments, [], spec.captions)
        result.music = self._resolve_music(spec, result)
        result.timeline = build_timeline(
            spec,
            result.resolved,
            captions=result.captions,
            music_path=result.music,
        )
        result.warnings.extend(result.timeline.warnings)

        # Writing the composition here is what makes `plan` previewable: open
        # the generated index.html in a browser to see the layout before
        # spending a single voice or render credit.
        composition = CompositionWriter(
            spec, result.timeline, self.settings.paths.composition / spec.id
        ).write()
        result.composition_dir = composition.directory

        self._write_report(spec, result)
        return result

    def run(self, spec: Spec, issues: list[SpecIssue] | None = None) -> RunResult:
        """The full build."""
        result = RunResult(spec_id=spec.id, spec_issues=list(issues or []), budget=self.budget)

        with stage("brand", log):
            self._apply_brand(spec, result)

        with stage("voice", log) as info:
            self._synthesize_voice(spec, result)
            info["clips"] = len(result.voice_clips)
            info["fx"] = len(result.sfx_clips)

        with stage("avatar", log) as info:
            self._generate_avatar(spec, result)
            info["ok"] = result.avatar is not None

        with stage("visuals", log) as info:
            self._resolve_visuals(spec, result)
            info["qa"] = result.qa.summary()

        with stage("captions", log) as info:
            result.captions = build_cues(spec.all_segments, result.voice_clips, spec.captions)
            info["cues"] = len(result.captions)

        with stage("music", log):
            result.music = self._resolve_music(spec, result)

        with stage("timeline", log) as info:
            result.timeline = build_timeline(
                spec,
                result.resolved,
                voice_clips=result.voice_clips,
                sfx_clips=result.sfx_clips,
                captions=result.captions,
                avatar=result.avatar,
                avatar_start=result.avatar_start,
                music_path=result.music,
            )
            self._mix_audio(spec, result)
            result.warnings.extend(result.timeline.warnings)
            info["elements"] = len(result.timeline.elements)

        with stage("compose", log) as info:
            composition_dir = self.settings.paths.composition / spec.id
            writer = CompositionWriter(spec, result.timeline, composition_dir)
            composition = writer.write()
            result.composition_dir = composition.directory
            info["media"] = composition.media_files

        with stage("render", log) as info:
            output = self.settings.paths.output / f"{spec.id}.mp4"
            try:
                result.render = self.runner.run_pipeline(composition.directory, output)
                result.output = result.render.output
                info["output"] = result.output.name if result.output else "none"
            except RenderError as exc:
                result.warnings.append(str(exc))
                log.error("%s", exc, extra={"stage": "render"})

        self._write_report(spec, result)
        return result

    # -- stages -------------------------------------------------------------

    def _apply_brand(self, spec: Spec, result: RunResult) -> None:
        book = Brandbook.load(self.settings.paths.brand_dir)
        spec.brand = apply_brandbook(spec.brand, book)
        if book is None:
            result.warnings.append("no brandbook found; using per-video brand_elements")

    def _synthesize_voice(self, spec: Spec, result: RunResult) -> None:
        if not self.voice_client.is_available:
            result.warnings.append(f"narration skipped: {self.voice_client.unavailable_reason()}")
            return

        result.voice_clips = self.voice_client.synthesize_script(spec.all_segments, spec.voice)
        if len(result.voice_clips) < len(spec.all_segments):
            result.warnings.append(
                f"only {len(result.voice_clips)}/{len(spec.all_segments)} narration segments were rendered"
            )

        effects = spec.audio_fx or suggest_audio_fx(spec)
        result.sfx_clips = self.voice_client.generate_all_fx(effects)

    def _generate_avatar(self, spec: Spec, result: RunResult) -> None:
        """Render the presenter over the window it is supposed to speak in.

        The avatar is driven by our own ElevenLabs narration, so the audio has
        to be positioned relative to the start of that window — otherwise the
        lip sync drifts by however long the intro was.
        """
        if not spec.avatar.enabled:
            return

        wanted = spec.avatar.segments
        segments = [s for s in spec.all_segments if not wanted or s.id in wanted]
        if not segments:
            result.warnings.append("avatar.segments matched no script segments; presenter skipped")
            return

        window_start = min(segment.start for segment in segments)
        window_end = max(segment.end for segment in segments)
        result.avatar_start = window_start

        gaps = [
            (a.end, b.start) for a, b in zip(segments, segments[1:], strict=False) if b.start - a.end > 1.5
        ]
        if gaps:
            result.warnings.append(
                "avatar.segments are not contiguous ("
                + ", ".join(f"{a:g}–{b:g}s silent" for a, b in gaps)
                + "); the presenter stays on screen through the gaps"
            )

        clips = [clip for clip in result.voice_clips if not wanted or clip.segment_id in wanted]
        narration: Path | None = None
        if clips:
            shifted = [replace(clip, start=max(0.0, clip.start - window_start)) for clip in clips]
            mix = build_mix(
                self.settings.paths.voice / "narration_for_avatar.mp3",
                duration=window_end - window_start,
                voice_clips=shifted,
                sfx_clips=[],
                music_path=None,
                music=spec.music,
                ffmpeg=self.settings.ffmpeg_cmd,
            )
            if mix.ok:
                narration = mix.path
            elif len(shifted) == 1 and shifted[0].start < 0.05:
                narration = shifted[0].path
            else:
                result.warnings.append(
                    f"could not build a narration track for the avatar ({mix.reason}); using text mode"
                )

        result.avatar = self.avatar_client.generate(
            spec.avatar,
            spec.voice,
            text=" ".join(segment.text for segment in segments),
            audio_path=narration,
            duration_hint=window_end - window_start,
        )
        if result.avatar is None:
            result.warnings.append("avatar track missing; the render continues without a presenter")

    def _resolve_visuals(self, spec: Spec, result: RunResult) -> None:
        result.resolved = self.resolver.resolve_all(spec)
        for item in result.resolved:
            result.qa.add(item.qa)
        for item in result.resolved:
            if item.qa.outcome == "rejected":
                result.warnings.append(
                    f"visual {item.visual.id} could not be filled: "
                    + ("; ".join(item.qa.notes) or "no candidates passed QA")
                )
            elif item.qa.outcome == "manual_review":
                result.warnings.append(
                    f"visual {item.visual.id} needs a human look: "
                    + (item.qa.vision.reason if item.qa.vision else "vision gate unavailable")
                )
        log.info("%s", result.qa.summary(), extra={"stage": "visuals"})

    def _resolve_music(self, spec: Spec, result: RunResult) -> Path | None:
        if not spec.music.track and not self.music_library.items:
            return None
        item = self.music_library.resolve(spec.music.track)
        if item is None:
            result.warnings.append(
                f"no music track available (looked for {spec.music.track!r} in "
                f"{self.settings.paths.music_library})"
            )
            return None
        return item.path

    def _mix_audio(self, spec: Spec, result: RunResult) -> None:
        if not (result.voice_clips or result.sfx_clips or result.music):
            return
        assert result.timeline is not None
        mix = build_mix(
            self.settings.paths.voice / f"{spec.id}_mix.mp3",
            duration=spec.duration_target,
            voice_clips=result.voice_clips,
            sfx_clips=result.sfx_clips,
            music_path=result.music,
            music=spec.music,
            ffmpeg=self.settings.ffmpeg_cmd,
        )
        if mix.ok and mix.path is not None:
            use_mixed_audio(result.timeline, mix.path, spec.duration_target)
        else:
            result.warnings.append(
                f"pre-mix unavailable ({mix.reason}); audio tracks are handed to the renderer separately"
            )

    # -- reporting ----------------------------------------------------------

    def _write_report(self, spec: Spec, result: RunResult) -> None:
        path = self.settings.paths.reports / f"{spec.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        result.report_path = path
        log.info("report written to %s", path, extra={"stage": "report"})
