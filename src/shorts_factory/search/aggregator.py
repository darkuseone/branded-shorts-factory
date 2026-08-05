"""Fan every query out across every provider, then merge, dedupe and rank.

The unit of work is `(provider, query)`. Running that grid in a thread pool is
what makes multi-keyword search affordable: six queries across five sources is
thirty cheap HTTP calls that finish in the time of the slowest one.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..config import Settings
from ..errors import ProviderError
from ..logging_utils import get_logger
from ..spec import Spec, Visual
from .candidates import Candidate, MediaType
from .keywords import QueryPlan, build_query_plan
from .providers import StockProvider, build_providers
from .ranking import RankedCandidate, rank_candidates

log = get_logger("search")

# Visual types that are inherently still images; everything else prefers motion.
_IMAGE_TYPES = frozenset({"image", "infographic", "meme"})


@dataclass
class SearchOutcome:
    """Everything one visual's search produced, including what went wrong."""

    visual_id: str
    plan: QueryPlan
    ranked: list[RankedCandidate] = field(default_factory=list)
    provider_errors: dict[str, str] = field(default_factory=dict)
    skipped_providers: dict[str, str] = field(default_factory=dict)
    total_found: int = 0

    @property
    def best(self) -> RankedCandidate | None:
        return self.ranked[0] if self.ranked else None

    @property
    def best_score(self) -> float:
        return self.ranked[0].score if self.ranked else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "visual_id": self.visual_id,
            "queries": self.plan.queries,
            "warnings": self.plan.warnings,
            "total_found": self.total_found,
            "best_score": round(self.best_score, 4),
            "shortlist": [item.to_dict() for item in self.ranked[:5]],
            "provider_errors": self.provider_errors,
            "skipped_providers": self.skipped_providers,
        }


def media_type_for(visual: Visual) -> MediaType:
    return "image" if visual.type in _IMAGE_TYPES else "video"


class SearchAggregator:
    """Parallel multi-keyword search over the free stock tier."""

    def __init__(self, settings: Settings, providers: list[StockProvider] | None = None):
        self.settings = settings
        self.providers = providers if providers is not None else build_providers(settings)

    def available_providers(self) -> list[StockProvider]:
        return [provider for provider in self.providers if provider.is_available()]

    def search_visual(
        self,
        visual: Visual,
        spec: Spec | None = None,
        *,
        per_query_limit: int = 8,
        shortlist: int = 12,
    ) -> SearchOutcome:
        plan = build_query_plan(visual, spec)
        outcome = SearchOutcome(visual_id=visual.id, plan=plan)
        for warning in plan.warnings:
            log.warning("%s: %s", visual.id, warning, extra={"stage": "search"})

        media_type = media_type_for(visual)
        usable: list[StockProvider] = []
        for provider in self.providers:
            if not provider.is_available():
                outcome.skipped_providers[provider.name] = provider.unavailable_reason()
                continue
            if not provider.supports(media_type):
                outcome.skipped_providers[provider.name] = f"no {media_type} support"
                continue
            if visual.source_hint and visual.source_hint != provider.name:
                continue
            usable.append(provider)

        if not usable:
            log.warning(
                "%s: no usable providers (%s)",
                visual.id,
                ", ".join(f"{k}={v}" for k, v in outcome.skipped_providers.items()) or "none configured",
                extra={"stage": "search"},
            )
            return outcome

        merged: dict[str, Candidate] = {}
        jobs = [(provider, query) for provider in usable for query in plan.queries]
        workers = max(1, min(self.settings.max_parallel_searches, len(jobs)))

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search") as pool:
            futures = {
                pool.submit(self._safe_search, provider, query, media_type, per_query_limit): (
                    provider.name,
                    query,
                )
                for provider, query in jobs
            }
            for future in as_completed(futures):
                provider_name, query = futures[future]
                try:
                    results = future.result()
                except ProviderError as exc:
                    outcome.provider_errors[provider_name] = str(exc)
                    continue
                for candidate in results:
                    outcome.total_found += 1
                    existing = merged.get(candidate.key)
                    if existing is None:
                        candidate.merge_query(query)
                        merged[candidate.key] = candidate
                    else:
                        existing.merge_query(query)

        outcome.ranked = rank_candidates(list(merged.values()), visual, plan, limit=shortlist)
        log.info(
            "%s: %d unique from %d hits across %d providers × %d queries (best %.2f)",
            visual.id,
            len(merged),
            outcome.total_found,
            len(usable),
            len(plan.queries),
            outcome.best_score,
            extra={"stage": "search"},
        )
        return outcome

    def search_all(self, spec: Spec, **kwargs: object) -> dict[str, SearchOutcome]:
        """Search every visual in the spec, sequentially over an internally
        parallel search (providers rate-limit per key, not per visual)."""
        return {
            visual.id: self.search_visual(visual, spec, **kwargs)  # type: ignore[arg-type]
            for visual in spec.visuals
        }

    def _safe_search(
        self,
        provider: StockProvider,
        query: str,
        media_type: MediaType,
        limit: int,
    ) -> list[Candidate]:
        try:
            results = list(provider.search(query, media_type, limit))
        except ProviderError:
            raise
        except Exception as exc:  # a malformed payload must not kill the run
            raise ProviderError(provider.name, f"unexpected failure: {exc}") from exc
        log.debug("%s '%s' -> %d", provider.name, query, len(results), extra={"stage": "search"})
        return results
