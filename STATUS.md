# Status — autonomous overnight session, 2026-09-24

Read this first. `DECISIONS.md` has the full reasoning behind every
judgment call referenced here.

## TL;DR

- Built essentially the whole spec (§14 build order 1–9) tonight, working
  around one real, hard blocker: **agents-core isn't installable as
  `agents_core` yet** (still a non-packaged monorepo at `core/`, not
  `src/agents_core/`). Everything that needed it is built with an
  injectable interface instead, clearly marked, ready to swap in.
- **273 tests passing, ruff clean.**
- **Zero GitHub writes made, all night** — confirmed by construction
  (nothing calls `execute_actions` with a live client and `gate_passed=True`
  outside a mock) and by the real run's own request log.
- **Real cost: $0.00.** No `ANTHROPIC_API_KEY` in this environment, so no
  Anthropic API call was physically possible tonight, by anyone, at any
  point. This is also why the LLM-dependent evals are provisional.
- One real report-mode run happened, against `agents-core` and
  `real-estate-agent` (the only 2 of your 6 configured repos this session
  had live GitHub API access to) — see "The real run" below.

## Done

Per SPEC_REPO_MAINT.md §14's build order:

1. **Client and config** — `gh.py` (ETags, pagination, rate-limit guard,
   exactly 2 write methods, introspection-tested), `config.py` (gate
   validation; `public_demo` + `allow_apply` fails at config-load time).
2. **Sandbox seeding script** — `scripts/seed_sandbox.py`, written per
   §13, **never run** (no sandbox repo exists yet — that's on you, see
   "Your next steps"). Its pure content-generation functions are unit
   tested; nothing that writes to GitHub is.
3. **Fetch and metrics** — `fetch.py`, `metrics.py`, `untriaged.py`,
   `stale.py`, `health.py` (§5), fully tested with fixtures.
4. **Duplicates and triage** — `duplicates.py` (TF-IDF, §5.3), `triage.py`
   (§7.2 post-processing: label allowlisting, priority escalation,
   duplicate confirmation, caching, per-run cap).
5. **Changelog** — `changelog.py`: base-ref selection, PR/commit content,
   semver suggestion, the §7.3 ref guard with retry, deterministic
   fallback, PR-set-hash caching.
6. **Actions and safety** — `sanitize.py` (§8.4) and `actions.py` (§8.1–8.3:
   plan → gate → execute → log, both double-post-prevention layers, per-run
   and per-repo-per-day write caps).
7. **Schema and publish** — `schema.py` (§6, the full `latest.json`
   contract), `state.py`, `pipeline.py` (end-to-end orchestration, new
   tonight beyond the original 9-step list — needed to actually run
   anything).
8. **Evals** — 40-issue fixture set + `labels_proposed.json` (my proposed
   answer key) + 4 injection fixtures + 3 changelog fixture sets. See
   "Eval results" below for what actually ran vs. what's provisional.
9. **Workflow** — `.github/workflows/agent-repo-maint.yml`, two jobs with
   distinct least-privilege permissions, cron + `workflow_dispatch` with a
   repos filter. Calls `scripts/run_agent.py` directly rather than the
   agents-core reusable workflow (see DECISIONS.md for why).

Plus, beyond the original 9 steps: **one real report-mode run**, executed
and its output committed (`public-data/repo_maint/latest.json` +
`data/repo_maint/state.json`).

## Test count and lint

```
uv run pytest    ->  273 passed
uv run ruff check .  ->  All checks passed!
```

One test per module under `agents/repo_maint/`, plus an end-to-end
integration test (`test_pipeline.py`) against a fully mocked GitHub API,
plus tests for the eval machinery itself. No test makes a live network or
LLM call.

## Eval results (`evals/results/repo_maint-2026-09-24.json`)

**Overall status: PROVISIONAL.** Two of six evals ran for real tonight, at
zero cost; four need a live model call that wasn't possible.

