"""A small, dependency-free HTTP client with retries and polite rate limiting.

`urllib` is used on purpose: the orchestrator must run on a bare Python 3.11
without a `pip install` step. Everything the providers need — JSON calls,
binary downloads, backoff on 429/5xx — lives here so the adapters stay short.
"""

from __future__ import annotations

import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProviderError
from .logging_utils import get_logger

log = get_logger("http")

USER_AGENT = "branded-shorts-factory/0.1 (+https://github.com/darkuseone/branded-shorts-factory)"
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _safe_request_url(url: str) -> str:
    """Percent-encode path/query so spaces and quotes don't crash urlopen."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@+$,;")
    query = urllib.parse.quote(parts.query, safe="=&%:@+$,;")
    fragment = urllib.parse.quote(parts.fragment, safe="=&%:@+$,;")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, fragment))


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


class RateLimiter:
    """Token-free minimum-interval limiter, safe to share across threads."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self.min_interval
        if sleep_for > 0:
            time.sleep(sleep_for)


class HttpClient:
    """Retrying HTTP client scoped to one provider (for error messages)."""

    def __init__(
        self,
        *,
        provider: str = "http",
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.5,
        min_interval: float = 0.0,
        default_headers: Mapping[str, str] | None = None,
    ):
        self.provider = provider
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.limiter = RateLimiter(min_interval)
        self.default_headers = {"User-Agent": USER_AGENT, **(default_headers or {})}

    # -- core ---------------------------------------------------------------

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
    ) -> Response:
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if urllib.parse.urlparse(url).query else "?"
                url = f"{url}{sep}{urllib.parse.urlencode(clean, doseq=True)}"

        merged = dict(self.default_headers)
        merged.update(headers or {})
        payload = data
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")

        last_error: str = "unknown error"
        last_status: int | None = None
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            request = urllib.request.Request(
                _safe_request_url(url), data=payload, headers=merged, method=method.upper()
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as raw:
                    return Response(raw.status, dict(raw.headers), raw.read())
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                detail = _short(exc.read())
                last_error = f"{exc.reason}: {detail}" if detail else str(exc.reason)
                if exc.code not in RETRY_STATUSES or attempt == self.retries:
                    raise ProviderError(self.provider, last_error, status=exc.code) from exc
                delay = _retry_after(exc.headers) or self._delay(attempt)
            except http.client.IncompleteRead as exc:
                got = len(exc.partial)
                expected = got + exc.expected if exc.expected else None
                last_error = f"connection closed early ({got}B read" + (f" of {expected}B)" if expected else ")")
                if attempt == self.retries:
                    raise ProviderError(self.provider, last_error) from exc
                delay = self._delay(attempt)
            except (urllib.error.URLError, http.client.InvalidURL, ValueError) as exc:
                last_error = f"network error: {getattr(exc, 'reason', exc)}"
                if attempt == self.retries:
                    raise ProviderError(self.provider, last_error) from exc
                delay = self._delay(attempt)
            except TimeoutError as exc:
                last_error = "timed out"
                if attempt == self.retries:
                    raise ProviderError(self.provider, last_error) from exc
                delay = self._delay(attempt)

            log.debug(
                "%s %s failed (%s), retry %d/%d in %.1fs",
                method,
                url,
                last_error,
                attempt + 1,
                self.retries,
                delay,
            )
            time.sleep(delay)

        raise ProviderError(self.provider, last_error, status=last_status)

    def _delay(self, attempt: int) -> float:
        return (self.backoff**attempt) * (1.0 + random.random() * 0.25)

    # -- conveniences -------------------------------------------------------

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, json_body: Any, **kwargs: Any) -> Any:
        return self.request("POST", url, json_body=json_body, **kwargs).json()

    def download(
        self,
        url: str,
        destination: Path,
        *,
        headers: Mapping[str, str] | None = None,
        overwrite: bool = False,
        min_bytes: int = 1024,
    ) -> Path:
        """Download to ``destination`` atomically; returns the final path."""
        if destination.exists() and destination.stat().st_size >= min_bytes and not overwrite:
            log.debug("cache hit for %s", destination.name)
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self.request("GET", url, headers=headers, timeout=max(self.timeout, 120.0))
        if len(response.body) < min_bytes:
            raise ProviderError(self.provider, f"download too small ({len(response.body)}B): {url}")

        tmp = destination.with_suffix(destination.suffix + ".part")
        tmp.write_bytes(response.body)
        tmp.replace(destination)
        return destination


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(float(raw), 30.0)
    except ValueError:
        return None


def _short(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", errors="replace").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")
