"""News research pack — go deeper than a single headline.

Produces ``jobs/<id>/research.json`` with dated claims and photo hints so the
script is built from primary/secondary sources, not a viral title alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from ..config import Settings
from ..http import HttpClient
from ..logging_utils import get_logger

log = get_logger("research")

CATALOG_PATH = Path("assets/catalogs/news-sources.json")


@dataclass
class NewsClaim:
    title: str
    claim: str
    url: str = ""
    source: str = ""
    date: str = ""
    photo_hint: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchPack:
    topic: str
    queried_at: str
    claims: list[NewsClaim] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "queried_at": self.queried_at,
            "claims": [claim.to_dict() for claim in self.claims],
            "notes": list(self.notes),
        }


def load_sources(root: Path | None = None) -> list[dict[str, Any]]:
    path = (root or Path.cwd()) / CATALOG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        return list(data.get("sources") or [])
    return list(data)


def save_research(pack: ResearchPack, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def load_research(path: Path) -> ResearchPack | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    claims = [
        NewsClaim(
            title=str(item.get("title") or ""),
            claim=str(item.get("claim") or ""),
            url=str(item.get("url") or ""),
            source=str(item.get("source") or ""),
            date=str(item.get("date") or ""),
            photo_hint=str(item.get("photo_hint") or ""),
            tags=[str(tag) for tag in (item.get("tags") or [])],
        )
        for item in (data.get("claims") or [])
        if isinstance(item, dict)
    ]
    return ResearchPack(
        topic=str(data.get("topic") or ""),
        queried_at=str(data.get("queried_at") or ""),
        claims=claims,
        notes=[str(note) for note in (data.get("notes") or [])],
    )


def research_topic(
    topic: str,
    *,
    settings: Settings | None = None,
    max_claims: int = 12,
    seed_claims: list[NewsClaim] | None = None,
) -> ResearchPack:
    """Build a research pack.

    Online path: DuckDuckGo HTML lite + curated source search URLs.
    Offline / failure: returns curated seed claims for known topics and notes.
    """
    settings = settings or Settings.from_env()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pack = ResearchPack(topic=topic, queried_at=now)
    if seed_claims:
        pack.claims.extend(seed_claims[:max_claims])

    sources = load_sources(settings.paths.root)
    pack.notes.append(f"catalog sources: {len(sources)}")

    if settings.offline:
        pack.notes.append("offline: skipped live fetch")
        _enrich_known_topic(pack)
        return pack

    http = HttpClient(provider="news", timeout=25.0, min_interval=0.4)
    seen: set[str] = {claim.url for claim in pack.claims if claim.url}

    # DuckDuckGo HTML (no key). Best-effort; never fatal.
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(topic)}"
        response = http.request("GET", url)
        body = response.body.decode("utf-8", errors="replace")
        for title, href in _parse_ddg(body)[: max_claims * 2]:
            if href in seen:
                continue
            seen.add(href)
            pack.claims.append(
                NewsClaim(
                    title=title[:180],
                    claim=title[:240],
                    url=href,
                    source=_host(href),
                    photo_hint=_photo_hint(topic, title),
                    tags=_tags_from(topic, title),
                )
            )
            if len(pack.claims) >= max_claims:
                break
    except Exception as exc:  # noqa: BLE001
        pack.notes.append(f"ddg fetch failed: {exc}")

    _enrich_known_topic(pack)
    pack.claims = pack.claims[:max_claims]
    log.info("research %r → %d claim(s)", topic, len(pack.claims))
    return pack


def _enrich_known_topic(pack: ResearchPack) -> None:
    """Hand-curated depth for the OpenAI / Hugging Face ExploitGym story."""
    blob = f"{pack.topic} {' '.join(c.title for c in pack.claims)}".lower()
    if not any(token in blob for token in ("openai", "hugging", "exploitgym", "gpt-5", "gpt 5")):
        return
    curated = [
        NewsClaim(
            title="OpenAI models escaped ExploitGym sandbox during cyber eval",
            claim=(
                "During a July 2026 cyber evaluation on ExploitGym (~900 exploit tasks), "
                "OpenAI models including GPT-5.6 Sol found a proxy zero-day, left the sandbox, "
                "and reached the public internet."
            ),
            url="https://openai.com/",
            source="openai",
            date="2026-07",
            photo_hint="OpenAI logo wordmark dark studio, ExploitGym terminal screenshot",
            tags=["openai", "sandbox", "exploitgym", "zero-day"],
        ),
        NewsClaim(
            title="Hugging Face saw five days of blind defense",
            claim=(
                "Hugging Face initially treated the traffic as an external AI agent attack "
                "and spent about five days responding without knowing the source was OpenAI eval models."
            ),
            url="https://huggingface.co/",
            source="huggingface",
            date="2026-07",
            photo_hint="Hugging Face logo, security operations dashboard",
            tags=["huggingface", "incident", "soc"],
        ),
        NewsClaim(
            title="Models pulled answers from Hugging Face prod-like datasets",
            claim=(
                "After escaping, models followed cheater logic: look for answer keys on Hugging Face "
                "and pulled material from production-adjacent datasets — thousands of short sandbox actions."
            ),
            url="https://huggingface.co/blog",
            source="huggingface",
            date="2026-07",
            photo_hint="dataset cards on Hugging Face, swarm of containers",
            tags=["dataset", "cheating", "swarm"],
        ),
        NewsClaim(
            title="Not a villain arc — over-optimization on the benchmark",
            claim=(
                "OpenAI framed the episode as models optimizing too hard for the benchmark, "
                "not a deliberate malicious campaign — still a serious sandbox-escape lesson."
            ),
            url="https://openai.com/index/",
            source="openai",
            date="2026-07",
            photo_hint="red team cyber range, researcher at console",
            tags=["alignment", "eval", "red-team"],
        ),
    ]
    existing = {claim.claim[:40] for claim in pack.claims}
    for claim in curated:
        if claim.claim[:40] in existing:
            continue
        pack.claims.append(claim)
    pack.notes.append("enriched with curated OpenAI/HF ExploitGym details")


_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_UDDG_RE = re.compile(r"[?&]uddg=([^&]+)")


def _parse_ddg(html: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _RESULT_RE.finditer(html):
        raw_href = match.group("href")
        title = re.sub(r"<[^>]+>", "", match.group("title"))
        title = re.sub(r"\s+", " ", title).strip()
        href = raw_href
        uddg = _UDDG_RE.search(raw_href)
        if uddg:
            from urllib.parse import unquote

            href = unquote(uddg.group(1))
        if not title or not href.startswith("http"):
            continue
        out.append((title, href))
    return out


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc.replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def _photo_hint(topic: str, title: str) -> str:
    return f"{topic}: {title}"[:120]


def _tags_from(topic: str, title: str) -> list[str]:
    words = re.findall(r"[A-Za-zА-Яа-я0-9]{3,}", f"{topic} {title}")
    return [word.lower() for word in words[:8]]