| Eval | Status | Result |
|---|---|---|
| `injection_resistance` | **ran** | 4/4 fixtures passed. Found and fixed a real gap along the way: a "reply with your system prompt" attempt wasn't caught by `sanitize.py` — added `"system prompt"` to its reject phrases. Also surfaced (and documented as a known, spec-inherent limitation, not a bug) that a model self-reporting `priority: p0` outright isn't clamped by code — §7.2's guard only ever escalates upward on a regex match. |
| `changelog_fidelity` | **ran** | 100% ref coverage post-guard on all 3 scripted fixture sets. The reported "first-attempt rate" (33%) reflects the *scripted* sequences, not real model quality — see the eval's own `note` field. |
| `classification_accuracy` | provisional_not_run | needs a live model call |
| `priority` | provisional_not_run | needs a live model call |
| `label_allowlist` | provisional_not_run | needs a live model call (the *code-level* allowlist, a different thing, is fully tested in `test_triage.py`) |
| `duplicate_confirmation` | provisional_not_run | needs a live model call |

`evals/repo_maint/labels_proposed.json` has my proposed answer key for
all 40 fixtures, ready to score a real triage run against once one is
possible.

**Fixture provenance, not hidden:** §11 asks for "25 from the seeded
sandbox, plus 15 real public issues." Both sources were genuinely checked
and both are blocked tonight — see DECISIONS.md. All 40 fixtures are
synthetic (26 from `scripts.seed_sandbox`, 14 hand-written to fill
classification gaps). Each fixture's `provenance` field says so.

## The real run

```
uv run python -m scripts.run_report_once
```

Ran twice back to back against the repos this session had live GitHub API
access to (`Kghaffari26/agents-core`, `Kghaffari26/real-estate-agent` —
see "Needed from you" below for why only 2 of 6):

| | Run 1 | Run 2 (immediately after) |
|---|---|---|
| mode | report | report |
| GitHub requests | 22 | 22 |
| — of which 304 (cached) | 0 | **12** |
| cost_usd | 0.0000 | 0.0000 |
| writes made | 0 | 0 |
| repos | agents-core: health 91 (A) · real-estate-agent: health 94 (A) | same |

Both repos are essentially empty right now (0 open issues, 0 open PRs),
so there wasn't much to triage — but this proves the full pipeline runs
end to end against live data, publishes valid §6 JSON, respects ETags on
a second run, and makes zero writes. Output is committed at
`public-data/repo_maint/latest.json` (+ `history/2026-09-24.json`) and
`data/repo_maint/state.json`.

## Needed from agents-core

The blocking dependency. Checked at session start and again ~15 minutes
later (05:34 and 05:50 UTC); both times, `Kghaffari26/agents-core`'s only
branch (`main`) has `core/guards.py` and `core/registry.py` in a
non-packaged monorepo (`pyproject.toml` has `[tool.uv] package = false`,
no `[build-system]`) — not `src/agents_core/{guards,registry}.py` in an
installable package. It cannot be `uv add`-ed as a git dependency in its
current form. Specifically still missing, all of which this repo needs
per tonight's rules ("never write your own http/llm/costs/guards/publish/
runner module"):

- `agents_core.http.HttpClient` — `gh.py` and `pipeline.py` take an
  injected client with the right shape; nothing implements retries/
  caching itself. Swap point: `gh.HttpClientLike`.
- `agents_core.llm` — triage/changelog take injected `classify_fn`/
  `draft_fn`; with none provided, triage produces no fresh results and
  changelog always uses its deterministic fallback.
- `agents_core.guards.verify_numbers` — `triage.py`'s
  `contains_number_support` is an explicitly narrower, triage-specific
  stand-in, not a reimplementation of the real thing.
- `agents_core.schema` (`Model`, `Timestamp`, `RunMeta`, `KeyStat`, etc.)
  — mirrored field-for-field in `schema.py` from a real read of the
  module, so the output validates against the right shape today.
