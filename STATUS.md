# Status — session 2026-09-27 (agents-core v0.3.1, fix proposer, evals, tracing)

Read this first. `DECISIONS.md` has the reasoning behind every judgment call
referenced here (this session's are under "Session 2026-09-27" and "(2): agents-core v0.3.1").

## Upgrade to agents-core v0.3.1 (latest)

- Pin and all three workflow `uses:` refs are `@v0.3.1`.
- **Temperature is back** on the fast tier (`config/models.toml`, 0 for triage and
  the judge); v0.3.1 sends it in `extra_body`. Live smoke check: one `complete` and
  one `structured` call at temperature 0, both fine, **$0.0003**.
- The fix-proposer `LLMJudge` gets `max_tokens=512` (was the tier's 4096), fixing the
  inflated pre-call estimate.
- `evals/repo_maint/ci.py` uses `run_suites(total_max_usd=)` instead of its own
  cap-splitting loop: `--total-max-usd` (default $1.00) and `--max-usd` per suite;
  `evals.yml` passes `total_max_usd: "1.00"`.
- **398 tests passing, ruff clean**, offline evals all 1.0 ($0). No live evals, zero
  GitHub API writes.
- Needed from agents-core: item 1 (temperature) and item 3 (`LLMJudge` controls) below
  are resolved by v0.3.1; item 2 (`DownloadResult.headers`) is still deferred.

## TL;DR (earlier today: the v0.3.0 session)

- **On agents-core v0.3.0**, with every v0.1.0 workaround removed. The workflow
  calls `run-agent.yml@v0.3.0` from two least-privilege jobs, and **CI can now do real
  work**: the reusable workflow passes `GITHUB_TOKEN`, `REPO_MAINT_TOKEN` and
  `APPLY_CHANGES` and restores the `data` branch.
- **New: the fix proposer** (spec §6.1). An `AgentLoop` proposes patches for small
  bugs on the sandbox, and a draft PR opens only after a human approves the exact
  proposal and all gates pass. Live eval: **5/5 seeded bugs fixed**.
- **Evals ported to `agents_core.evals`** with a history file and a PR gate
  (`run-evals.yml@v0.3.0`, $1.00 cap). Live injection is now **4/4** (was 3/4).
- **Tracing**: `trace.json` is published every run, with per-repo spans.
- **397 tests passing, ruff clean, actionlint clean.**
- **Anthropic spend this session: $0.172** ($0.164 evals + $0.0075 one real run),
  under the $1.50 cap.
- **Zero GitHub writes.** The seed script was not run. No `--apply`. Both real runs
  were GET-only, which trace.json's HTTP spans confirm.

## Done this session

**Migration (agents-core v0.1.0 → v0.3.0)**
- Conditional GitHub reads use `Http.download` (bodies + ETags under
  `data/repo_maint/github/`), replacing the ETag-bodies-in-state.json mechanism.
- §6 meta extensions go through a `RunMeta` subclass + `AgentResult.meta_fields`
  (the before-validator hack is gone). Non-fatal problems go to `meta.warnings`.
- `narrative_source` is `"llm" | "template"`. `schema_version` is `1.1.0`.
- **No Anthropic key → `status: ok` + a warning**: triage is left unscored, the
  changelog uses the template, and the fix proposer doesn't run. It no longer crashes
  (agents-hub's report).
- Every `key_stats[].delta_format` is `count_signed`. `delta` is computed against the
  previous run, and only when the same repos were watched.
- The GitHub host gets an agents-core `HostPolicy` daily cap (2,000 requests, 3
  attempts).
- Workflow: `report` job (`contents: write`, `issues/pull-requests/checks: read`)
  and `apply` job (`issues: write`, `apply_changes: true`, `--apply`), both on
  `run-agent.yml@v0.3.0`. The input-validation job is gone (v0.2.0 no longer
  shell-evaluates `extra_args`). New dispatch input `approve_fix`.

**Agreed decisions applied**
1. `narrative_source` publishes `"template"` instead of `"deterministic"`.
2. Security escalation: title only, and only when the model said `bug`. The inj-01
   live output is a regression test. Live injection: **4/4**.
3. No tags and no merged PRs → changelog from the default branch's commits in the
   last 30 days. Confirmed live: this repo's real changelog came from that path.
4. Token passthrough plus the two-job least-privilege layout (above).

**New features**
- (A) Tracing: `custom` spans per repo (fetch, compute, triage, changelog, repo,
  fix loop) on top of agents-core's automatic phase/LLM/HTTP/guard/loop/tool spans.
- (B) Evals: `evals/repo_maint/suites.py` (7 suites) and `evals/repo_maint/ci.py`
  (one total cap), with `evals/history.jsonl` and `.github/workflows/evals.yml`.
- (C) Fix proposer: `fix_proposer.py`, `patch.py`, `gh.create_draft_pr`
  (`requires_approval`), the `allow_fix_prs` flag, `--approve-fix`,
  `repos[i].fix_proposals` (spec §6.1), and the sandbox package with 5 bugs plus
  matching issues in `scripts/seed_sandbox.py` (not run).
- (D) `docs/case-studies.md`: 5 incidents. (E) README Highlights + Demo.

## Eval results (2026-09-27, `evals/history.jsonl`, `evals/results/2026-09-27.json`)

| Suite | Result | Cost |
|---|---|---|
| fix_proposer (live, 5 bugs) | 5/5 fixed (patch applies; package tests + hidden check pass); required/forbidden tools, max steps, stop reason all 1.0; LLM judge 1.0 | $0.065 |
| fix_proposer_replay (offline) | 5/5 | $0 |
| triage (live, 40) | classification 0.95 (misses fx-026 bug→other, fx-031 chore→bug, same as 2026-09-26); priority ±1 1.0; security p0/p1 1.0; allowlist 1.0; duplicates 1.0 | $0.069 |
| injection (live, 4) | 4/4 (first run that day 3/4: the model rated inj-04 p0; see DECISIONS.md and case study 1) | $0.007 ×2 |
| injection_worst_case (offline) | 4/4 | $0 |
| changelog (live, 3) | ref coverage 1.0, first attempt 1.0 | $0.004 |
| changelog_guard (offline) | 3/3 | $0 |

Triage stays **PROVISIONAL**: the fixtures are synthetic and `labels_proposed.json`
hasn't been human-reviewed. The LLM judge hasn't been calibrated against human
labels (`LLMJudge.calibrate`).

## The real runs

`uv run agents-run repo_maint`, report mode, twice:

| | Run 1 (2026-09-27T00-42-17Z-da3e80) | Run 2 (2026-09-27T00-44-22Z-3341c6) |
|---|---|---|
| cost | $0.0075 (one changelog draft) | $0.0000 |
| GitHub requests / 304s | 12 / 0 | 12 / 6 |
| non-GET requests | 0 | 0 |
| status / warnings | ok / 5 unreachable repos | ok / same |
| data_changed | true | false |

**Only 1 of 6 watched repos was reachable** from this session. Its GitHub access
covered only this repo, so agents-core, real-estate-agent, fed-agent, sam-agent and
agents-hub returned 403. They're listed in `meta.warnings` and the headline, which
is exactly what that path is for. In CI (or with a token that can read them) all six
are fetched. This repo: health 94 (A); −6 for a missing LICENSE and CONTRIBUTING.
The failing check run on `main` from last session no longer shows.

## Needed from agents-core (not modified from here)

1. **`temperature` is incompatible with its own SDK pin.** agents-core v0.2.0+
   sends a tier's `temperature` (models.toml) or a per-call `temperature=` to
   `messages.create`/`parse`, but anthropic 1.8 (which it requires) accepts neither.
   Any agent that sets it breaks. Either drop the feature or gate it per model. This
   repo sets no temperature (see case study 2).
2. **`Http.download` returns no response headers**, so an agent can't see rate-limit
   headers or `Link` pagination on conditional reads. Exposing `headers` on
   `DownloadResult` would let this repo drop its page-number pagination for cached
   lists.
3. (Minor) `LLMJudge` has no `max_tokens`/temperature control and uses the tier
   default of 4096 output tokens, which inflates its pre-call worst-case estimate.

## Things you need to do by hand

1. **Push/tag:** nothing needed for agents-core (v0.3.0 exists). This session's
   commits are on `main` and `claude/eager-euler-4udsmj`.
2. **Secrets:** `ANTHROPIC_API_KEY` (repo secret; both workflows), plus
   `SITE_DISPATCH_TOKEN` if agents-hub should be notified.
3. **The sandbox**, only when you want the fix proposer live:
   create `Kghaffari26/agents-hub-sandbox` (empty, initialized with a README), create
   a fine-grained PAT scoped to it only (Contents, Issues and Pull requests
   read/write, Metadata and Checks read) as `REPO_MAINT_TOKEN`, uncomment its entry in
   `config/repos.toml`, then **run `uv run python -m scripts.seed_sandbox --repo
   Kghaffari26/agents-hub-sandbox --confirm` yourself**. Watch a few report-mode runs
   first: proposals appear in `latest.json` under `fix_proposals`. To open one as a
   draft PR, set `APPLY_CHANGES=true` and dispatch the workflow with
   `approve_fix: <id>`.
4. **Review `evals/repo_maint/labels_proposed.json`** (still agent-proposed), and
   consider calibrating the fix judge on a few human-scored proposals.
5. **A stale directory outside the repo**: `/nonexistent-http-cache/` in the cloud
   container held the old tests' shared request counter. It's unused now, and its
   removal was blocked by a safety check. Ephemeral container, so it's harmless;
   remove it by hand if you're on a persistent machine that ran the old tests.
6. For local runs, use a token that can read all six watched repos. In CI the
   workflow token reads this repo, plus the others only if they're public; private
   ones need `token = "repo_maint"` and a `REPO_MAINT_TOKEN` that can read them.

## Test count and lint

```
uv run pytest                                   ->  397 passed
uv run ruff check .                             ->  All checks passed!
actionlint .github/workflows/*.yml              ->  clean
uv run python -m evals.repo_maint.ci --offline  ->  3 offline suites, all 1.0, $0
```
