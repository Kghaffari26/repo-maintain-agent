#!/usr/bin/env python3
"""Runs one real report-mode pass against the repos this session can
actually reach tonight, and prints a cost report.

    uv run python -m scripts.run_report_once

**Temporary scaffolding** (see DECISIONS.md/STATUS.md): ``agents.repo_maint.gh``
and ``agents.repo_maint.pipeline`` are built to take an injected HTTP
client -- in production that's ``agents_core.http.HttpClient``, not
available yet. This script wraps ``httpx.Client`` directly, just enough
to execute tonight's one real run. It is not imported by anything under
``agents/`` and should be deleted once ``agents_core.http`` is wired in.

**Hard safety rule**: this script never passes ``apply_flag=True`` and
never sets ``APPLY_CHANGES``. It is read-only, full stop -- see
``agents.repo_maint.actions.execute_actions``, which refuses to write
without an explicit gate pass anyway.

Repo selection: this session's GitHub access is scoped per-repo (see
STATUS.md, "Needed from agents-core" / config notes). Of the 6 repos in
config/repos.toml, only the ones this session was able to attach real API
access to tonight are run; the rest are reported as skipped, not
silently dropped.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from agents.repo_maint.config import load_config
from agents.repo_maint.gh import (
    GITHUB_ACCEPT,
    GITHUB_API_BASE,
    GITHUB_API_VERSION,
    GitHubClient,
    GitHubTokenMissing,
    resolve_token,
)
from agents.repo_maint.pipeline import run
from agents.repo_maint.state import load_state, save_state

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "repos.toml"
STATE_PATH = REPO_ROOT / "data" / "repo_maint" / "state.json"
OUTPUT_DIR = REPO_ROOT / "public-data" / "repo_maint"

#: Repos this session confirmed real GitHub API access to tonight (case-
#: insensitive full_name match against config/repos.toml). Everything else
#: in config is skipped and reported, not silently dropped -- see
#: STATUS.md for why (repo doesn't exist yet, or wasn't attachable this
#: session; per the environment's own safety guard, this script never
#: tries to attach a repo on its own).
REACHABLE_TONIGHT = {
    "kghaffari26/repo-maintain-agent",
    "kghaffari26/agents-core",
    "kghaffari26/real-estate-agent",
}


@dataclass
class _Response:
    status_code: int
    headers: httpx.Headers
    json_body: Any
    text: str


class _TempHttpClient:
    """Temporary stand-in for agents_core.http.HttpClient (see module docstring)."""

    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url=GITHUB_API_BASE,
            headers={
                "Accept": GITHUB_ACCEPT,
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "Authorization": f"Bearer {token}",
            },
            timeout=30.0,
        )

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        for attempt in range(3):
            response = self._client.request(method, url, **kwargs)
            if response.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return _Response(response.status_code, response.headers, body, response.text)

    def close(self) -> None:
        self._client.close()


def main() -> int:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "NOTE: ANTHROPIC_API_KEY is set, but this script never passes a "
            "classify_fn/draft_fn -- no LLM calls will be made regardless.",
            file=sys.stderr,
        )

    config = load_config(CONFIG_PATH)
    reachable = [r for r in config.repo if r.full_name.lower() in REACHABLE_TONIGHT]
    skipped = [r for r in config.repo if r.full_name.lower() not in REACHABLE_TONIGHT]

    print(f"Repos in config: {len(config.repo)}")
    print(f"Running against {len(reachable)}: {[r.full_name for r in reachable]}")
    if skipped:
        print(f"Skipped tonight (no reachable GitHub API access this session): "
              f"{[r.full_name for r in skipped]}")

    config = config.model_copy(update={"repo": reachable})
    state = load_state(STATE_PATH)

    clients: list[_TempHttpClient] = []

    def build_client(repo):
        token = resolve_token(repo.token)
        client = _TempHttpClient(token)
        clients.append(client)
        return GitHubClient(http=client)

    try:
        output = run(
            config,
            state,
            build_client,
            apply_flag=False,  # hard safety rule: never apply from this script
            apply_changes_env=None,
        )
    except GitHubTokenMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for client in clients:
            client.close()

    save_state(STATE_PATH, state)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    latest_path = OUTPUT_DIR / "latest.json"
    latest_path.write_text(json.dumps(output.model_dump(mode="json"), indent=2) + "\n")

    history_dir = OUTPUT_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    date_str = output.meta.finished_at.strftime("%Y-%m-%d")
    (history_dir / f"{date_str}.json").write_text(
        json.dumps(output.model_dump(mode="json"), indent=2) + "\n"
    )

    print()
    print("=== Run complete ===")
    print(f"mode: {output.mode}")
    print(f"headline: {output.headline}")
    print(f"github_requests: {output.meta.github_requests}")
    print(f"github_304s: {output.meta.github_304s}")
    print(f"cost_usd: {output.meta.cost_usd:.4f}  (no ANTHROPIC_API_KEY calls were made)")
    print(f"actions planned: {len(output.actions)}")
    for repo_entry in output.repos:
        print(
            f"  - {repo_entry.full_name}: health={repo_entry.health.score} "
            f"({repo_entry.health.grade}), open_issues={repo_entry.counts.open_issues}, "
            f"untriaged={repo_entry.counts.untriaged}, stale_prs={repo_entry.counts.stale_prs}, "
            f"partial={repo_entry.partial}"
        )
    print(f"wrote {latest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
