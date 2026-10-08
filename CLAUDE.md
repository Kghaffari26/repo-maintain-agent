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

It's an agents-core agent: `agents-core` is pinned at v0.3.2 in `pyproject.toml`, by
commit SHA `9e4f342a06b4e74bb27d73cf759e931033fa97bf` until the `v0.3.2` tag exists
(the workflows' `uses:` refs too); its README, CHANGELOG and CLAUDE.md define the agent contract,
the number guard, the agent loop, tracing, evals and the data-branch contract. Don't modify agents-core from here —
if it's missing something, list it under "Needed from agents-core" in STATUS.md.

## Architecture

```
repo-maint-agent/
├── CLAUDE.md, README.md, STATUS.md, DECISIONS.md
├── pyproject.toml          # uv-managed, Python 3.12, ruff; entry point agents_core.agents: repo_maint
├── .env.example
├── config/repos.toml       # watched repos, settings, health penalty weights
├── agents/repo_maint/      # see README.md's module table; agent.py is the Agent
├── scripts/seed_sandbox.py # NEVER auto-run; uploads the sandbox package, creates real issues/PRs
├── scripts/sandbox_package/  # "ledgerlite": the sandbox's code, 5 deliberate bugs (eval fixture repo)
├── evals/repo_maint/       # suites.py (agents_core.evals), ci.py, fixtures, fix_fixtures/
├── evals/history.jsonl     # eval baseline the PR gate compares against; evals/results/<date>.json
├── tests/repo_maint/       # one test file per agents/repo_maint/ module
├── data/                   # costs.jsonl, eval_costs.jsonl, guard_failures.jsonl (agents-core);
│                           #   repo_maint/state.json, repo_maint/github/ (Http.download cache)
├── public-data/            # latest.json, history/, manifest-entry.json, costs-summary.json,
│                           #   schema.json, trace.json, trace.schema.json
└── .github/workflows/      # agent-repo-maint.yml (run-agent.yml), evals.yml (run-evals.yml)
```

Run it with `uv run agents-run repo_maint [--dry-run] [--apply] [--repos a/b,c/d] [--approve-fix <id>]`.
Evals: `uv run python -m evals.repo_maint.ci [--offline | --live] [--total-max-usd N] [--max-usd N] [--record]`.

## Rules

- **Never write your own http/llm/costs/guards/publish/runner module.** Use
  `agents_core` (`Http`, `ctx.llm`, `CostTracker`, `guards.verify_numbers`,
  the runner's publish). The only place the Anthropic SDK may be imported is
  `agents_core.llm`; this repo never imports `anthropic` or `httpx` outside tests.
- **`gh.py` exposes exactly three write methods**: `add_labels`, `add_comment`
  and `create_draft_pr` (§6.1, the only one marked `requires_approval`). Don't add
  a fourth. `test_gh_client.py::test_only_three_write_methods_exist`,
  `test_only_create_draft_pr_is_marked_requires_approval`,
  `test_nothing_in_gh_can_merge_or_enable_auto_merge` and
  `test_gh_module_does_not_implement_its_own_networking` enforce this. If you find
  yourself wanting to bypass them, the change belongs somewhere else (or needs a
  spec update first). Never give the fix loop a write tool.
- **Fix PRs need a human.** `create_draft_pr` must only ever be reached through
  `config.approve_fix_pr` (all five gates + `allow_fix_prs` + role `sandbox` + the
  exact proposal id in `--approve-fix`). PRs are drafts, carry the review banner,
  never merge and never enable auto-merge.
- **GitHub reads**: conditional ones go through `Http.download` (`cache_key=` in
  `gh.py`, bodies in `data/repo_maint/github/`); every other read passes
  `ttl_seconds=0` so agents-core's dev cache never serves stale issue data.
- **`temperature`** is set only on the fast tier (`config/models.toml`, 0). It needs
  agents-core >= v0.3.1, which sends it in `extra_body` (anthropic 1.8 has no
  `temperature=` keyword; case study 2). `tests/repo_maint/fakes.FakeAnthropic`
  rejects any kwarg the installed SDK doesn't accept; keep it that strict, and assert
  `kwargs["extra_body"]["temperature"]`, never `kwargs["temperature"]`.
- **Apply mode is real but gated five ways** (§8.1). Never run with
  `--apply` against live GitHub, or call `execute_actions` with a live client
  and `gate_passed=True` outside a test, unless you've actually confirmed all
  five gates with the person running it. `scripts/seed_sandbox.py` in
  particular must never run without a human explicitly invoking it with
  `--confirm` against a repo they've set up as the sandbox.
- **Numbers come from data.** Triage `summary`/`first_response`, changelog drafts
  and the fix loop's `finish` summary/rationale go through
  `agents_core.guards.verify_numbers`; keep every published figure computed in
  code (`compute_repo`, the `transform` stage), never by the model.
- **`--dry-run` must stay free**: `fetch` and `transform` (`pipeline.fetch_all`,
  `compute_repo`) make no LLM calls and write nothing.
- **No key must not crash.** Without an Anthropic key the run publishes `status: ok`
  with a warning (`agent.llm_available`); keep new LLM features behind that check.
- **Warnings, not silence.** Non-fatal problems go to `meta.warnings` (`ctx.warn` /
  `RunResult.warnings`); new §6 fields are additive only (document them in spec §6.1).
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
  `ci --live` spends real money (~$0.15 for everything); don't run it casually.
  After changing the fix loop's prompt or tools, re-record the trajectories
  (`ci --live --only fix_proposer --record`) or the offline replay suite will fail
  on a strict-replay mismatch. Commit `evals/history.jsonl` with the change.
- Tests never write into the repo's `data/`/`evals/`/`public-data/`
  (`tests/conftest.py`), and each mocked `Http` gets its own cache dir.

## When agents-core moves past v0.3.2

0. Once the `v0.3.2` tag exists, the SHA pin can be swapped for `@v0.3.2` (same
   commit, `9e4f342`) in `pyproject.toml` (then `uv lock`) and the three `uses:` refs.
1. Read its CHANGELOG and README "Migrating from …" sections, then bump the pin in
   `pyproject.toml` (`[tool.uv.sources]` `rev = "<tag or SHA>"`, then `uv lock`)
   and every `uses: ...run-agent.yml@<ref>` / `run-evals.yml@<ref>` line in
   `.github/workflows/` together.
2. Check STATUS.md's "Needed from agents-core" list against it.
3. Run the offline evals, then the live ones once, and commit the new history lines.
