"""Fill every visual slot with material that survives both quality gates.

For one visual the loop is:

    multi-keyword free search → escalation policy → download → level 1 (native)
    → level 2 (Grok Vision) → accept, or take the next candidate

Escalation to the paid tier only happens when free stock genuinely cannot do
the job, and each visual gets a bounded number of attempts so a bad slot cannot
stall the run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .assets.library import MemeLibrary
from .config import Settings
from .errors import BudgetExceededError
from .generative.budget import TokenBudget
from .generative.grok_imagine import GrokImagineClient
from .generative.magnific import GenerationRequest, MagnificClient, prompt_for
from .generative.policy import after_library_miss, decide
from .logging_utils import get_logger
from .media.download import Downloader, LocalAsset
from .qa.gate import VisualQA, combine
from .qa.native import check_native
from .qa.vision import GrokVisionGate
from .search.aggregator import SearchAggregator, SearchOutcome, media_type_for
from .search.candidates import Candidate
from .search.keywords import QueryPlan
from .search.ranking import RankedCandidate, rank_candidates
from .spec import Spec, Visual

log = get_logger("resolver")

#: How many candidates one visual may burn before we give up on it.
MAX_ATTEMPTS = 4

_STYLE_HINTS = {
    "motion_graphics": "premium motion graphics, smooth animation, brand-clean",
    "infographic": "clean animated infographic, large legible numbers, minimal chrome",
    "footage": "cinematic footage, shallow depth of field, natural light",
    "image": "editorial photograph, crisp detail",
    "generated": "high-end cinematic render",
    "screen_record": "clean product interface, high contrast",
}


@dataclass
class ResolvedVisual:
    """One filled slot, plus the trail of how it got filled."""

    visual: Visual
    qa: VisualQA
    search: SearchOutcome | None
    escalated: bool = False

    @property
    def asset(self) -> LocalAsset | None:
        return self.qa.asset

    @property
    def usable(self) -> bool:
        return self.qa.outcome in {"accepted", "manual_review"} and self.qa.asset is not None


class VisualResolver:
    def __init__(
        self,
        settings: Settings,
        *,
        aggregator: SearchAggregator | None = None,
        downloader: Downloader | None = None,
        magnific: MagnificClient | None = None,
        grok_imagine: GrokImagineClient | None = None,
        vision: GrokVisionGate | None = None,
        budget: TokenBudget | None = None,
        memes: MemeLibrary | None = None,
    ):
        self.settings = settings
        self.aggregator = aggregator or SearchAggregator(settings)
        self.downloader = downloader or Downloader(settings)
        self.magnific = magnific or MagnificClient(settings)
        self.grok_imagine = grok_imagine or GrokImagineClient(settings)
        self.vision = vision or GrokVisionGate(settings)
        self.budget = budget or TokenBudget.from_budgets(settings.budgets)
        self.memes = memes or MemeLibrary(settings.paths.meme_library)

    # -- public API ---------------------------------------------------------

    def resolve_all(self, spec: Spec) -> list[ResolvedVisual]:
        return [self.resolve(spec, visual) for visual in spec.visuals]

    def resolve(self, spec: Spec, visual: Visual) -> ResolvedVisual:
        context = spec.context_for(visual)

        if visual.type == "meme":
            return ResolvedVisual(visual=visual, qa=self._resolve_meme(spec, visual), search=None)

        local = self._resolve_local_broll(spec, visual)
        if local is not None:
            return ResolvedVisual(visual=visual, qa=local, search=None)

        outcome = self.aggregator.search_visual(visual, spec)
        decision = decide(
            visual,
            outcome,
            self.budget,
            self.settings.budgets,
            magnific_available=self.magnific.is_available,
            grok_available=self.grok_imagine.is_available,
            min_score=spec.constraints.min_visual_score,
        )
        log.info("%s: %s — %s", visual.id, decision.action, decision.reason, extra={"stage": "resolve"})

        queue, escalated = self._build_queue(spec, visual, outcome, decision)
        qa = self._try_candidates(spec, visual, queue, outcome.plan, context)
        qa.notes.insert(0, f"policy: {decision.action} ({decision.reason})")
        return ResolvedVisual(visual=visual, qa=qa, search=outcome, escalated=escalated)

    # -- candidate assembly -------------------------------------------------

    def _build_queue(
        self,
        spec: Spec,
        visual: Visual,
        outcome: SearchOutcome,
        decision,
    ) -> tuple[list[RankedCandidate], bool]:
        """Order the candidates to try, escalating to paid tiers if the policy says so."""
        free = list(outcome.ranked)
        escalated = False

        if decision.action == "accept_free" or decision.action == "manual_review":
            return free, escalated

        premium: list[Candidate] = []
        media_type = media_type_for(visual)

        if decision.action == "magnific_library":
            premium = self.magnific.search_library(outcome.plan.primary, media_type)
            escalated = True
            if not premium:
                follow_up = after_library_miss(
                    visual,
                    outcome,
                    self.budget,
                    hero=decision.hero,
                    grok_available=self.grok_imagine.is_available,
                )
                log.info(
                    "%s: %s — %s",
                    visual.id,
                    follow_up.action,
                    follow_up.reason,
                    extra={"stage": "resolve"},
                )
                decision = follow_up

        if decision.action == "magnific_generate":
            escalated = True
            generated = self._generate_magnific(spec, visual, outcome.plan, hero=decision.hero)
            if generated:
                premium.append(generated)
            elif self.grok_imagine.is_available and visual.allow_generative:
                decision = type(decision)(
                    "grok_fallback", "magnific generation produced nothing", decision.hero
                )

        if decision.action == "grok_fallback":
            escalated = True
            prompt = self._prompt(spec, visual, outcome.plan)
            generated = self.grok_imagine.generate(visual.id, prompt, media_type)
            if generated:
                premium.append(generated)

        if not premium:
            return free, escalated

        # Re-rank the premium options against the same plan so the comparison
        # with free stock is apples to apples, then put them first.
        ranked_premium = rank_candidates(premium, visual, outcome.plan, limit=4)
        return ranked_premium + free, escalated

    def _prompt(self, spec: Spec, visual: Visual, plan: QueryPlan) -> str:
        return prompt_for(
            plan.primary,
            spec.context_for(visual),
            _STYLE_HINTS.get(visual.type, "premium cinematic look"),
            avoid=visual.must_avoid,
        )

    def _generate_magnific(
        self, spec: Spec, visual: Visual, plan: QueryPlan, *, hero: bool
    ) -> Candidate | None:
        request = GenerationRequest(
            visual_id=visual.id,
            prompt=self._prompt(spec, visual, plan),
            media_type=media_type_for(visual),
            duration=visual.duration,
            style=_STYLE_HINTS.get(visual.type, "cinematic"),
            negative_prompt=", ".join(visual.must_avoid),
        )
        try:
            return self.magnific.generate(request, self.budget, hero=hero)
        except BudgetExceededError as exc:
            log.warning("%s: %s", visual.id, exc, extra={"stage": "resolve"})
            return None

    # -- the attempt loop ---------------------------------------------------

    def _try_candidates(
        self,
        spec: Spec,
        visual: Visual,
        queue: list[RankedCandidate],
        plan: QueryPlan,
        context: str,
    ) -> VisualQA:
        result = VisualQA(visual_id=visual.id, outcome="rejected", attempts=0)
        if not queue:
            result.notes.append("no candidates found by any source")
            return result

        held_for_review: VisualQA | None = None

        for ranked in queue[:MAX_ATTEMPTS]:
            result.attempts += 1
            candidate = ranked.candidate
            asset = self.downloader.fetch(candidate, subdir=visual.id)
            if asset is None:
                result.rejected.append({"candidate": candidate.key, "stage": "download", "reason": "failed"})
                continue

            native = check_native(asset, visual, plan, context)
            if not native.passed:
                log.info(
                    "%s: level 1 rejected %s (%.2f) — %s",
                    visual.id,
                    candidate.key,
                    native.score,
                    "; ".join(native.issues) or "low relevance",
                    extra={"stage": "qa"},
                )
                result.rejected.append(
                    {
                        "candidate": candidate.key,
                        "stage": "native",
                        "score": round(native.score, 3),
                        "issues": native.issues,
                    }
                )
                continue

            vision = self.vision.check(asset, visual, context) if spec.constraints.require_vision_qa else None
            outcome = combine(
                native,
                vision,
                require_vision=spec.constraints.require_vision_qa,
                manual_review_ok=spec.constraints.manual_review_ok,
            )

            if outcome == "accepted":
                result.outcome = "accepted"
                result.asset = asset
                result.native = native
                result.vision = vision
                return result

            if outcome == "manual_review" and held_for_review is None:
                held_for_review = VisualQA(
                    visual_id=visual.id,
                    outcome="manual_review",
                    asset=asset,
                    native=native,
                    vision=vision,
                    attempts=result.attempts,
                    notes=[f"held for review: {vision.reason if vision else 'vision unavailable'}"],
                )
                continue

            result.rejected.append(
                {
                    "candidate": candidate.key,
                    "stage": "vision",
                    "verdict": vision.verdict if vision else "n/a",
                    "reason": vision.reason if vision else "",
                    "distractors": vision.distractors if vision else [],
                }
            )

        if held_for_review is not None:
            held_for_review.rejected = result.rejected
            held_for_review.notes.append(f"{result.attempts} candidate(s) tried")
            return held_for_review

        result.notes.append(f"all {result.attempts} candidate(s) failed quality checks")
        return result

    # -- local overrides ----------------------------------------------------

    def _resolve_local_broll(self, spec: Spec, visual: Visual) -> VisualQA | None:
        """Accept ``jobs/<id>/broll/<visual_id>.*`` when an agent pre-staged stock.

        Used when Pexels/Pixabay keys are missing but Magnific MCP (or another
        source) already dropped files into the job folder.
        """
        folder = self.settings.paths.root / "jobs" / spec.id / "broll"
        if not folder.is_dir():
            return None
        extensions = (".mp4", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp")
        matches = sorted(
            path
            for path in folder.iterdir()
            if path.is_file() and path.stem == visual.id and path.suffix.lower() in extensions
        )
        if not matches:
            return None
        path = matches[0]
        media_type = "video" if path.suffix.lower() in {".mp4", ".webm", ".mov"} else "image"
        tags = [visual.query, *visual.keywords, *visual.must_include, "tech", "cyber", "ai"]
        candidate = Candidate(
            source="local_broll",
            external_id=path.name,
            media_type=media_type,
            download_url="",
            title=f"{visual.id} local b-roll",
            tags=[tag for tag in tags if tag],
            license="pre-staged",
        )
        from .media.ffmpeg import probe

        info = probe(path, self.settings.ffprobe_cmd)
        if info and info.width and info.height:
            candidate.width, candidate.height = info.width, info.height
            if info.duration:
                candidate.duration = info.duration
        asset = LocalAsset(candidate=candidate, path=path, info=info)
        from .media.ffmpeg import mean_luma

        luma = mean_luma(path, ffmpeg=self.settings.ffmpeg_cmd)
        # Infographics are often dark-on-void by brand; don't reject staged plates.
        dark_floor = 8.0 if visual.type in {"infographic", "motion_graphics"} else 18.0
        if luma is not None and luma < dark_floor:
            log.warning(
                "%s: local b-roll too dark (luma=%.1f); falling through to search",
                visual.id,
                luma,
                extra={"stage": "resolve"},
            )
            return None
        log.info("%s: using local b-roll %s", visual.id, path.name, extra={"stage": "resolve"})
        notes = [f"local b-roll override: {path.relative_to(self.settings.paths.root)}"]
        if luma is not None:
            notes.append(f"luma={luma:.1f}")
        return VisualQA(
            visual_id=visual.id,
            outcome="accepted",
            asset=asset,
            attempts=1,
            notes=notes,
        )

    # -- memes --------------------------------------------------------------

    def _resolve_meme(self, spec: Spec, visual: Visual) -> VisualQA:
        """Memes come only from the user's bank, matched by irony beat + tags."""
        result = VisualQA(visual_id=visual.id, outcome="rejected", attempts=1)
        if not spec.memes.enabled:
            result.notes.append("meme slot skipped: memes.enabled is false")
            return result

        tags = [tag.lower() for tag in (visual.keywords or visual.must_include or spec.memes.tags)]
        beat = ""
        humor = ""
        if visual.notes.startswith("auto-irony:"):
            parts = visual.notes.split(":")
            beat = parts[1] if len(parts) > 1 else ""
            humor = parts[2] if len(parts) > 2 else ""

        science_safe = (spec.rubric or "").lower() in {
            "космос",
            "space",
            "наука",
            "science",
            "ai",
            "технологии",
            "tech",
            "it",
        }
        item = self.memes.pick_for_beat(
            beat=beat,
            tags=tags,
            humor=humor,
            science_safe=science_safe,
        )
        if item is None:
            matches = self.memes.find(tags, limit=1)
            item = matches[0] if matches else None
        if item is None:
            result.notes.append(f"no meme in the bank matches {tags or beat or humor}")
            return result

        # Clamp the slot to the punch window so a 9s NOOOO never eats the VO.
        usable = item.max_use or min(spec.memes.max_duration, item.duration or spec.memes.max_duration)
        usable = max(0.5, min(usable, spec.memes.max_duration, visual.duration or usable))
        visual.duration = usable

        candidate = Candidate(
            source="meme_library",
            external_id=item.name,
            media_type="video" if item.path.suffix.lower() in {".mp4", ".webm", ".gif"} else "image",
            download_url="",
            title=item.title,
            tags=item.tags,
            license="user-supplied",
        )
        result.outcome = "accepted"
        result.asset = LocalAsset(candidate=candidate, path=item.path)
        result.notes.append(
            f"meme '{item.name}' humor={item.humor or '?'} beat={beat or item.beats[:1]} "
            f"trim={item.trim_start:g}s use={usable:g}s"
        )
        # Stash trim on the visual notes for the timeline writer.
        visual.notes = f"{visual.notes}|trim_start={item.trim_start:g}|max_use={usable:g}|humor={item.humor}"
        return result
