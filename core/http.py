"""A small retrying HTTP client shared by agents.

Agent-specific clients (e.g. ``agents.repo_maint.gh.GitHubClient``) wrap
this rather than talking to ``httpx`` directly, so retry/backoff and
timeout behavior stay consistent across agents.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


@dataclass
class HttpResponse:
    """A normalized response, decoupled from the underlying HTTP library."""

    status_code: int
    headers: httpx.Headers
    json_body: Any
    text: str


class HttpClient:
    """A thin, retrying wrapper over ``httpx.Client``."""

    def __init__(
        self,
        base_url: str = "",
        default_headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers=default_headers or {},
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
                time.sleep(self.backoff_seconds * (2**attempt))
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries - 1:
                time.sleep(self.backoff_seconds * (2**attempt))
                continue

            return _to_http_response(response)

        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _to_http_response(response: httpx.Response) -> HttpResponse:
    body: Any = None
    if response.content:
        try:
            body = response.json()
        except ValueError:
            body = None
    return HttpResponse(
        status_code=response.status_code,
        headers=response.headers,
        json_body=body,
        text=response.text,
    )
