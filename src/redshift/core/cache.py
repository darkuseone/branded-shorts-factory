"""Three-level cache (§4.2). The single biggest saving in the whole project.

    cache/objects/<sha256>[.ext]  downloaded media, kept forever, plus a
                                  <sha256>.meta.json describing it
    cache/queries/<sha1>.json     search results, TTL 14 days
    cache/llm/<sha1>.json         LLM replies, TTL 30 days

Rule from §4.2, no exceptions: every network call does a cache lookup first.
The object store is shared between runs — 40-60% of b-roll gets reused between
videos on the same pillar, which is exactly where the money is saved.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger("cache")

DAY_SECONDS = 86_400


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def query_key(source: str, query: str, params: dict[str, Any] | None = None) -> str:
    basis = json.dumps(
        {"source": source, "query": query, "params": params or {}}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def llm_key(model: str, prompt: str, params: dict[str, Any] | None = None) -> str:
    basis = json.dumps(
        {"model": model, "prompt": prompt, "params": params or {}}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


@dataclass
class ObjectMeta:
    """`<sha256>.meta.json` — everything L0 needs to judge an asset for free."""

    sha256: str
    src_url: str = ""
    source: str = ""
    license: str = ""
    author: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    phash: str = ""
    motion_score: float = 0.0
    sharpness: float = 0.0
    colors: list[str] = field(default_factory=list)
    acid_share: float = 0.0
    faces: int = 0
    has_text: bool = False
    watermark_suspect: bool = False
    first_seen: float = 0.0
    used_in_published: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Cache:
    """Content-addressed store plus two TTL caches."""

    def __init__(self, root: Path, *, queries_ttl_days: int = 14, llm_ttl_days: int = 30):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.queries = self.root / "queries"
        self.llm = self.root / "llm"
        self.queries_ttl = queries_ttl_days * DAY_SECONDS
        self.llm_ttl = llm_ttl_days * DAY_SECONDS
        for directory in (self.objects, self.queries, self.llm):
            directory.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    # -- objects ------------------------------------------------------------

    def object_path(self, digest: str, suffix: str = "") -> Path:
        return self.objects / f"{digest}{suffix}"

    def meta_path(self, digest: str) -> Path:
        return self.objects / f"{digest}.meta.json"

    def has_object(self, digest: str) -> bool:
        return bool(self.find_object(digest))

    def find_object(self, digest: str) -> Path | None:
        for candidate in self.objects.glob(f"{digest}*"):
            if candidate.is_file() and not candidate.name.endswith(".meta.json"):
                return candidate
        return None

    def put_object(
        self, payload: bytes, *, suffix: str = "", meta: ObjectMeta | None = None
    ) -> tuple[str, Path]:
        """Store bytes under their own sha256. Idempotent by construction."""
        digest = sha256_bytes(payload)
        existing = self.find_object(digest)
        if existing is not None:
            self.hits += 1
            return digest, existing

        target = self.object_path(digest, suffix)
        _atomic_write_bytes(target, payload)
        record = meta or ObjectMeta(sha256=digest)
        record.sha256 = digest
        record.first_seen = record.first_seen or time.time()
        self.put_meta(record)
        self.misses += 1
        return digest, target

    def put_meta(self, meta: ObjectMeta) -> None:
        _atomic_write_text(
            self.meta_path(meta.sha256), json.dumps(meta.to_dict(), ensure_ascii=False, indent=2)
        )

    def get_meta(self, digest: str) -> ObjectMeta | None:
        path = self.meta_path(digest)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        known = {f for f in ObjectMeta.__dataclass_fields__}
        return ObjectMeta(**{k: v for k, v in raw.items() if k in known})

    def all_meta(self) -> list[ObjectMeta]:
        out: list[ObjectMeta] = []
        for path in self.objects.glob("*.meta.json"):
            meta = self.get_meta(path.name.removesuffix(".meta.json"))
            if meta:
                out.append(meta)
        return out

    # -- TTL caches ---------------------------------------------------------

    def get_query(self, key: str) -> Any | None:
        return self._get_ttl(self.queries / f"{key}.json", self.queries_ttl)

    def put_query(self, key: str, value: Any) -> None:
        self._put_ttl(self.queries / f"{key}.json", value)

    def get_llm(self, key: str) -> Any | None:
        return self._get_ttl(self.llm / f"{key}.json", self.llm_ttl)

    def put_llm(self, key: str, value: Any) -> None:
        self._put_ttl(self.llm / f"{key}.json", value)

    def _get_ttl(self, path: Path, ttl: float) -> Any | None:
        if not path.exists():
            self.misses += 1
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        if time.time() - float(envelope.get("stored_at", 0)) > ttl:
            path.unlink(missing_ok=True)
            self.misses += 1
            return None
        self.hits += 1
        return envelope.get("value")

    def _put_ttl(self, path: Path, value: Any) -> None:
        _atomic_write_text(path, json.dumps({"stored_at": time.time(), "value": value}, ensure_ascii=False))

    # -- housekeeping -------------------------------------------------------

    def purge_expired(self) -> int:
        """Drop stale query/llm entries. Objects are never touched (§20.4)."""
        removed = 0
        now = time.time()
        for directory, ttl in ((self.queries, self.queries_ttl), (self.llm, self.llm_ttl)):
            for path in directory.glob("*.json"):
                try:
                    stored = float(json.loads(path.read_text(encoding="utf-8")).get("stored_at", 0))
                except (OSError, json.JSONDecodeError):
                    stored = 0.0
                if now - stored > ttl:
                    path.unlink(missing_ok=True)
                    removed += 1
        if removed:
            log.info("purged %d expired cache entries", removed, extra={"stage": "cache"})
        return removed

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))
