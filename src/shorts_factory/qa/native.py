"""Level 1 — the native, logical check.

Deterministic, free and instant. It compares what the asset *says it is* against
what the narration is *talking about*, and rejects the classic failure mode:
the script says "planet" and the clip is a can of juice.

Three signals decide it:

* **lexical overlap** — asset metadata vs. the query fan and the spoken words;
* **domain conflict** — the narration maps to one topic domain, the asset maps
  hard into a different one;
* **hard constraints** — `must_include` / `must_avoid`, resolution, orientation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..media.download import LocalAsset
from ..search.keywords import QueryPlan, tokenize, translate_term
from ..spec import Visual

# Compact domain lexicon. Each domain lists the words that unambiguously belong
# to it; a candidate is "in" a domain when it hits at least two of them, which
# keeps single incidental words (a "space" in a UI description) from counting.
TOPIC_LEXICON: dict[str, frozenset[str]] = {
    "space": frozenset(
        [
            "space",
            "planet",
            "planets",
            "star",
            "stars",
            "galaxy",
            "universe",
            "cosmos",
            "nebula",
            "orbit",
            "orbital",
            "astronaut",
            "spacecraft",
            "rocket",
            "satellite",
            "telescope",
            "solar",
            "lunar",
            "mars",
            "jupiter",
            "saturn",
            "asteroid",
            "comet",
            "astronomy",
            "cosmic",
            "milky",
            "interstellar",
            "spacewalk",
            "launchpad",
        ]
    ),
    "tech": frozenset(
        [
            "computer",
            "server",
            "data",
            "code",
            "coding",
            "software",
            "hardware",
            "processor",
            "chip",
            "microchip",
            "circuit",
            "algorithm",
            "network",
            "internet",
            "cyber",
            "digital",
            "robot",
            "robotics",
            "ai",
            "artificial",
            "neural",
            "machine",
            "laptop",
            "keyboard",
            "screen",
            "interface",
            "datacenter",
            "semiconductor",
        ]
    ),
    "science_lab": frozenset(
        [
            "laboratory",
            "lab",
            "microscope",
            "experiment",
            "scientist",
            "chemistry",
            "chemical",
            "molecule",
            "atom",
            "dna",
            "genetics",
            "cell",
            "bacteria",
            "virus",
            "petri",
            "pipette",
            "research",
            "specimen",
            "biology",
            "physics",
        ]
    ),
    "medical": frozenset(
        [
            "doctor",
            "hospital",
            "surgery",
            "patient",
            "medical",
            "medicine",
            "nurse",
            "clinic",
            "health",
            "anatomy",
            "heart",
            "brain",
            "scan",
            "mri",
            "xray",
            "stethoscope",
            "pharmacy",
            "pill",
            "treatment",
            "diagnosis",
        ]
    ),
    "nature": frozenset(
        [
            "forest",
            "tree",
            "trees",
            "mountain",
            "mountains",
            "ocean",
            "sea",
            "river",
            "lake",
            "waterfall",
            "beach",
            "sunset",
            "sunrise",
            "wildlife",
            "animal",
            "bird",
            "landscape",
            "valley",
            "desert",
            "jungle",
            "sky",
            "clouds",
        ]
    ),
    "food": frozenset(
        [
            "food",
            "juice",
            "drink",
            "beverage",
            "bottle",
            "can",
            "coffee",
            "tea",
            "fruit",
            "vegetable",
            "meal",
            "restaurant",
            "kitchen",
            "cooking",
            "chef",
            "recipe",
            "snack",
            "breakfast",
            "dinner",
            "dessert",
            "cocktail",
            "smoothie",
        ]
    ),
    "finance": frozenset(
        [
            "money",
            "finance",
            "financial",
            "market",
            "stock",
            "stocks",
            "trading",
            "investment",
            "bank",
            "banking",
            "currency",
            "crypto",
            "bitcoin",
            "chart",
            "candlestick",
            "profit",
            "dollar",
            "euro",
            "coin",
            "economy",
            "budget",
        ]
    ),
    "people_office": frozenset(
        [
            "office",
            "meeting",
            "business",
            "team",
            "colleague",
            "desk",
            "workplace",
            "corporate",
            "manager",
            "presentation",
            "interview",
            "employee",
            "coworking",
            "startup",
            "conference",
        ]
    ),
    "city": frozenset(
        [
            "city",
            "urban",
            "street",
            "traffic",
            "building",
            "buildings",
            "skyline",
            "downtown",
            "road",
            "highway",
            "bridge",
            "subway",
            "metro",
            "pedestrian",
            "architecture",
            "skyscraper",
        ]
    ),
    "sport": frozenset(
        [
            "sport",
            "football",
            "soccer",
            "basketball",
            "running",
            "runner",
            "gym",
            "fitness",
            "workout",
            "athlete",
            "training",
            "stadium",
            "match",
            "race",
            "swimming",
            "cycling",
            "boxing",
        ]
    ),
    "abstract": frozenset(
        [
            "abstract",
            "particles",
            "geometric",
            "gradient",
            "loop",
            "motion",
            "animation",
            "background",
            "texture",
            "waveform",
            "fractal",
            "glow",
            "neon",
            "minimal",
            "shapes",
        ]
    ),
}

#: Domains that never conflict with anything — abstract motion works anywhere.
NEUTRAL_DOMAINS = frozenset({"abstract"})

#: Below this, the asset is not credibly about the same thing as the narration.
PASS_THRESHOLD = 0.42

#: Fraction of the primary query an asset should echo before its subject
#: coverage counts as complete, and the floor in absolute words.
PRIMARY_COVERAGE = 0.5
PRIMARY_MIN_HITS = 2

#: Above this much subject coverage, the domain classifier may not overrule
#: the author's own words.
DOMAIN_VETO_FLOOR = 0.5


@dataclass
class NativeVerdict:
    passed: bool
    score: float
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "score": round(self.score, 3),
            "issues": self.issues,
            "notes": self.notes,
        }


def domains_of(text: str, *, min_hits: int = 2) -> set[str]:
    """Which topic domains a piece of text belongs to."""
    tokens = set(tokenize(text))
    # Narration is often Russian; map what we can into the English lexicon.
    translated = {translate_term(token) or token for token in tokens}
    tokens |= {word for phrase in translated for word in phrase.split()}

    found: set[str] = set()
    for domain, vocabulary in TOPIC_LEXICON.items():
        hits = len(tokens & vocabulary)
        if hits >= min_hits or (hits == 1 and len(tokens) <= 4):
            found.add(domain)
    return found


def check_native(
    asset: LocalAsset,
    visual: Visual,
    plan: QueryPlan,
    context: str,
    *,
    threshold: float = PASS_THRESHOLD,
) -> NativeVerdict:
    """Judge one downloaded asset against the moment it has to illustrate."""
    issues: list[str] = []
    notes: list[str] = []
    candidate = asset.candidate

    metadata = candidate.searchable_text.lower()
    asset_tokens = set(tokenize(metadata))
    plan_tokens = set(plan.author_terms)
    context_tokens = set()
    for token in tokenize(context):
        translated = translate_term(token) or token
        context_tokens.update(translated.split())

    if not asset_tokens:
        # Generated assets carry their prompt as metadata; a truly blank asset
        # cannot be judged here, so defer to the vision gate rather than block.
        notes.append("no metadata; deferring to the vision gate")
        lexical = 0.5
        primary_hit = 0.0
    else:
        # The primary query carries most of the weight: the expanded fan
        # contains type modifiers ("cinematic 4k") that no honest asset is
        # obliged to mention. Missing context words are not held against an
        # asset either, so their weight is dropped when there are none.
        primary_tokens = set(tokenize(plan.primary))
        # Coverage saturates instead of demanding the whole query. A stock
        # title is three to six words; asking it to echo every token of
        # "security operations centre monitors red alert" is impossible by
        # construction, and the gate was rejecting exact matches with an empty
        # issue list — the surest sign the arithmetic, not the asset, was
        # wrong. Echoing about half the subject words means it is on topic.
        matched = len(asset_tokens & primary_tokens)
        target = max(PRIMARY_MIN_HITS, round(len(primary_tokens) * PRIMARY_COVERAGE))
        primary_hit = min(matched / target, 1.0) if primary_tokens else 0.0
        breadth = len(asset_tokens & plan_tokens) / max(len(plan_tokens), 1)
        components = [(0.55, primary_hit), (0.20, breadth)]
        if context_tokens:
            context_overlap = len(asset_tokens & context_tokens) / len(context_tokens)
            components.append((0.25, min(context_overlap * 2.5, 1.0)))
        total_weight = sum(weight for weight, _ in components)
        lexical = sum(weight * value for weight, value in components) / total_weight

    score = lexical

    # --- domain conflict -------------------------------------------------
    # Domain conflict is a blunt instrument — it multiplies the score by 0.25 —
    # so it only fires when the script itself says what the domain is. The
    # expanded query fan carries cosmetic modifiers the search layer appends
    # ("well lit laboratory"), and classifying from those made an AI-security
    # story read as science_lab, after which every honest tech clip was thrown
    # out for "reading as tech": the gate arguing with its own boilerplate.
    # When nothing confidently classifies the subject, nothing is rejected for
    # it either.
    wanted = domains_of(context) - NEUTRAL_DOMAINS
    if not wanted:
        wanted = domains_of(plan.primary) - NEUTRAL_DOMAINS
    got = domains_of(metadata) - NEUTRAL_DOMAINS
    # The classifier is a guess over a hand-written lexicon and one stray word
    # swings it: "venus temperature chart" reads as finance because of
    # "chart". The author's own query words are the ground truth, so an asset
    # that already echoes the subject cannot be vetoed by a lexicon hunch.
    lexicon_may_veto = primary_hit < DOMAIN_VETO_FLOOR
    if wanted and got and lexicon_may_veto and not (wanted & got):
        conflict = (
            f"asset reads as {'/'.join(sorted(got))} but the script is about {'/'.join(sorted(wanted))}"
        )
        issues.append(conflict)
        score *= 0.25
    elif wanted and (wanted & got):
        notes.append(f"domain match: {'/'.join(sorted(wanted & got))}")
        score = min(1.0, score + 0.15)

    # --- hard constraints -------------------------------------------------
    for term in visual.must_include:
        if term.lower() not in metadata:
            issues.append(f"missing required subject '{term}'")
            score *= 0.5
    for term in visual.must_avoid:
        if term.lower() in metadata:
            issues.append(f"contains banned subject '{term}'")
            score = 0.0

    # --- technical --------------------------------------------------------
    if asset.height and asset.height < 720:
        issues.append(f"resolution too low ({asset.width}×{asset.height})")
        score *= 0.6
    if asset.is_video and asset.duration and asset.duration + 0.3 < visual.duration:
        notes.append(f"clip is {asset.duration:g}s for a {visual.duration:g}s slot; it will loop")
    if asset.width and asset.height and asset.width > asset.height * 2.2:
        issues.append("ultra-wide source; a 9:16 crop would lose most of the frame")
        score *= 0.7

    score = max(0.0, min(score, 1.0))
    blocking = [issue for issue in issues if "banned" in issue or "reads as" in issue]
    passed = score >= threshold and not blocking
    return NativeVerdict(passed=passed, score=score, issues=issues, notes=notes)
