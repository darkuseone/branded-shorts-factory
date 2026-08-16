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
            # Words stock libraries actually use for technology footage. Their
            # absence is why "portrait of a developer at a workstation" read as
            # no domain at all and scored like a holiday snap.
            "developer",
            "programmer",
            "engineer",
            "workstation",
            "monitor",
            "terminal",
            "dashboard",
            "database",
            "cloud",
            "api",
            "dataset",
            "gpu",
            "cybersecurity",
            "hacker",
            "programming",
            # The vocabulary of a security story, which is what these Shorts
            # are usually about. Without it "security operations center"
            # classified as no domain at all.
            "security",
            "infrastructure",
            "breach",
            "malware",
            "firewall",
            "encryption",
            "vulnerability",
            "exploit",
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
    # Only words that name a domestic scene outright. Everything describing a
    # *person* — portrait, smiling, posing, beauty, fashion, model — was in
    # here for one run and vetoed eleven perfectly good slots: stock titles for
    # technology b-roll are full of "portrait of a developer" and "smiling
    # engineer at a workstation". A stop-list that also stops the footage you
    # want is not a stop-list, it is an outage.
    "lifestyle_domestic": frozenset(
        [
            "salon",
            "hairdresser",
            "haircut",
            "barber",
            "barbershop",
            "makeup",
            "manicure",
            "pedicure",
            "cosmetics",
            "spa",
            "massage",
            "wedding",
            "bride",
            "groom",
            "birthday",
            "toddler",
            "puppy",
            "kitten",
            "grooming",
            "hairstyle",
            "hairstylist",
            "nightclub",
            "picnic",
            "shopping",
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

#: Domains that are never an illustration for anything else. A clip that reads
#: as a hair salon is not a weak match for a story about a model breaking into
#: a code host — it is the wrong film. These veto on sight, without the
#: coverage escape hatch the ordinary domain check allows, because the escape
#: hatch is what let a haircut open a video about AI.
HARD_VETO_DOMAINS = frozenset({"lifestyle_domestic", "food", "sport"})

#: Below this, the asset is not credibly about the same thing as the narration.
#:
#: Deliberately coarse. Word overlap cannot tell "source code on a monitor"
#: from "code repository screen" — a human calls that the same shot, the
#: tokeniser sees one word in common. Since anything thinly covered now has to
#: be *looked at* before it ships (`needs_vision`), a low bar here does not
#: mean more junk on screen; it means more candidates reach the gate that can
#: actually see them. Level 1 filters, level 2 judges.
PASS_THRESHOLD = 0.30

#: Things a scenario bans that no stock library ever admits to in words. A
#: clip's title describes its subject, never its production furniture, so
#: these bans can only be settled by looking at the frame — matching them
#: against metadata is how a ban gets marked satisfied without a single check
#: being made.
_UNSEEABLE_BANS = (
    "text",
    "caption",
    "subtitle",
    "title card",
    "watermark",
    "logo",
    "overlay",
    "smiling",
    "smile",
    "looking at camera",
)


def _only_the_frame_can_tell(term: str) -> bool:
    """Whether a `must_avoid` term is invisible to a metadata check."""
    lowered = term.casefold()
    return any(ban in lowered for ban in _UNSEEABLE_BANS)


#: What this format cannot use, whatever a slot happens to say about itself.
#: The Short lays its own word-by-word captions, data chips and cards over the
#: picture, so footage carrying somebody else's burned-in title fights them for
#: the same pixels — a NASA product with "M82 CIGAR GALAXY" across the top
#: third went to air that way. Leaving this to each slot's `must_avoid` means
#: relying on the author writing it out twenty-five times; two of twenty-five
#: had it, and the twenty-third was the one that shipped the title card.
HOUSE_BANS = (
    "burned-in titles, captions or lower thirds",
    "watermarks or channel logos",
)

#: Slot kinds the house rules apply to. A card is drawn from the scenario's own
#: content and a meme is chosen from a curated bank, so neither is found
#: footage and neither needs looking at for somebody else's text.
_FOUND_FOOTAGE_TYPES = frozenset({"footage", "photo", "image", "b_roll", "motion_graphics"})


def house_bans_for(visual: Visual) -> tuple[str, ...]:
    """The house rules that apply to this slot, if any."""
    return HOUSE_BANS if visual.type in _FOUND_FOOTAGE_TYPES else ()


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
    #: The metadata alone cannot settle it — the asset is plausible but thinly
    #: covered, or carries no words at all. Somebody has to look at the frame.
    #: With no vision gate available this is a rejection, not a pass: an empty
    #: slot falls back to the brand backdrop, which is never *wrong*, and the
    #: first cut opened with a hair salon precisely because "defer to vision"
    #: was decided in a run where vision never ran.
    needs_vision: bool = False
    #: Softer than `needs_vision`: worth a look, but shipping without one is
    #: not a failure. The house rules live here — every piece of found footage
    #: is checked for burned-in titles and watermarks, because this format
    #: lays its own captions and cards over the picture and a clip carrying
    #: somebody else's text collides with them. That is a real defect and a
    #: weak reason to empty the slot, so with no vision gate available the
    #: asset still ships. `needs_vision` keeps its harder meaning: the words
    #: did not settle it at all, so an unseen asset is a gamble, not a risk.
    worth_a_look: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "score": round(self.score, 3),
            "issues": self.issues,
            "notes": self.notes,
            "needs_vision": self.needs_vision,
            "worth_a_look": self.worth_a_look,
        }


def domains_of(text: str, *, min_hits: int = 2, strict: bool = False) -> set[str]:
    """Which topic domains a piece of text belongs to.

    A short text gets the benefit of the doubt on a single hit — "planet
    venus" is about space on the strength of one word. `strict` withdraws that,
    for callers whose verdict is final and who therefore need two independent
    words before they act.
    """
    tokens = set(tokenize(text))
    # Narration is often Russian; map what we can into the English lexicon.
    translated = {translate_term(token) or token for token in tokens}
    tokens |= {word for phrase in translated for word in phrase.split()}

    found: set[str] = set()
    for domain, vocabulary in TOPIC_LEXICON.items():
        hits = len(tokens & vocabulary)
        if hits >= min_hits or (not strict and hits == 1 and len(tokens) <= 4):
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

    thin_coverage = False
    if not asset_tokens:
        # Generated assets carry their prompt as metadata; a truly blank asset
        # cannot be judged here, so defer to the vision gate rather than block.
        notes.append("no metadata; only the vision gate can judge this")
        lexical = 0.5
        primary_hit = 0.0
        thin_coverage = True
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
        # One word in common with a three-word subject is not a match, it is a
        # coincidence: "office" is shared by an OpenAI headquarters and by a
        # stranger walking down a corridor. Plausible enough to look at, never
        # enough to ship unseen.
        thin_coverage = bool(primary_tokens) and matched < min(PRIMARY_MIN_HITS, len(primary_tokens))
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
    # Two independent words, never the one-word shortcut `domains_of` allows
    # for short texts: a veto this absolute has to be sure, and "a developer
    # shopping for a laptop" is not a shopping video.
    hard = domains_of(metadata, min_hits=2, strict=True) & HARD_VETO_DOMAINS
    if hard and not (wanted & hard):
        # No coverage argument saves this one. Lexical overlap on a word like
        # "face" or "model" is exactly how a salon and a portrait got in.
        issues.append(f"asset reads as {'/'.join(sorted(hard))}, which never illustrates this")
        score = 0.0
    elif wanted and got and lexicon_may_veto and not (wanted & got):
        conflict = (
            f"asset reads as {'/'.join(sorted(got))} but the script is about {'/'.join(sorted(wanted))}"
        )
        issues.append(conflict)
        score *= 0.25
    elif wanted and (wanted & got):
        matched_domains = "/".join(sorted(wanted & got))
        if thin_coverage:
            # The asset is about the right subject and shares none of the words
            # for it — "portrait of a developer at a workstation" against "code
            # repository screen". A lexical gate cannot settle that either way,
            # so it goes to the gate that can see the frame rather than being
            # thrown out for a vocabulary mismatch. Without a vision gate this
            # still ends as a rejection; it never ships unseen.
            notes.append(f"domain match: {matched_domains}; no shared words, so worth a look")
            score = max(score, threshold)
        else:
            # The bonus is a reward for agreeing with the author's own words,
            # so it is withheld when those words are barely echoed. It was
            # worth +0.15, which is precisely what carried single-generic-word
            # matches over the line: 0.293 became 0.443 against a 0.42 bar.
            notes.append(f"domain match: {matched_domains}")
            score = min(1.0, score + 0.15)

    # --- hard constraints -------------------------------------------------
    for term in visual.must_include:
        if term.lower() not in metadata:
            issues.append(f"missing required subject '{term}'")
            score *= 0.5
    unseeable_avoid = False
    for term in visual.must_avoid:
        if term.lower() in metadata:
            issues.append(f"contains banned subject '{term}'")
            score = 0.0
        elif _only_the_frame_can_tell(term):
            # "text overlay", "watermark", "stock model smiling" — none of
            # these are ever written down. A stock title says "Astrophysics
            # Multiwavelength Vertical Video", not "has our name burned into
            # the top third", so checking the metadata for them passes every
            # time and the ban reads as satisfied when nothing was checked.
            # A NASA product with its own title card shipped that way. The
            # ban is real, so the candidate has to be looked at.
            unseeable_avoid = True

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
    if passed and thin_coverage:
        notes.append("thin subject coverage; needs a look at the frame")
    if passed and unseeable_avoid:
        notes.append("carries a must_avoid only the frame can settle")
    return NativeVerdict(
        passed=passed,
        score=score,
        issues=issues,
        notes=notes,
        needs_vision=passed and (thin_coverage or unseeable_avoid),
        worth_a_look=passed and bool(house_bans_for(visual)),
    )
