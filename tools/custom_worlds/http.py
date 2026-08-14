"""A very small stdlib-only HTTP client.

The crawler runs during image builds and in CI, where pulling extra dependencies is a nuisance, so
everything here is built on :mod:`urllib.request`. It adds the three things urllib does not give us
for free: retries with backoff, ``Retry-After`` handling, and dropping the ``Authorization`` header
when a redirect crosses to a different host (GitHub release assets redirect to a storage host that
rejects requests carrying both its signed query string and an ``Authorization`` header).
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "dockipelago-custom-worlds-crawler/1.0 (+https://github.com/mpcodemonkey/dockipelago)"

#: Statuses worth trying again. 403 is deliberately absent: GitHub uses it for "rate limit exceeded"
#: but also for "you may not read this", and retrying the latter just burns quota.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(Exception):
    """An HTTP request failed in a way the caller may want to inspect."""

    def __init__(self, url: str, status: int | None, reason: str, body: bytes = b"") -> None:
        self.url = url
        self.status = status
        self.reason = reason
        self.body = body
        super().__init__(f"{status or 'connection error'} for {url}: {reason}")


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop credentials when a redirect leaves the host we sent them to."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new_request.remove_header("Authorization")
        return new_request


class HttpClient:
    """Fetches URLs, retrying transient failures."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        max_attempts: int = 4,
        backoff: float = 2.0,
        max_retry_after: float = 60.0,
        sleep: Any = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_AuthStrippingRedirectHandler())

    def get(self, url: str, *, headers: Mapping[str, str] | None = None, max_bytes: int | None = None) -> bytes:
        """GET ``url`` and return the body, raising :class:`HttpError` if it never succeeds."""
        request_headers = {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        request_headers.update(headers or {})

        last_error: HttpError | None = None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(url, headers=request_headers)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return _read_body(response, url, max_bytes)
            except urllib.error.HTTPError as error:
                body = error.read(4096)
                last_error = HttpError(url, error.code, error.reason or "", body)
                if error.code not in RETRY_STATUSES:
                    raise last_error from error
                delay = self._retry_delay(attempt, error.headers.get("Retry-After"))
            except urllib.error.URLError as error:
                last_error = HttpError(url, None, str(error.reason))
                delay = self._retry_delay(attempt, None)
            except TimeoutError as error:
                last_error = HttpError(url, None, f"timed out after {self.timeout}s: {error}")
                delay = self._retry_delay(attempt, None)

            if attempt == self.max_attempts:
                break
            logger.debug("retrying %s in %.1fs (%s)", url, delay, last_error)
            self._sleep(delay)

        assert last_error is not None
        raise last_error

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        """GET ``url`` and decode the response as JSON."""
        request_headers = {"Accept": "application/json"}
        request_headers.update(headers or {})
        body = self.get(url, headers=request_headers)
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise HttpError(url, None, f"response was not valid JSON: {error}", body[:4096]) from error

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.max_retry_after)
            except ValueError:
                pass  # Retry-After may be an HTTP date; fall back to plain backoff.
        return float(self.backoff ** (attempt - 1))


def _read_body(response: Any, url: str, max_bytes: int | None) -> bytes:
    if max_bytes is None:
        return bytes(response.read())
    # Read one byte past the limit so an oversized body is detected rather than silently truncated.
    body = bytes(response.read(max_bytes + 1))
    if len(body) > max_bytes:
        raise HttpError(url, None, f"response larger than the {max_bytes} byte limit")
    return body
