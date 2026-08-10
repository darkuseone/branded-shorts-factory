"""The mandatory wrapper around every external call.

Architectural invariant #2 (.cursorrules): nothing in `pipeline/` talks to a
network directly. Everything goes through a client that is, in this order:

    cache lookup → budget guard → retry with backoff → cost log

`BaseClient` implements that order once so no client can get it wrong, and so
`--dry-run` has a single place to intercept: with fixtures enabled, no client
ever reaches the network and a full pipeline costs nothing (§19.E13).
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..core.budget import Budget
from ..core.cache import Cache, ObjectMeta, query_key, sha256_bytes
from ..core.errors import ProviderError
from ..core.log import get_logger
from ..core.retry import CircuitBreaker, with_retries

log = get_logger("client")

USER_AGENT = "redshift-shorts-factory/0.2 (+https://github.com/darkuseone/branded-shorts-factory)"
DEFAULT_TIMEOUT = 30.0


class BaseClient:
    """Shared plumbing. Subclasses implement the provider's own calls."""

    #: Provider name used in logs, cost records and the circuit breaker.
    name = "client"
    #: Minimum seconds between requests, to stay inside published rate limits.
    min_interval = 0.0

    def __init__(
        self,
        *,
        cache: Cache,
        budget: Budget | None = None,
        breaker: CircuitBreaker | None = None,
        stage: str = "",
        fixtures: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.cache = cache
        self.budget = budget
        self.breaker = breaker or CircuitBreaker()
        self.stage = stage
        self.fixtures = fixtures
        self.timeout = timeout
        self._next_allowed = 0.0
        # Read by the @cost decorator after each call.
        self._last_call_cached = False
        self._last_tokens_in = 0
        self._last_tokens_out = 0
        self._last_credits = 0.0

    # -- capability ---------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return self.fixtures is not None

    def is_available(self) -> bool:
        """Whether this client can be called at all right now."""
        return True

    def unavailable_reason(self) -> str:
        return "" if self.is_available() else f"{self.name} is not configured"

    # -- fixtures (--dry-run) ----------------------------------------------

    def fixture(self, name: str) -> Any | None:
        """Load `tests/fixtures/<client>/<name>.json` instead of calling out."""
        if self.fixtures is None:
            return None
        path = self.fixtures / self.name / f"{name}.json"
        if not path.exists():
            log.debug("no fixture %s for %s", name, self.name, extra={"stage": self.stage})
            return None
        self._last_call_cached = True
        return json.loads(path.read_text(encoding="utf-8"))

    # -- cached calls -------------------------------------------------------

    def cached_query(
        self,
        query: str,
        params: dict[str, Any] | None,
        produce: Callable[[], Any],
    ) -> Any:
        """Run a search, answering from `cache/queries/` when possible."""
        key = query_key(self.name, query, params)
        hit = self.cache.get_query(key)
        if hit is not None:
            self._last_call_cached = True
            log.debug("cache hit: %s '%s'", self.name, query, extra={"stage": self.stage})
            return hit

        fixture = self.fixture(_slug(query))
        if fixture is not None:
            return fixture

        value = with_retries(produce, provider=self.name, breaker=self.breaker)
        self.cache.put_query(key, value)
        return value

    def cached_llm(self, key: str, produce: Callable[[], Any]) -> Any:
        """Run an LLM call, answering from `cache/llm/` when possible (§19.E4)."""
        hit = self.cache.get_llm(key)
        if hit is not None:
            self._last_call_cached = True
            return hit
        value = with_retries(produce, provider=self.name, breaker=self.breaker)
        self.cache.put_llm(key, value)
        return value

    # -- transport ----------------------------------------------------------

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self._next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_allowed = time.monotonic() + self.min_interval

    def headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT}

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """One HTTP round trip. Retries are applied by the caller, not here."""
        if self.dry_run:
            raise ProviderError(self.name, "network call attempted during a dry run")

        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                separator = "&" if urllib.parse.urlparse(url).query else "?"
                url = f"{url}{separator}{urllib.parse.urlencode(clean, doseq=True)}"

        merged = {**self.headers(), **(headers or {})}
        payload = data
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")

        self._throttle()
        request = urllib.request.Request(url, data=payload, headers=merged, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
            error = ProviderError(self.name, f"{exc.reason}: {detail}", status=exc.code)
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                with contextlib.suppress(ValueError):
                    error.retry_after = float(retry_after)  # type: ignore[attr-defined]
            raise error from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError(self.name, f"network error: {exc}") from exc

    def get_json(self, url: str, **kwargs: Any) -> Any:
        body = self.request("GET", url, **kwargs)
        return json.loads(body) if body else None

    def post_json(self, url: str, json_body: Any, **kwargs: Any) -> Any:
        body = self.request("POST", url, json_body=json_body, **kwargs)
        return json.loads(body) if body else None

    # -- downloads ----------------------------------------------------------

    def download_to_cache(
        self,
        url: str,
        *,
        suffix: str = "",
        meta: ObjectMeta | None = None,
        min_bytes: int = 4096,
    ) -> tuple[str, Path] | None:
        """Fetch a media file straight into the object store.

        Saved immediately, not at the end of the stage: a run that dies halfway
        must not lose what it already paid to download (§20.1).
        """
        if self.dry_run:
            return None
        try:
            payload = with_retries(
                lambda: self.request("GET", url, timeout=max(self.timeout, 120.0)),
                provider=self.name,
                breaker=self.breaker,
            )
        except ProviderError as exc:
            log.warning("download failed: %s", exc, extra={"stage": self.stage})
            return None

        if len(payload) < min_bytes:
            log.warning("download too small (%d bytes): %s", len(payload), url, extra={"stage": self.stage})
            return None

        record = meta or ObjectMeta(sha256=sha256_bytes(payload))
        record.src_url = url
        record.source = record.source or self.name
        return self.cache.put_object(payload, suffix=suffix, meta=record)


def _slug(text: str, limit: int = 48) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in text.lower()).strip("_")
    return cleaned[:limit] or "query"
