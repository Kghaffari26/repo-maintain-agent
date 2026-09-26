# repo-maint-agent

A standalone repo for one agent: daily GitHub issue triage, stale-PR
flagging, changelog drafting, and health scoring. Spec:
[`docs/specs/SPEC_REPO_MAINT.md`](docs/specs/SPEC_REPO_MAINT.md) — read it
before changing behavior, especially §5 (computations), §6 (the JSON
contract), §7 (LLM usage) and §8 (safety design). Where this file and the
spec disagree, the spec wins, except where DECISIONS.md records an explicit,
reasoned deviation.

**Read `STATUS.md` and `DECISIONS.md` first.** They're the actual current
state of the build and the reasoning behind every non-obvious choice —
don't re-derive decisions that are already logged there.

It's an agents-core agent: `agents-core` is pinned at `v0.1.0` in
`pyproject.toml`; its README and CLAUDE.md define the agent contract, the
number guard and the data-branch contract. Don't modify agents-core from here —
if it's missing something, list it under "Needed from agents-core" in STATUS.md.

## Architecture

```
repo-maint-agent/
├── CLAUDE.md, README.md, STATUS.md, DECISIONS.md
├── pyproject.toml          # uv-managed, Python 3.12, ruff; entry point agents_core.agents: repo_maint
├── .env.example
├── config/repos.toml       # watched repos, settings, health penalty weights
├── agents/repo_maint/      # see README.md's module table; agent.py is the Agent
├── scripts/seed_sandbox.py # NEVER auto-run; creates real issues/PRs
├── evals/repo_maint/       # §11 fixtures, free evals, live evals (--live)
├── tests/repo_maint/       # one test file per agents/repo_maint/ module
├── data/                   # costs.jsonl, guard_failures.jsonl (agents-core); repo_maint/state.json
├── public-data/            # latest.json, history/, manifest-entry.json, costs-summary.json, schema.json
└── .github/workflows/agent-repo-maint.yml   # calls agents-core's run-agent.yml
```

Run it with `uv run agents-run repo_maint [--dry-run] [--apply] [--repos a/b,c/d]`.

## Rules

- **Never write your own http/llm/costs/guards/publish/runner module.** Use
  `agents_core` (`Http`, `ctx.llm`, `CostTracker`, `guards.verify_numbers`,
  the runner's publish). The only place the Anthropic SDK may be imported is
  `agents_core.llm`; this repo never imports `anthropic` or `httpx` outside tests.
- **`gh.py` exposes exactly two write methods** (`add_labels`,
  `add_comment`). Don't add a third. `test_gh_client.py::test_only_two_write_methods_exist`
  and `test_gh_module_does_not_implement_its_own_networking` enforce this —
  if you find yourself wanting to bypass them, that's a sign the change
  belongs somewhere else (or needs a spec update first).
- **GitHub reads always pass `ttl_seconds=0`** to `Http`: freshness comes from
  GitHub ETags, whose bodies live in `state.json`, not from agents-core's dev cache.
- **Apply mode is real but gated five ways** (§8.1). Never run with
  `--apply` against live GitHub, or call `execute_actions` with a live client
  and `gate_passed=True` outside a test, unless you've actually confirmed all
  five gates with the person running it. `scripts/seed_sandbox.py` in
  particular must never run without a human explicitly invoking it with
  `--confirm` against a repo they've set up as the sandbox.
- **Numbers come from data.** Triage `summary`/`first_response` and changelog
  drafts go through `agents_core.guards.verify_numbers`; keep every published
  figure computed in `compute_repo` (the `transform` stage), never by the model.
- **`--dry-run` must stay free**: `fetch` and `transform` (`pipeline.fetch_all`,
  `compute_repo`) make no LLM calls and write nothing.
- Every module under `agents/repo_maint/` has 100% of its public behavior
  covered by `tests/repo_maint/test_<module>.py`. Keep it that way — add
  tests in the same commit as the code, not after. Tests use the real
  `agents_core.http.Http` over `httpx.MockTransport` and the real
  `agents_core.llm.LLM` over `tests/repo_maint/fakes.FakeAnthropic`.
- Ruff (`uv run ruff check .`) must pass clean. Line length 100.
- `evals/repo_maint/fixtures.json` and `labels_proposed.json` are
  synthetic (see DECISIONS.md for exactly why real public issues weren't
  used) — don't present them as scraped data, and prefer replacing them
  with real ones once real issues exist rather than editing them by hand.
  `run_evals --live` spends real money (~$0.08); don't run it casually.

## When agents-core moves past v0.1.0

1. Check its README for changes to the agent contract / data-branch contract,
   then bump the pin in `pyproject.toml` (`uv add "agents-core @ git+https://github.com/Kghaffari26/agents-core@<tag>"`)
   and the `uses: ...run-agent.yml@<tag>` lines in the workflow together.
2. If `run-agent.yml` now forwards `GITHUB_TOKEN`/`REPO_MAINT_TOKEN`/`APPLY_CHANGES`
   (see STATUS.md, "Needed from agents-core"), drop the "KNOWN LIMITS" note at
   the top of `.github/workflows/agent-repo-maint.yml` and update STATUS.md.
