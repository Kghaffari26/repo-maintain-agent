#!/usr/bin/env python3
"""The CI entry point: `.github/workflows/agent-repo-maint.yml` calls this.

    uv run python -m scripts.run_agent [--apply]

**Temporary scaffolding** (see DECISIONS.md/STATUS.md): the intended
production entry point is `uv run agents-run repo_maint [--apply]`
through `agents_core`'s registry/runner, per tonight's instructions.
That package isn't installable yet (no commit of
Kghaffari26/agents-core has a `src/agents_core/{guards,registry}.py`
package -- its actual layout is a non-packaged monorepo). This script
is a stand-in: same shape (config -> pipeline.run -> publish -> commit),
built so swapping it for `agents_core.runner` later is a small, mostly
mechanical change, not a rewrite. It wraps `httpx.Client` directly for
the same reason `agents.repo_maint.gh` needs an injected client but has
no networking of its own -- this script is that injection point until
`agents_core.http` exists.

Unlike `scripts/run_report_once.py` (tonight's one-off, restricted to
repos this session could personally reach), this script runs against
every repo in config/repos.toml -- that's what CI will actually have
credentials for once REPO_MAINT_TOKEN is configured.

Known limitation (documented, not fixed tonight): a single repo that
fails hard (bad token, repo renamed/deleted, etc.) currently fails the
whole run rather than being isolated and reported per-repo -- see
STATUS.md.
"""

from __future__ import annotations

import argparse
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
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "repos.toml"
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "repo_maint" / "state.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "public-data" / "repo_maint"


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
        response = None
        for attempt in range(3):
            response = self._client.request(method, url, **kwargs)
            if response.status_code >= 500 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        assert response is not None
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return _Response(response.status_code, response.headers, body, response.text)

    def close(self) -> None:
        self._client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Enable apply mode (gate 1 of 5, §8.1)"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_changes_env = os.environ.get("APPLY_CHANGES")
    max_run_usd = float(os.environ.get("MAX_RUN_USD", "0.25"))

    config = load_config(args.config)
    repos_filter = os.environ.get("REPOS_FILTER", "").strip()
    if repos_filter:
        wanted = {name.strip().lower() for name in repos_filter.split(",") if name.strip()}
        filtered = [r for r in config.repo if r.full_name.lower() in wanted]
        config = config.model_copy(update={"repo": filtered})
    state = load_state(args.state)
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
            apply_flag=args.apply,
            apply_changes_env=apply_changes_env,
        )
    except GitHubTokenMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for client in clients:
            client.close()

    if output.meta.cost_usd > max_run_usd:
        # No live LLM calls are made yet (see module docstring), so this never
        # trips today, but the check is here for when agents_core.costs lands.
        print(
            f"ERROR: run cost ${output.meta.cost_usd:.4f} exceeded MAX_RUN_USD ${max_run_usd:.2f}",
            file=sys.stderr,
        )
        return 1

    save_state(args.state, state)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    latest_path = args.out_dir / "latest.json"
    latest_path.write_text(json.dumps(output.model_dump(mode="json"), indent=2) + "\n")

    history_dir = args.out_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    date_str = output.meta.finished_at.strftime("%Y-%m-%d")
    (history_dir / f"{date_str}.json").write_text(
        json.dumps(output.model_dump(mode="json"), indent=2) + "\n"
    )

    print(f"mode={output.mode} repos={len(output.repos)} actions={len(output.actions)} "
          f"cost_usd={output.meta.cost_usd:.4f} github_requests={output.meta.github_requests}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
