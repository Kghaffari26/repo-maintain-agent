"""Test-only stand-ins for the HTTP/LLM clients this agent expects to be
injected from ``agents_core`` in production (see DECISIONS.md and
STATUS.md -- that package isn't installable yet). Nothing here is
shipped as part of the ``agents`` package; it exists purely so tests
can exercise ``gh.py``, ``triage.py`` and ``changelog.py`` against
mocked data instead of the network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class FakeHttpResponse:
    status_code: int
    headers: httpx.Headers
    json_body: Any
    text: str


class FakeHttpClient:
    """A minimal ``HttpClientLike`` built on ``httpx.MockTransport``.

    Stands in for ``agents_core.http.HttpClient`` in tests. No retry
    logic, no caching -- just enough to drive a mocked GitHub API.
    """

    def __init__(self, handler, base_url: str = "https://api.github.com") -> None:
        self._client = httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    def request(self, method: str, url: str, **kwargs: Any) -> FakeHttpResponse:
        response = self._client.request(method, url, **kwargs)
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return FakeHttpResponse(
            status_code=response.status_code,
            headers=response.headers,
            json_body=body,
            text=response.text,
        )

    def close(self) -> None:
        self._client.close()


class FakeClock:
    """A controllable clock for cache/staleness tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeLLM:
    """A scripted stand-in for the injectable ``classify``/``draft`` callables.

    Records every call it receives (for assertions) and returns
    pre-programmed responses in order, or raises if the script runs out.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self._responses.pop(0)


def sleepless(*_args, **_kwargs) -> None:
    """A drop-in for time.sleep in tests that would otherwise slow down the suite."""
    return None


__all__ = ["FakeHttpClient", "FakeHttpResponse", "FakeClock", "FakeLLM", "sleepless", "time"]
