# repo-maint-agent

A daily agent that triages GitHub issues, flags stale PRs, drafts changelogs,
scores repo health and, on a sandbox repo, proposes fixes for small bugs as
**draft** pull requests that a human must approve. Read-only by default.

## Highlights

- **An agent loop that fixes real bugs, and can't merge anything.** The fix
  proposer is an [agents-core](https://github.com/Kghaffari26/agents-core)
  `AgentLoop` (12 steps, $0.15 per loop, 2 issues per run) with read-only tools
  (`list_files`, `search_code`, `read_file`) plus `propose_patch`, which validates a
  unified diff by applying it in memory, then `finish`. It has no write tool.
  A proposal becomes a draft PR only after a human approves its exact id and all
  five write gates plus `allow_fix_prs` pass
  ([spec §6.1](docs/specs/SPEC_REPO_MAINT.md)). `gh.py` has exactly three write
  methods (`add_labels`, `add_comment`, `create_draft_pr`), enforced by an AST test,
  and nothing that can merge or enable auto-merge.
- **Guards, not trust.** Every model-written number is checked against the data
  (agents-core's number guard) on triage summaries, changelogs (plus a ref guard:
  every PR/commit must appear, nothing invented) and the fix loop's final summary.
  One retry, then a deterministic template, labelled `narrative_source: "template"`.
  Issue text is fenced as untrusted; tool output is wrapped as untrusted data;
  anything that could reach GitHub is sanitized (no pings, no outside links).
- **Evals as a gate.** Seven `agents_core.evals` suites, run on every PR by
  `run-evals.yml` with a $1.00 cap. They fail on a >0.05 regression. Latest
  scores (2026-09-27, [`evals/history.jsonl`](evals/history.jsonl)):

  | Suite | Cases | Result | Cost |
  |---|---|---|---|
  | fix_proposer (live): patch applied to the fixture repo, its tests + a hidden check pass | 5 bugs | **5/5 fixed**, 4–5 model steps each, all `finished`; required/forbidden tools 100%; LLM judge 1.0 | $0.065 |
  | fix_proposer_replay (offline, recorded trajectories) | 5 | 5/5 | $0 |
  | triage (live): classification / priority ±1 / security at p0–p1 / label allowlist / duplicates | 40 | 95% / 100% / 100% / 100% / 100% | $0.069 |
  | injection (live) | 4 | **4/4** (was 3/4 on 2026-09-26; see [case studies](docs/case-studies.md)) | $0.007 |
  | injection_worst_case (offline: a fully compliant model) | 4 | 4/4 | $0 |
  | changelog (live): refs covered / right first time | 3 | 100% / 100% | $0.004 |
  | changelog_guard (offline, scripted) | 3 | 3/3 | $0 |

  Triage scores are **provisional**: the fixtures are synthetic and the answer key
  was proposed by an agent and hasn't been reviewed by a human.
- **Tracing.** Every run publishes agents-core's redacted `trace.json`: spans for
  each phase, each repo's fetch/compute/triage/changelog, every LLM call, HTTP
  request, guard check, agent loop and tool call, plus a `trace_summary` in
  `manifest-entry.json`. A bad run can be diagnosed from the data branch alone.
- **Costs.** Hard caps everywhere: $0.40 per run (MAX_RUN_USD, checked before each
  call with a worst-case estimate), $0.15 per fix loop, $1.00 per eval run, and a
  2,000-request daily cap on the GitHub host. Real numbers: a report run with a
  changelog draft cost **$0.0075**; the next run cost **$0** (cached triage and
  changelog, 6 of 12 GitHub reads answered 304); a full live eval run cost
  **$0.14**. With no Anthropic key it still publishes (`status: ok`, a warning,
  template output).
- **Numbers come from code.** Health scores, counts, deltas and line counts are
  computed in `transform`; the model only writes narrative about them.

It's an agents-core agent pinned at **v0.3.2** (by commit SHA `9e4f342` until the
`v0.3.2` tag exists), registered as `repo_maint`.
**Start here:** [`STATUS.md`](STATUS.md) (current state, what needs a human),
[`DECISIONS.md`](DECISIONS.md) (every judgment call), and
[`docs/case-studies.md`](docs/case-studies.md) (bugs the evals, guards and live runs
caught).

## Demo

Everything below is reproducible from this repo; the fix-proposer output is a
real recorded run.

