# repo-maint-agent

A daily agent that triages GitHub issues, flags stale PRs, drafts changelogs,
and scores repo health for a configured list of repos. Read-only by default;
apply mode (labels + one triage comment per issue) requires five separate
gates to all pass (see [`docs/specs/SPEC_REPO_MAINT.md`](docs/specs/SPEC_REPO_MAINT.md) §8.1).

**Start here:** [`STATUS.md`](STATUS.md) has the current state of the build,
what's provisional, and exactly what to do next. [`DECISIONS.md`](DECISIONS.md)
is a running log of every judgment call made without asking, in case one
needs revisiting.

## What's implemented

Everything under `agents/repo_maint/` except the two pieces that need a
package this repo can't install yet (see below):

| Module | What it does |
|---|---|
| `config.py` | `config/repos.toml` parsing, write-gate validation |
| `gh.py` | GitHub REST reads (ETags, pagination, rate-limit guard) + exactly two writes (`add_labels`, `add_comment`) |
| `fetch.py` | Orchestrates all reads for one repo into a `RepoSnapshot` |
| `untriaged.py`, `metrics.py`, `duplicates.py`, `stale.py`, `health.py` | Deterministic computations (§5) |
| `triage.py` | Issue triage post-processing: label allowlisting, priority escalation, duplicate confirmation, caching |
| `changelog.py` | Base-ref selection, PR/commit content, the §7.3 ref guard, deterministic fallback |
| `sanitize.py` | Comment sanitization before posting (§8.4) |
| `actions.py` | Plan → gate → execute → log (§8) |
| `schema.py` | The `latest.json` output contract (§6) |
| `state.py` | `data/repo_maint/state.json` read/write |
| `pipeline.py` | Ties everything above into one run per repo |

## What's not wired in yet

`gh.py`'s HTTP client and `triage.py`/`changelog.py`'s LLM calls are all
**injected**, not implemented here — per this session's hard rule, this repo
never writes its own http/llm/costs/guards/publish/runner module. Those come
from `agents_core`, which isn't installable yet (see STATUS.md). Until then:

- `scripts/run_report_once.py` / `scripts/run_agent.py` wrap `httpx.Client`
  directly as a **temporary** stand-in, clearly marked for deletion once
  `agents_core.http` exists.
- Triage and changelog runs with no LLM injected simply produce no fresh
  triage results (cache hits only) and always fall back to the deterministic
  changelog grouping — not a workaround, that fallback is itself spec
  behavior (§7.3 step 3).

## Running it

```bash
uv sync
uv run pytest              # 261 tests
uv run ruff check .

# One real report-mode run against whatever repos this environment has
# GitHub API access to (see the REACHABLE_TONIGHT note in the script):
uv run python -m scripts.run_report_once

# The general CI entry point (every repo in config/repos.toml):
uv run python -m scripts.run_agent [--apply]
```

Nothing here ever makes a write request unless every gate in §8.1 passes —
see `agents.repo_maint.config.check_write_gates` and
`agents.repo_maint.actions.execute_actions`.

## Evals

```bash
uv run python -m evals.repo_maint.build_fixtures   # regenerate fixtures.json / labels_proposed.json
uv run python -m evals.repo_maint.run_evals         # writes evals/results/repo_maint-<date>.json
```

`injection_resistance` and `changelog_fidelity` run for real, at zero cost.
`classification_accuracy`, `priority`, `label_allowlist`, and
`duplicate_confirmation` are `provisional_not_run` — they need a live model
call. See STATUS.md.

## Safety design (§8)

- Read-only by default. Apply mode needs all five gates: `--apply` flag,
  `APPLY_CHANGES == 'true'`, the repo's `allow_apply = true`, role `own` or
  `sandbox` (never `public_demo` — enforced at config-load time, not just at
  write time), and a resolvable token.
- `gh.py` exposes exactly two write methods. There is no other write path —
  verified by a static introspection test (`test_gh_client.py`).
- Every write is capped (`max_writes_per_run`, `max_writes_per_repo_per_day`)
  and logged as `planned`/`applied`/`skipped`/`failed`, never silently
  dropped.
- Comments are sanitized before posting: mentions neutralized, non-repo URLs
  stripped, HTML/images removed, length-capped, and rejected outright on a
  marker string, "ignore previous", `system prompt`, or a secret-looking
  pattern.
