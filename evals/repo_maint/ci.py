"""Run the repo_maint eval suites under one total spend cap (the PR gate's command).

    uv run python -m evals.repo_maint.ci                 # offline suites; live ones too if a key is set
    uv run python -m evals.repo_maint.ci --offline       # offline suites only ($0)
    uv run python -m evals.repo_maint.ci --live --max-usd 1.00 --record

Offline suites always run (no model, $0). Live suites run when an Anthropic key is
configured (``ANTHROPIC_API_KEY`` or ``AGENTS_ANTHROPIC_API_KEY``) or with ``--live``,
sharing ``--max-usd`` (default ``AGENTS_CORE_EVAL_MAX_USD``, else $1.00): each suite
gets what the earlier ones left, and agents-core stops a suite before a call that
could pass its cap. ``--record`` saves the live fix-proposer trajectories for the
offline replay suite. Every suite appends a line to ``evals/history.jsonl``;
``agents-evals compare`` (run by run-evals.yml) then fails the PR on a regression.
"""  # noqa: E501

from __future__ import annotations

import argparse
import os
import sys

from agents_core import settings
from agents_core.evals import EvalReport, run_suite

from evals.repo_maint import suites

MIN_SUITE_USD = 0.02  # don't start a live suite with less than this left


def has_key() -> bool:
    try:
        settings.anthropic_api_key()
    except RuntimeError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.repo_maint.ci", description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="offline suites only")
    mode.add_argument("--live", action="store_true", help="require the live suites")
    parser.add_argument("--max-usd", type=float, default=None, help="total cap for all suites")
    parser.add_argument("--only", default="", help="comma-separated suite names to run")
    parser.add_argument("--record", action="store_true", help="save fix-proposer trajectories")
    parser.add_argument("--no-write", action="store_true", help="don't write results/history")
    args = parser.parse_args(argv)
    settings.load_dotenv()

    run_live = not args.offline and (args.live or has_key())
    if args.live and not has_key():
        print("--live needs ANTHROPIC_API_KEY (or AGENTS_ANTHROPIC_API_KEY)", file=sys.stderr)
        return 2
    if args.record:
        os.environ[suites.RECORD_ENV] = "1"
    total_cap = args.max_usd if args.max_usd is not None else settings.eval_max_usd()
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}

    plan = list(suites.OFFLINE_SUITES) + (list(suites.LIVE_SUITES) if run_live else [])
    if wanted:
        plan = [s for s in plan if s.name in wanted or s.name.removeprefix("repo_maint-") in wanted]
    spent = 0.0
    reports: list[EvalReport] = []
    for suite in plan:
        if not suite.cases:
            print(f"{suite.name}: no cases (skipped)")
            continue
        live = suite in suites.LIVE_SUITES
        remaining = round(total_cap - spent, 6)
        if live and remaining < MIN_SUITE_USD:
            print(f"{suite.name}: skipped, ${remaining:.4f} of the ${total_cap:.2f} cap left")
            continue
        # Offline suites spend nothing (replays zero their usage) but still need a cap > 0.
        cap = remaining if live else max(total_cap, MIN_SUITE_USD)
        report = run_suite(suite, max_usd=cap, write=not args.no_write)
        spent += report.usd
        reports.append(report)
        scores = " ".join(f"{k}={v:.3f}" for k, v in report.scores.items())
        print(
            f"{report.suite}: pass_rate={report.pass_rate:.3f} {scores} usd={report.usd:.4f}"
            + (" (spend cap reached)" if report.budget_exhausted else "")
        )
    kind = "live + offline" if run_live else "offline"
    print(f"total: ${spent:.4f} of ${total_cap:.2f} ({kind})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
