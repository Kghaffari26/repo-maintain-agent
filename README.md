# repo-maint-agent

A daily agent that triages GitHub issues, flags stale PRs, drafts changelogs,
and scores repo health for a configured list of repos. Read-only by default;
apply mode (labels + one triage comment per issue) requires five separate
gates to all pass (see [`docs/specs/SPEC_REPO_MAINT.md`](docs/specs/SPEC_REPO_MAINT.md) §8.1).

It's an [agents-core](https://github.com/Kghaffari26/agents-core) agent (pinned
at `v0.1.0`): HTTP, the LLM, cost tracking, the number guard, publishing and the
runner all come from `agents_core`, and the agent is registered as `repo_maint`
under the `agents_core.agents` entry point.

**Start here:** [`STATUS.md`](STATUS.md) has the current state of the build,
what's provisional, and what needs a human. [`DECISIONS.md`](DECISIONS.md)
logs every judgment call made without asking.

## Running it

```bash
uv sync
uv run pytest                               # 317 tests, no network, no model
uv run ruff check .

uv run agents-run repo_maint --dry-run      # fetch + compute; no LLM, no writes, no publish
uv run agents-run repo_maint                # report mode: publishes public-data/
uv run agents-run repo_maint --repos a/b,c/d   # only these configured repos
```

Environment (see `.env.example`; `agents-run` loads `.env`):

| Variable | Used for |
|---|---|
| `GITHUB_TOKEN` | GitHub reads for repos with `token = "default"` |
| `REPO_MAINT_TOKEN` | repos with `token = "repo_maint"` (the sandbox) |
| `ANTHROPIC_API_KEY` or `AGENTS_ANTHROPIC_API_KEY` | triage + changelog, read by `agents_core.llm` |
| `AGENTS_CORE_MAX_RUN_USD` | per-run spend cap (CI sets `0.25`) |
| `APPLY_CHANGES` | gate 2 of §8.1; leave unset locally |

`--apply` is gate 1 of 5. Nothing writes unless all five pass — see
`agents.repo_maint.config.check_write_gates` and `actions.execute_actions`.

## Output: the agents-core data-branch contract

`public-data/` (published by `agents_core.runner`; CI force-pushes it to the
`data` branch):

```
public-data/
├── latest.json              # RepoMaintOutput — the spec §6 shape
├── history/YYYY-MM-DD.json  # 90 kept
├── manifest-entry.json      # id repo_maint, route /repos, items_count = repos watched
├── costs-summary.json
└── schema.json
```

Local run state lives in `data/`: `data/costs.jsonl` and `data/guard_failures.jsonl`
(agents-core) and `data/repo_maint/state.json` (ETags and their bodies, triage and
changelog caches, double-post protection).

## Modules

| Module | What it does |
|---|---|
| `agent.py` | The `agents_core.agent.Agent`: fetch → transform → analyze |
| `pipeline.py` | The three stages per repo, with per-repo failure isolation |
| `config.py` | `config/repos.toml` parsing, write-gate validation |
| `gh.py` | GitHub REST over `agents_core.http.Http`: ETags (+ cached bodies), pagination, rate-limit guard, exactly two writes (`add_labels`, `add_comment`) |
| `fetch.py` | All reads for one repo into a `RepoSnapshot` |
| `untriaged.py`, `metrics.py`, `duplicates.py`, `stale.py`, `health.py` | Deterministic computations (§5) |
| `triage.py` | The §7.2 prompt (fast tier, structured output), post-processing, `verify_numbers` guard, caching |
| `changelog.py` | Base ref, PR/commit content, the §7.3 prompt (smart tier), ref + number guard, deterministic fallback |
| `sanitize.py` | Comment sanitization before posting (§8.4) |
| `actions.py` | Plan → gate → execute → log (§8) |
| `schema.py` | The `latest.json` contract (§6), on `agents_core.schema` |
| `state.py` | `data/repo_maint/state.json` |

## CI

`.github/workflows/agent-repo-maint.yml` runs daily at 07:00 PT and on
dispatch. Two jobs call `Kghaffari26/agents-core/.github/workflows/run-agent.yml@v0.1.0`
(`secrets: inherit`, `max_run_usd: 0.25`, `site_repo: Kghaffari26/agents-hub`):
`report` by default, `apply` only when the repo variable `APPLY_CHANGES` is `'true'`.
**It can't do real work yet** — the reusable workflow doesn't pass `GITHUB_TOKEN`,
`REPO_MAINT_TOKEN` or `APPLY_CHANGES` to the agent (see STATUS.md).

## Evals

```bash
uv run python -m evals.repo_maint.build_fixtures        # regenerate fixtures + proposed answer key
uv run python -m evals.repo_maint.run_evals             # free evals only
uv run python -m evals.repo_maint.run_evals --live      # + live-model evals (~$0.08, capped at $0.40)
```

Results go to `evals/results/repo_maint-<date>.json`. Status stays
**PROVISIONAL**: the fixtures are synthetic, and the answer key
(`labels_proposed.json`) was proposed by an agent session and hasn't been
reviewed by a human yet.

## Safety design (§8)

- Read-only by default. Apply mode needs all five gates: `--apply` flag,
  `APPLY_CHANGES == 'true'`, the repo's `allow_apply = true`, role `own` or
  `sandbox` (never `public_demo` — enforced at config-load time), and a
  resolvable token.
- `gh.py` exposes exactly two write methods. There is no other write path —
  verified by a static introspection test (`test_gh_client.py`).
- Every write is capped (`max_writes_per_run`, `max_writes_per_repo_per_day`)
  and logged as `planned`/`applied`/`skipped`/`failed`, never silently dropped.
- Issue/PR text is fenced as untrusted in every prompt; the model has no tools;
  every model field is validated against an allowlist or closed set in code.
- Comments are sanitized before posting: mentions neutralized, non-repo URLs
  stripped, HTML/images removed, length-capped, and rejected outright on a
  marker string, "ignore previous", `system prompt`, or a secret-looking pattern.
