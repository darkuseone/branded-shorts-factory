"""Query expansion — one visual slot becomes a small fan of precise queries.

Searching a single phrase is the main reason stock footage comes back generic
or wrong. For every visual we build a plan of 3–6 queries: the author's own
query and keywords, the nouns actually spoken over that moment, and modifiers
that match the requested visual type. All of them are searched in parallel and
the union is ranked, so a weak primary phrase can be rescued by a good synonym.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..spec import Spec, Visual

MAX_QUERIES_PER_VISUAL = 6

_TOKEN_RE = re.compile(r"[\w\-']+", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")

# Stop words for the two languages the scripts are written in. Kept small on
# purpose: these are only used to pick content nouns out of narration.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "with",
        "without",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "as",
        "by",
        "not",
        "no",
        "so",
        "such",
        "very",
        "more",
        "most",
        "just",
        "only",
        "own",
        "same",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "he",
        "she",
        "his",
        "her",
        "i",
        "me",
        "my",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "all",
        "can",
        "will",
        "would",
        "could",
        "should",
        "do",
        "does",
        "did",
        "done",
        "has",
        "have",
        "had",
        "having",
        "about",
        "into",
        "over",
        "under",
        "again",
        "и",
        "в",
        "во",
        "не",
        "что",
        "он",
        "на",
        "я",
        "с",
        "со",
        "как",
        "а",
        "то",
        "все",
        "она",
        "так",
        "его",
        "но",
        "да",
        "ты",
        "к",
        "у",
        "же",
        "вы",
        "за",
        "бы",
        "по",
        "только",
        "ее",
        "мне",
        "было",
        "вот",
        "от",
        "меня",
        "еще",
        "нет",
        "о",
        "из",
        "ему",
        "теперь",
        "когда",
        "даже",
        "ну",
        "вдруг",
        "ли",
        "если",
        "уже",
        "или",
        "ни",
        "быть",
        "был",
        "него",
        "до",
        "вас",
        "нибудь",
        "опять",
        "уж",
        "вам",
        "ведь",
        "там",
        "потом",
        "себя",
        "ничего",
        "ей",
        "может",
        "они",
        "тут",
        "где",
        "есть",
        "надо",
        "ней",
        "для",
        "мы",
        "тебя",
        "их",
        "чем",
        "была",
        "сам",
        "чтоб",
        "без",
        "будто",
        "чего",
        "раз",
        "тоже",
        "себе",
        "под",
        "будет",
        "ж",
        "тогда",
        "кто",
        "этот",
        "того",
        "потому",
        "этого",
        "какой",
        "совсем",
        "ним",
        "здесь",
        "этом",
        "один",
        "почти",
        "мой",
        "тем",
        "чтобы",
        "нее",
        "сейчас",
        "были",
        "куда",
        "зачем",
        "всех",
        "никогда",
        "можно",
        "при",
        "наконец",
        "два",
        "об",
        "другой",
        "хоть",
        "после",
        "над",
        "больше",
        "тот",
        "через",
        "эти",
        "нас",
        "про",
        "всего",
        "них",
        "какая",
        "много",
        "разве",
        "три",
        "эту",
        "моя",
        "впрочем",
        "хорошо",
        "свою",
        "этой",
        "перед",
        "иногда",
        "лучше",
        "чуть",
        "том",
        "нельзя",
        "такой",
        "им",
        "более",
        "всегда",
        "конечно",
        "всю",
        "между",
        "это",
        "как-то",
    ]
)

# Modifiers per visual type. These are appended to the *primary* query only —
# the whole point is to bias one or two of the parallel searches toward the
# look we want without narrowing every branch of the fan.
_TYPE_MODIFIERS: dict[str, tuple[str, ...]] = {
    "footage": ("cinematic 4k", "well lit laboratory", "vertical"),
    "image": ("high resolution", "minimal background"),
    "motion_graphics": ("motion graphics animation", "abstract loop", "particles"),
    "infographic": ("data visualization animation", "chart animation", "clean infographic"),
    "screen_record": ("ui screen recording", "interface closeup"),
    "generated": ("cinematic render",),
    "meme": (),
}

#: Phrases that bias stock toward near-black plates — strip / rescue with lit variants.
_DARK_BIAS_RE = re.compile(
    r"\b(deep void|near[- ]black|pitch black|black screen|void background|dark void)\b",
    re.IGNORECASE,
)
_LIT_RESCUE = ("well lit", "clinical lighting", "bright laboratory", "high key detail")

# Best-effort RU→EN glossary. Stock APIs are English-first, so a Cyrillic-only
# query silently returns junk. This covers the vocabulary that actually shows
# up in science/tech Shorts; anything else raises a warning instead of guessing.
_RU_EN: dict[str, str] = {
    "космос": "space",
    "космический": "space",
    "планета": "planet",
    "планеты": "planets",
    "звезда": "star",
    "звезды": "stars",
    "звёзды": "stars",
    "галактика": "galaxy",
    "вселенная": "universe",
    "чёрная": "black",
    "черная": "black",
    "дыра": "hole",
    "телескоп": "telescope",
    "ракета": "rocket",
    "спутник": "satellite",
    "земля": "earth",
    "луна": "moon",
    "солнце": "sun",
    "марс": "mars",
    "орбита": "orbit",
    "астронавт": "astronaut",
    "наука": "science",
    "учёный": "scientist",
    "ученый": "scientist",
    "лаборатория": "laboratory",
    "мозг": "brain",
    "нейрон": "neuron",
    "нейросеть": "neural network",
    "компьютер": "computer",
    "процессор": "processor chip",
    "чип": "microchip",
    "сервер": "server room",
    "данные": "data",
    "график": "chart",
    "графика": "graphics",
    "деньги": "money",
    "рынок": "stock market",
    "город": "city",
    "океан": "ocean",
    "вода": "water",
    "огонь": "fire",
    "лёд": "ice",
    "лед": "ice",
    "энергия": "energy",
    "молния": "lightning",
    "робот": "robot",
    "технологии": "technology",
    "будущее": "futuristic",
    "человек": "person",
    "люди": "people",
    "время": "time",
    "часы": "clock",
    "квант": "quantum",
    "квантовый": "quantum",
    "атом": "atom",
    "молекула": "molecule",
    "днк": "dna",
    "вирус": "virus",
    "клетка": "cell biology",
    "медицина": "medicine",
    "сердце": "heart",
    "телефон": "smartphone",
    "код": "programming code",
    "алгоритм": "algorithm",
    "карта": "map",
    "самолёт": "airplane",
    "самолет": "airplane",
    "поезд": "train",
    "машина": "car",
    "завод": "factory",
    "лес": "forest",
    "гора": "mountain",
    "горы": "mountains",
    "небо": "sky",
    "облака": "clouds",
    "ночь": "night",
    "свет": "light",
    "тьма": "darkness",
}


@dataclass
class QueryPlan:
    """The fan of queries to run for one visual slot."""

    visual_id: str
    primary: str
    queries: list[str]
    context: str = ""
    warnings: list[str] = field(default_factory=list)
    must_include: list[str] = field(default_factory=list)
    must_avoid: list[str] = field(default_factory=list)

    @property
    def all_terms(self) -> list[str]:
        """Every distinct token across the plan — the vocabulary QA scores against."""
        seen: dict[str, None] = {}
        for query in self.queries:
            for token in tokenize(query):
                seen.setdefault(token, None)
        return list(seen)


def tokenize(text: str) -> list[str]:
    """Lowercase content tokens, stop words and 1-char noise removed."""
    return [
        token.lower()
        for token in _TOKEN_RE.findall(text or "")
        if len(token) > 1 and token.lower() not in _STOPWORDS
    ]


def is_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def translate_term(term: str) -> str | None:
    """Translate a Cyrillic term with the built-in glossary, word by word."""
    words = tokenize(term)
    if not words:
        return None
    translated: list[str] = []
    for word in words:
        if not is_cyrillic(word):
            translated.append(word)
            continue
        hit = _RU_EN.get(word)
        if hit is None:
            # Try a crude stem match: Russian inflections keep the first 4–5 chars.
            stem = word[:5]
            hit = next((v for k, v in _RU_EN.items() if k.startswith(stem) and len(stem) >= 4), None)
        if hit is None:
            return None
        translated.append(hit)
    return " ".join(dict.fromkeys(translated))


def keywords_from_text(text: str, limit: int = 4) -> list[str]:
    """Pick the most informative words from narration (longer = more specific)."""
    counts: dict[str, int] = {}
    for token in tokenize(text):
        if is_cyrillic(token) and translate_term(token) is None:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [word for word, _ in ranked[:limit]]


def build_query_plan(visual: Visual, spec: Spec | None = None) -> QueryPlan:
    """Expand one visual into 3–6 complementary search queries."""
    warnings: list[str] = []
    context = spec.context_for(visual) if spec else ""

    authored = visual.all_queries
    english: list[str] = []
    for term in authored:
        if not is_cyrillic(term):
            english.append(term)
            continue
        translated = translate_term(term)
        if translated:
            english.append(translated)
        else:
            warnings.append(
                f"could not translate {term!r} for English-first stock APIs; "
                "add an English keyword to visuals[].keywords"
            )

    if not english:
        fallback = translate_term(visual.query) or ""
        if fallback:
            english.append(fallback)
        else:
            english.append(visual.query)
            warnings.append("no English query available; stock results will be unreliable")

    primary = english[0]
    primary = _DARK_BIAS_RE.sub(" ", primary)
    primary = " ".join(primary.split()).strip() or english[0]
    queries: list[str] = []

    def add(query: str) -> None:
        cleaned = " ".join(query.split()).strip()
        cleaned = _DARK_BIAS_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split()).strip()
        if not cleaned:
            return
        if cleaned.lower() in {q.lower() for q in queries}:
            return
        if len(queries) < MAX_QUERIES_PER_VISUAL:
            queries.append(cleaned)

    for term in english:
        add(term)

    for modifier in _TYPE_MODIFIERS.get(visual.type, ())[:2]:
        add(f"{primary} {modifier}")

    # If the author leaned on void/black language, force at least one lit rescue
    # so stock APIs don't return an empty dark plate.
    if any(_DARK_BIAS_RE.search(term or "") for term in authored + english):
        add(f"{primary} {_LIT_RESCUE[0]}")
        add(f"{primary} {_LIT_RESCUE[1]}")

    if context:
        extra = [word for word in keywords_from_text(context, limit=3) if word not in primary.lower()]
        for word in extra[:2]:
            translated = translate_term(word) if is_cyrillic(word) else word
            if translated:
                add(f"{primary} {translated}")

    for term in visual.must_include[:2]:
        translated = translate_term(term) if is_cyrillic(term) else term
        if translated:
            add(f"{primary} {translated}")

    return QueryPlan(
        visual_id=visual.id,
        primary=primary,
        queries=queries,
        context=context,
        warnings=warnings,
        must_include=[t.lower() for t in visual.must_include],
        must_avoid=[t.lower() for t in visual.must_avoid],
    )
