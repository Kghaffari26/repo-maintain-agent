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

## Architecture

```
repo-maint-agent/
├── CLAUDE.md, README.md, STATUS.md, DECISIONS.md
├── pyproject.toml          # uv-managed, Python 3.12, ruff configured
├── .env.example
├── config/repos.toml       # watched repos, settings, health penalty weights
├── agents/repo_maint/      # see README.md's module table
├── scripts/
│   ├── seed_sandbox.py     # NEVER auto-run; creates real issues/PRs
│   ├── run_report_once.py  # tonight's one-off run (temporary scaffolding)
│   └── run_agent.py        # the CI entry point (temporary scaffolding)
├── evals/repo_maint/       # §11 fixtures + runner
├── tests/repo_maint/       # one test file per agents/repo_maint/ module
├── data/repo_maint/        # state.json (committed)
├── public-data/repo_maint/ # latest.json + history/ (published output)
└── .github/workflows/agent-repo-maint.yml
```

## Rules (carried over from tonight's session, still binding)

- **Never write your own http/llm/costs/guards/publish/runner module.**
  Import from `agents_core` once it's installable (see "Needed from
  agents-core" in STATUS.md). Until then, injectable interfaces
  (`HttpClientLike` in `gh.py`, `classify_fn`/`draft_fn` in
  `triage.py`/`changelog.py`) stand in — don't fill them with a real
  implementation written from scratch here.
- **`gh.py` exposes exactly two write methods** (`add_labels`,
  `add_comment`). Don't add a third. `test_gh_client.py::test_only_two_write_methods_exist`
  and `test_gh_module_does_not_implement_its_own_networking` enforce this —
  if you find yourself wanting to bypass them, that's a sign the change
  belongs somewhere else (or needs a spec update first).
- **Apply mode is real but gated five ways** (§8.1). Never call
  `execute_actions` with a live client and `gate_passed=True` outside a
  test unless you've actually confirmed all five gates with the person
  running it. `scripts/seed_sandbox.py` in particular must never run
  without a human explicitly invoking it with `--confirm` against a repo
  they've set up as the sandbox.
- Every module under `agents/repo_maint/` has 100% of its public behavior
  covered by `tests/repo_maint/test_<module>.py`. Keep it that way — add
  tests in the same commit as the code, not after.
- Ruff (`uv run ruff check .`) must pass clean. Line length 100.
- `evals/repo_maint/fixtures.json` and `labels_proposed.json` are
  synthetic (see DECISIONS.md for exactly why real public issues weren't
  used) — don't present them as scraped data, and prefer replacing them
  with real ones once real issues exist rather than editing them by hand.

## When agents-core becomes installable

1. Confirm the commit actually has both `src/agents_core/guards.py` and
   `src/agents_core/registry.py` (or whatever its real installable layout
   ends up being) and a `[build-system]` in its `pyproject.toml`.
2. `uv add "agents-core @ git+https://github.com/Kghaffari26/agents-core@<sha>"`.
3. Delete the mirrored `Model`/`Timestamp`/`RunMeta`/`KeyStat`/etc. classes
   at the top of `schema.py` and import them from `agents_core.schema`
   instead — `RepoMaintOutput` and the rest of `schema.py` shouldn't need
   to change.
4. Replace `gh.py`'s injected-client default with `agents_core.http.HttpClient`,
   delete `scripts/run_report_once.py` and the `_TempHttpClient` in
   `scripts/run_agent.py`.
5. Wire a real `classify_fn` (§7.2 prompt) and `draft_fn` (§7.3 prompt)
   through `agents_core.llm`, and swap `triage.py`'s narrow
   `contains_number_support` guard for `agents_core.guards.verify_numbers`.
6. Register `agents/repo_maint/agent.py:AGENT` through `agents_core.registry`
   and switch the workflow to `uv run agents-run repo_maint [--apply]`.
7. Re-run the evals for real; `labels_proposed.json` is the answer key.