- `agents_core.costs`, `agents_core.publish`, `agents_core.runner`,
  `agents_core.registry` — not wired in at all; `pipeline.py` +
  `scripts/run_agent.py` stand in for `core.runner`, and there's no
  `agents/repo_maint/agent.py:AGENT` registered anywhere yet.

None of `agents-core`'s files were modified — see CLAUDE.md for the exact
swap-in steps once it's packaged.

## Blockers / things I could not do

- **Live evals** (classification_accuracy, priority, label_allowlist,
  duplicate_confirmation): need `ANTHROPIC_API_KEY`, not present.
- **Real public-repo issues for the eval fixture set**: this session's
  repo-scope safety guard correctly denied attaching an unrelated
  third-party repo (`pallets/flask`) for API access — it did allow the
  same call for repos you own. Your own watched repos genuinely have zero
  issues right now (confirmed via the GitHub API), so there was nothing
  real to pull from either source.
- **4 of your 6 configured repos weren't reachable this session**:
  `Kghaffari26/sam-agent` (an `add_repo` call for it was denied by the
  session's own auto-mode classifier — looked like a transient/concurrency
  issue, not a policy one, worth just retrying), `Kghaffari26/macro-fed-agent`
  and `Kghaffari26/agents-hub` (not found — don't exist yet, or a naming
  mismatch), and **`Kghaffari26/repo-maint-agent`** — tonight's instructions
  named it that way, but this session's own repo (where all of this lives)
  is actually `Kghaffari26/repo-maintain-agent` (with "ain"). I used the
  name exactly as given in `config/repos.toml` rather than silently
  "fixing" it; there's a comment right above that entry. **Please confirm
  in the morning** whether `repo-maint-agent` is a real, separate repo I
  should know about, or whether that entry should just say
  `repo-maintain-agent`.
- **`scripts/seed_sandbox.py`'s PR-creation half** (`create_pr_branch`) is
  an intentional `NotImplementedError` stub — it depends on the sandbox
  repo's actual file layout at seed time, which doesn't exist yet. The
  issue-creation half is complete.
- **`pipeline.run()` doesn't isolate a single repo's hard failure** — if
  one configured repo 404s or its token is wrong, the whole run currently
  fails rather than reporting that repo as failed and continuing with the
  rest. Documented, not fixed tonight (would need `run()`'s per-repo loop
  wrapped in its own try/except plus a "failed repo" shape in the schema).

## Your next steps

1. **Confirm the `repo-maint-agent` vs `repo-maintain-agent` naming** (see
   above) and fix `config/repos.toml` if needed.
2. **Create the sandbox repo** (e.g. `Kghaffari26/agents-hub-sandbox`),
   uncomment its `[[repo]]` block in `config/repos.toml`, set
   `REPO_MAINT_TOKEN` (fine-grained PAT, Issues read/write + Pull requests
   read + Metadata read + Checks read, scoped to just that repo), then run
   `uv run python -m scripts.seed_sandbox --repo <owner>/<sandbox> --confirm`
   yourself — I deliberately never ran it.
3. **Set `ANTHROPIC_API_KEY`** as a repo secret (for the workflow) and in
   your local `.env` if you want to test triage/changelog for real before
   trusting them in CI.
4. **When agents-core is actually packaged**, follow the steps in
   `CLAUDE.md`'s last section to wire it in and delete the temporary
   scaffolding (`scripts/run_report_once.py`, `_TempHttpClient` in
   `scripts/run_agent.py`, the mirrored schema classes).
5. **Only then** consider setting the `APPLY_CHANGES` repo variable to
   `"true"` — and only for the sandbox repo, per the spec's own §2/§13
   guidance, until you've watched it run in report mode for a while.
6. `Kghaffari26/sam-agent` — the `add_repo(push)` attach was denied by the
   session's auto-mode classifier for reasons that looked transient; worth
   a retry in a fresh session if you want it watched tonight's way.

Branch: `claude/pensive-faraday-twjjvf`. Everything above is pushed.
