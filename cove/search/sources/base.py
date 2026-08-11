"""The Source contract and the bounded HTTP facility adapters share.

Search talks to third-party indexers, so every request is deliberately
constrained: fixed timeouts, TLS always verified, a body cap, one retry at
most, and no cookies. Nothing here impersonates a browser or works around an
indexer's rate limiting - Cove identifies itself honestly and takes no for an
answer.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from cove import __version__
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind

# A slow indexer must not hold a search open indefinitely, and the caller has
# no cancellation yet, so the bound has to live here.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 15.0

# Remote bodies are untrusted: read at most this much before giving up, so a
# hostile or broken endpoint cannot stream Cove out of memory.
MAX_BODY_BYTES = 4 * 1024 * 1024

# Upper bound on normalised results a single source may contribute.
MAX_RESULTS = 200

# Honest identification. Cove does not pretend to be a browser.
USER_AGENT = f"Cove/{__version__} (+https://github.com/Sin213/cove-download-manager)"

_CHUNK = 64 * 1024


class SearchHttp:
    """Bounded HTTP for source adapters.

    One requests session per instance - that is, per search - so a search
    reuses connections across its sources without carrying state between
    searches. The session honours Cove's configured network interface, the
    same as every other direct HTTP call Cove makes.
    """

    def __init__(self, interface: str = "", *, session=None):
        self._interface = interface
        self._session = session

    def session(self):
        if self._session is None:
            from cove.netiface import bound_requests_session

            self._session = bound_requests_session(self._interface)
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        """The response body for `url`, capped at :data:`MAX_BODY_BYTES`.

        Retries once, and only for a connection-level failure: an HTTP status
        error or an oversized body is deterministic, and hammering an indexer
        that already said no is exactly the behaviour that gets Cove blocked.
        """
        import requests

        last: SourceError | None = None
        for attempt in range(2):
            try:
                return self._read(url, params)
            except SourceError as error:
                if error.kind not in (SourceErrorKind.NETWORK, SourceErrorKind.TIMEOUT):
                    raise
                last = error
            except requests.RequestException as error:  # pragma: no cover - defensive
                last = SourceError(SourceErrorKind.NETWORK, str(error))
        assert last is not None
        raise last

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        raw = self.get_bytes(url, params)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise SourceError(SourceErrorKind.PARSE, f"invalid JSON from {url}: {error}")

    def _read(self, url: str, params: dict[str, Any] | None) -> bytes:
        import requests

        session = self.session()
        try:
            response = session.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=True,
                verify=True,
                # An indexer does not get to choose where Cove sends its next
                # request: a redirect could point at loopback, a private
                # address, or plain HTTP. The endpoints here are fixed and
                # HTTPS, so a redirect is a failure, not a hop to follow.
                allow_redirects=False,
            )
        except requests.Timeout as error:
            raise SourceError(SourceErrorKind.TIMEOUT, str(error))
        except requests.RequestException as error:
            raise SourceError(SourceErrorKind.NETWORK, str(error))
        try:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                raise SourceError(SourceErrorKind.HTTP, str(error))
            if 300 <= response.status_code < 400:
                raise SourceError(
                    SourceErrorKind.HTTP,
                    f"{url} redirected to {response.headers.get('Location', 'elsewhere')}",
                )
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=_CHUNK):
                    if not chunk:
                        continue
                    body += chunk
                    if len(body) > MAX_BODY_BYTES:
                        raise SourceError(
                            SourceErrorKind.HTTP,
                            f"response body from {url} exceeds {MAX_BODY_BYTES} bytes",
                        )
            except requests.Timeout as error:
                raise SourceError(SourceErrorKind.TIMEOUT, str(error))
            except requests.RequestException as error:
                raise SourceError(SourceErrorKind.NETWORK, str(error))
            return bytes(body)
        finally:
            response.close()
            # Search is anonymous: no indexer gets to keep state on the user
            # between requests.
            session.cookies.clear()


def coerce_count(value: Any) -> int:
    """A swarm count from an untrusted row: never negative, never None.

    Indexers send these as ints, as strings, and occasionally as nonsense. A
    row is not worth discarding over a bad seeder count, so it becomes 0.
    """
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, count)


def coerce_size(value: Any) -> int | None:
    """A byte size from an untrusted row, or None when it is not usable."""
    try:
        size = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return size if size >= 0 else None


def coerce_timestamp(value: Any) -> int | None:
    """A Unix timestamp from an untrusted row, or None. 0 means "unknown"."""
    try:
        stamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp if stamp > 0 else None


class Source(ABC):
    """One built-in indexer.

    Instances are stateless and shipped with Cove; there is no registration
    hook and nothing is ever loaded from disk or the network.
    """

    id: str
    label: str
    categories: tuple[Category, ...]
    homepage: str
    # False when the indexer does not publish swarm counts at all, so callers
    # can tell "no seeders" apart from "unknown".
    reports_swarm: bool
    enabled_default: bool = True

    def serves(self, category: Category) -> bool:
        """Whether this source can answer `category`. ALL means every source."""
        return category is Category.ALL or category in self.categories

    @abstractmethod
    def search(
        self,
        query: str,
        category: Category,
        http: SearchHttp,
    ) -> list[SearchResult]:
        """Normalised results, or [] when the indexer legitimately has none.

        Raises SourceError when the indexer could not be reached or its
        response was structurally unusable.
        """