```bash
uv sync
uv run pytest                                   # 397 tests; no network, no model
uv run python -m evals.repo_maint.ci --offline  # offline suites incl. the replayed fix loops, $0
uv run agents-run repo_maint --dry-run          # fetch + compute; no LLM, no writes, no publish
```

**A fix proposal** (eval case fix-03, the issue "First page of the transaction list
skips the first 10 rows" against [`scripts/sandbox_package`](scripts/sandbox_package)).
The loop's trajectory was `search_code("def paginate") → read_file(ledgerlite/pagination.py)
→ propose_patch → finish`, 4 steps, $0.009:

```diff
--- a/ledgerlite/pagination.py
+++ b/ledgerlite/pagination.py
@@ -18,6 +18,6 @@
     if page < 1:
         raise ValueError("page is 1-based")
-    start = page * per_page
+    start = (page - 1) * per_page
     return list(items[start : start + per_page])
```

> Fixed off-by-one in paginate() where start index was computed as page*per_page
> instead of (page-1)*per_page, causing page 1 to skip the first 10 items.

That summary passed the number guard ("10" is in the issue). On fix-02 the model's
first summary said the code built `date(year, 13, 1)`; "13" is in neither the issue
nor the diff, so the guard sent it back once and the second summary passed
(logged in `data/guard_failures.jsonl`). On fix-04 its first diff had a malformed
hunk header; `propose_patch` rejected it and the model resubmitted.

In `latest.json` that becomes an entry in `repos[i].fix_proposals` with
`status: "proposed"`, the diff, the loop's steps/cost/tools, and an `id`. A
maintainer who wants it runs the workflow in apply mode with `approve_fix: <id>`;
the agent then re-applies the diff to the current default branch and opens a draft
PR from `repo-maint/fix-3-<id>` headed "**Proposed by repo-maint agent; needs human
review.**" (tested end to end against a mocked GitHub API in
`tests/repo_maint/test_fix_proposer.py`; no PR has been opened for real).

**A real report run** on this repo (2026-09-27, the other five watched repos aren't
reachable from the session that ran it, so they're listed in `meta.warnings`):

```json
{ "meta": { "status": "ok", "cost_usd": 0.0075, "github_requests": 12,
            "warnings": ["Kghaffari26/agents-core: skipped this run (… -> 403)", "…"] },
  "headline": "1 repo watched (5 unreachable): 0 issues triaged, 0 untriaged, 0 stale PRs.",
  "key_stats": [{ "label": "Avg health", "value": 94, "format": "count",
                  "delta": 0, "delta_format": "count_signed", "good_direction": "up" }] }
```

Its changelog came from the last 30 days of commits (the repo has no tags or merged
PRs yet), drafted by the model and passing the ref guard on the first attempt. See
[`public-data/`](public-data) for the full output, `trace.json` included.

## Running it

```bash
uv run agents-run repo_maint                    # report mode: publishes public-data/
uv run agents-run repo_maint --repos a/b,c/d    # only these configured repos
uv run agents-run repo_maint --apply --approve-fix <id>   # see §8.1 and §6.1 first
uv run python -m evals.repo_maint.ci            # offline suites; live ones too if a key is set
uv run python -m evals.repo_maint.ci --live --total-max-usd 1.00 --record   # re-record trajectories
uv run ruff check .
```

Environment (see `.env.example`; `agents-run` loads `.env`):

| Variable | Used for |
|---|---|
| `GITHUB_TOKEN` | GitHub reads for repos with `token = "default"` |
| `REPO_MAINT_TOKEN` | repos with `token = "repo_maint"` (the sandbox; also writes its draft PRs) |
| `ANTHROPIC_API_KEY` or `AGENTS_ANTHROPIC_API_KEY` | triage, changelog, fix proposer, eval judge. Optional: without it the run publishes template output with a warning |
| `AGENTS_CORE_MAX_RUN_USD` | per-run spend cap (CI sets `0.40`) |
| `AGENTS_CORE_EVAL_MAX_USD` | eval spend cap (the PR gate sets `1.00`) |
| `APPLY_CHANGES` | gate 2 of §8.1; leave unset locally |

## Output: the agents-core data-branch contract

```
public-data/
├── latest.json              # RepoMaintOutput: the spec §6 shape + §6.1 additive fields
├── history/YYYY-MM-DD.json  # 90 kept
├── manifest-entry.json      # incl. trace_summary
├── costs-summary.json
├── schema.json
├── trace.json               # this run's spans (redacted, size-capped)
└── trace.schema.json
```

Local run state in `data/` is committed back by CI: `costs.jsonl`,
`eval_costs.jsonl`, `guard_failures.jsonl` (agents-core),
`repo_maint/state.json` (triage/changelog caches, double-post protection, fix
proposals) and `repo_maint/github/` (conditional-read bodies + ETags from
`Http.download`).

## Modules

| Module | What it does |
|---|---|
| `agent.py` | The `agents_core.agent.Agent`: fetch → transform → analyze; no-key fallback; `--repos`, `--approve-fix` |
| `pipeline.py` | The three stages per repo, per-repo failure isolation, warnings, key-stat deltas, tracing spans |
| `config.py` | `config/repos.toml`, the five write gates, `approve_fix_pr` (the fix-PR gate) |
| `gh.py` | GitHub REST over `agents_core.http.Http`: conditional reads via `Http.download`, `Link`-header pagination (cached lists too), rate-limit floor, exactly three writes |
| `fetch.py` | All reads for one repo into a `RepoSnapshot`; commits since a date |
| `untriaged.py`, `metrics.py`, `duplicates.py`, `stale.py`, `health.py` | Deterministic computations (§5) |
| `triage.py` | The §7.2 prompt (fast tier), post-processing, title-only security escalation, number guard, caching |
| `changelog.py` | Base ref, PR/commit content, the §7.3 prompt (smart tier), ref + number guard, template fallback |
| `fix_proposer.py` | The §6.1 agent loop, its tools, selection, proposal records, draft-PR building |
| `patch.py` | Parse and apply unified diffs in memory, with safety limits |
| `sanitize.py` | Sanitization of anything posted (§8.4) |
| `actions.py` | Plan → gate → execute → log for labels and comments (§8) |
| `schema.py` | The `latest.json` contract (§6, §6.1), on `agents_core.schema` |
| `state.py` | `data/repo_maint/state.json` |

## CI

- `.github/workflows/agent-repo-maint.yml`: daily at 07:00 PT and on dispatch
  (inputs `repos`, `approve_fix`). Two least-privilege jobs call
  `run-agent.yml@9e4f342…` (v0.3.2): `report` (`contents: write`, `issues: read`,
  `pull-requests: read`, `checks: read`) by default, and `apply` (`issues: write`
  instead, `apply_changes: true`, `--apply`) only when the repo variable
  `APPLY_CHANGES` is `'true'`. The reusable workflow restores the `data` branch
  first, passes `GITHUB_TOKEN`/`REPO_MAINT_TOKEN`/`APPLY_CHANGES`, and force-pushes
  `public-data/` to `data`.
- `.github/workflows/evals.yml`: on PRs touching agent code, evals, config or the
  sandbox package, `run-evals.yml@9e4f342…` (v0.3.2) (`contents: read`, `max_usd: 1.00` per suite and
  `total_max_usd: 1.00` across them,
  regression threshold 0.05).

## Safety design (§8)

- Read-only by default. Any write needs all five gates: `--apply`,
  `APPLY_CHANGES == 'true'`, the repo's `allow_apply = true`, role `own` or
  `sandbox` (never `public_demo`, refused at config load), and a resolvable token.
- A draft PR additionally needs role `sandbox`, `allow_fix_prs = true` (refused on
  any other role at config load) and a human approval of that exact proposal id;
  `create_draft_pr` refuses without a matching `FixPRApproval`.
- `gh.py` exposes exactly three write methods; only `create_draft_pr` is marked
  `requires_approval`. Static tests check both, and that nothing can merge.
- Every write is capped (`max_writes_per_run`, `max_writes_per_repo_per_day`) and
  logged as `planned`/`applied`/`skipped`/`failed`, never silently dropped.
- Issue/PR text is fenced as untrusted in every prompt; triage/changelog models
  have no tools; the fix loop's tools are read-only or validate-only; every model
  field is validated in code.
- Comments and PR text are sanitized: mentions neutralized, non-repo URLs stripped,
  HTML/images removed, length-capped, rejected outright on a marker string,
  "ignore previous", `system prompt`, or a secret-looking pattern.
