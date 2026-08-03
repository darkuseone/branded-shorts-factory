"""Multi-keyword, multi-source stock search."""

from .aggregator import SearchAggregator, SearchOutcome
from .candidates import Candidate
from .keywords import QueryPlan, build_query_plan
from .ranking import RankedCandidate, rank_candidates

__all__ = [
    "Candidate",
    "QueryPlan",
    "RankedCandidate",
    "SearchAggregator",
    "SearchOutcome",
    "build_query_plan",
    "rank_candidates",
]
