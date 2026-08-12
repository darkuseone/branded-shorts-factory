"""A better bottom rung than a blank plate.

The degradation ladder (§4.6) promises the video always ships: a slot nothing
could fill falls back to the brand backdrop. That is the right *guarantee* and
the wrong *result* once it happens eight times — sixteen of forty-six seconds
came back as a plain dark background, which on screen reads as the video
having broken rather than as a design.

There is something better available and already paid for. Every accepted asset
in the same video has passed both gates for this story: it is on topic, it has
been looked at where it needed to be, and it is already downloaded. Reusing one
in an empty slot is what an editor does — the second appearance of a shot is a
different angle on the same subject, not a repeat, provided it is cut
differently and does not land next to itself.

So the ladder gains a rung between "nothing found" and "brand backdrop":

    accepted asset from this video, reused → brand backdrop

The rules that keep it from looking cheap are the interesting part, and they
are the ones an editor would state: never next to itself, never more than
twice in one video, prefer the shot whose subject is closest to what the empty
slot asked for, and give the repeat a different camera move so the eye reads
it as a new shot rather than a loop.
"""

from __future__ import annotations

from dataclasses import replace

from ..logging_utils import get_logger
from ..qa.gate import VisualQA
from ..resolver import ResolvedVisual
from ..search.keywords import tokenize

log = get_logger("backfill")

#: How often one asset may appear in a single video, reuse included. Twice is
#: a callback; three times is a loop.
MAX_APPEARANCES = 2

#: A repeat gets a move on a different axis, so the second appearance reads as
#: a new shot instead of the same one playing again.
REUSE_MOTION = "zoom_in"

#: Slots whose content is generated rather than found. An empty card is a
#: different failure — nothing about it is fixed by showing footage.
NOT_REUSABLE_TYPES = frozenset({"infographic", "card", "chart", "quote", "meme"})


def backfill_empty_slots(resolved: list[ResolvedVisual]) -> list[str]:
    """Fill what QA emptied with footage this video already earned.

    Returns one note per slot filled, for the run report. Mutates `resolved`.
    """
    pool = _pool(resolved)
    if not pool:
        return []

    appearances = {key: 1 for key, _ in pool}
    notes: list[str] = []

    for index, item in enumerate(resolved):
        if item.usable or item.visual.type in NOT_REUSABLE_TYPES:
            continue

        # Keeping two shots apart is a preference; keeping them from touching
        # is the rule. Try for breathing room first, then settle for not
        # adjacent — a video with few accepted shots would otherwise starve
        # and fall back to the blank plate this exists to avoid.
        choice = _pick(pool, item, appearances, _neighbour_keys(resolved, index, reach=2))
        if choice is None:
            choice = _pick(pool, item, appearances, _neighbour_keys(resolved, index, reach=1))
        if choice is None:
            continue

        key, source = choice
        appearances[key] += 1
        item.qa = VisualQA(
            visual_id=item.visual.id,
            outcome="accepted",
            asset=source.asset,
            attempts=item.qa.attempts,
            rejected=item.qa.rejected,
            notes=[
                *item.qa.notes,
                f"nothing passed QA; reusing the shot accepted for {source.visual.id}"
                " rather than showing an empty backdrop",
            ],
        )
        # A different axis for the repeat. The camera reads this hint and
        # answers with a push instead of the film's lateral current.
        item.visual = replace(item.visual, motion=REUSE_MOTION)
        notes.append(f"visual {item.visual.id} reuses the footage accepted for {source.visual.id}")
        log.info(
            "%s: reusing %s (empty slot, %d appearance(s))",
            item.visual.id,
            source.visual.id,
            appearances[key],
            extra={"stage": "backfill"},
        )

    return notes


def _pool(resolved: list[ResolvedVisual]) -> list[tuple[str, ResolvedVisual]]:
    """Assets worth reusing: found footage that passed, keyed by file."""
    pool: list[tuple[str, ResolvedVisual]] = []
    for item in resolved:
        if not item.usable or item.asset is None:
            continue
        if item.visual.type in NOT_REUSABLE_TYPES:
            continue
        pool.append((str(item.asset.path), item))
    return pool


def _neighbour_keys(resolved: list[ResolvedVisual], index: int, *, reach: int) -> set[str]:
    """What is on screen nearby, so a repeat never lands next to itself."""
    keys: set[str] = set()
    for offset in range(-reach, reach + 1):
        neighbour = index + offset
        if offset == 0 or not (0 <= neighbour < len(resolved)):
            continue
        asset = resolved[neighbour].asset
        if asset is not None:
            keys.add(str(asset.path))
    return keys


def _pick(
    pool: list[tuple[str, ResolvedVisual]],
    item: ResolvedVisual,
    appearances: dict[str, int],
    neighbours: set[str],
) -> tuple[str, ResolvedVisual] | None:
    """The closest subject that is not already beside this slot or overused."""
    wanted = set(tokenize(item.visual.query))
    best: tuple[float, str, ResolvedVisual] | None = None

    for key, source in pool:
        if key in neighbours or appearances.get(key, 0) >= MAX_APPEARANCES:
            continue
        overlap = len(wanted & set(tokenize(source.visual.query)))
        # Fewer previous appearances breaks ties, so reuse spreads across the
        # footage instead of leaning on one clip.
        score = overlap - 0.25 * appearances.get(key, 0)
        if best is None or score > best[0]:
            best = (score, key, source)

    if best is None:
        return None
    return best[1], best[2]
