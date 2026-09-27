"""Test-only helpers: the real ``agents_core.http.Http`` and ``agents_core.llm.LLM``,
driven offline. ``Http`` gets an ``httpx.MockTransport`` (so no request leaves the
process) and a no-op sleep; ``LLM`` gets a scripted stand-in for the Anthropic
client object it would otherwise build. Nothing here is shipped in ``agents``.
"""

from __future__ import annotations

import inspect
import itertools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
from agents_core.costs import CostTracker
from agents_core.http import Http
from agents_core.llm import LLM

from agents.repo_maint.gh import GitHubClient


def sleepless(*_args, **_kwargs) -> None:
    """A drop-in for time.sleep in tests that would otherwise slow down the suite."""
    return None


def mock_http(handler, cache_dir: Path | None = None) -> Http:
    """A real agents_core Http whose network is ``handler`` (httpx.MockTransport)."""
    return Http(
        cache_dir=cache_dir or Path("/nonexistent-http-cache"),
        transport=httpx.MockTransport(handler),
        sleep=sleepless,
        max_attempts=1,
    )


def gh_client(handler, **kwargs: Any) -> GitHubClient:
    return GitHubClient(mock_http(handler), token="test-token", **kwargs)


# -- a scripted Anthropic client for agents_core.llm ---------------------------------------


def _check_sdk_kwargs(method: str, kwargs: dict[str, Any]) -> None:
    """Reject what the real SDK would: every kwarg must be a parameter of the installed
    ``anthropic`` ``Messages.create``/``parse``. (A live eval caught agents-core sending
    ``temperature``, which anthropic 1.8 no longer accepts; a lenient fake had hidden it.)"""
    real = getattr(anthropic.resources.messages.Messages, method)
    allowed = set(inspect.signature(real).parameters)
    unknown = sorted(set(kwargs) - allowed)
    if unknown:
        raise TypeError(f"Messages.{method}() got unexpected keyword arguments {unknown}")


class FakeAnthropic:
    """Stands in for ``anthropic.Anthropic`` inside ``agents_core.llm.LLM``.

    ``responses`` are consumed in order: a ``str`` answers ``messages.create``
    (plain text), a ``turn(...)`` dict answers ``messages.create`` with content
    blocks (a tool-use loop step, see ``turn``/``tool_use``), and a pydantic model or
    other dict answers ``messages.parse`` (structured output; a dict is validated
    against the requested ``output_format``).
    Every call's kwargs are recorded in ``calls``.
    """

    def __init__(self, responses: list[Any], *, input_tokens: int = 1000, output_tokens: int = 200):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self._usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        self.messages = SimpleNamespace(create=self._create, parse=self._parse)
        self._ids = itertools.count(1)

    def _next(self, kwargs: dict[str, Any], method: str) -> Any:
        _check_sdk_kwargs(method, kwargs)
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropic ran out of scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def _message(self, **extra: Any) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"msg_{next(self._ids)}", stop_reason="end_turn", usage=self._usage, **extra
        )

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        value = self._next(kwargs, "create")
        if isinstance(value, dict) and "content" in value:
            message = self._message(content=[SimpleNamespace(**b) for b in value["content"]])
            message.stop_reason = value.get("stop_reason", "tool_use")
            return message
        return self._message(content=[SimpleNamespace(type="text", text=value)])

    def _parse(self, *, output_format: Any, **kwargs: Any) -> SimpleNamespace:
        value = self._next({**kwargs, "output_format": output_format}, "parse")
        if isinstance(value, dict):
            value = output_format.model_validate(value)
        return self._message(content=[], parsed_output=value)


def fake_llm(responses: list[Any], tmp_path: Path, *, max_usd: float = 1.0) -> tuple[LLM, Any]:
    """An ``agents_core.llm.LLM`` over ``FakeAnthropic``, logging costs under tmp_path."""
    tracker = CostTracker(
        agent="repo_maint_test", run_id="test", max_usd=max_usd, path=tmp_path / "costs.jsonl"
    )
    client = FakeAnthropic(responses)
    return LLM(tracker, client=client, sleep=sleepless), client


# -- scripted tool-use turns (agents_core.agent_loop) ---------------------------------------

_tool_ids = itertools.count(1)


def tool_use(name: str, **tool_input: Any) -> dict[str, Any]:
    """One ``tool_use`` content block."""
    return {"type": "tool_use", "id": f"toolu_{next(_tool_ids)}", "name": name, "input": tool_input}


def turn(*blocks: dict[str, Any], text: str = "", stop_reason: str = "tool_use") -> dict[str, Any]:
    """One scripted assistant turn for ``FakeAnthropic`` (``messages.create``)."""
    content = ([{"type": "text", "text": text}] if text else []) + list(blocks)
    return {"content": content, "stop_reason": stop_reason}
