"""CLI: `python -m netracare.evals {list,run,update-baseline} ...`

Exit codes: 0 ok, 1 regression against the baseline (with --fail-under), 2 usage/data error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netracare.config import get_settings
from netracare.evals.gate import baseline_path, compare, load_baseline, write_baseline
from netracare.evals.runner import run_suite
from netracare.evals.schema import DatasetError
from netracare.evals.tasks import SUITES


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m netracare.evals")
    p.add_argument("--root", type=Path, default=Path(), help="repository root")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list eval suites")
    run = sub.add_parser("run", help="run a suite (or 'all')")
    run.add_argument("suite")
    run.add_argument("--baseline", type=Path, default=None)
    run.add_argument("--fail-under", action="store_true", help="exit 1 on any regression")
    run.add_argument("--tolerance", type=float, default=0.0)
    run.add_argument("--upload", action="store_true", help="log an experiment to Opik")
    run.add_argument("--no-report", action="store_true")
    run.add_argument(
        "--live", action="store_true", help="also run suites that call a real LLM (Groq)"
    )
    upd = sub.add_parser("update-baseline", help="re-run a suite and overwrite its baseline")
    upd.add_argument("suite")
    upd.add_argument("--live", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    root: Path = args.root

    if args.cmd == "list":
        for name, suite in SUITES.items():
            state = "live" if suite.requires_live else "scored" if suite.task else "schema-only"
            print(f"{name:24} {state:12} {suite.description}")
        return 0

    live = bool(getattr(args, "live", False))
    if args.suite == "all":
        names = [n for n, s in SUITES.items() if live or not s.requires_live]
    else:
        names = [args.suite]
    worst = 0
    for name in names:
        selected = SUITES.get(name)
        if selected is not None and selected.requires_live and not live:
            print(f"{name}: skipped (calls a real LLM; re-run with --live)")
            continue
        try:
            report = run_suite(
                name,
                settings,
                root=root,
                upload=getattr(args, "upload", False),
                write_report=not getattr(args, "no_report", False),
            )
        except (KeyError, DatasetError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not report.scored:
            print(f"{name}: {report.n_cases} cases, schema OK (not scored yet)")
            continue
        metrics = ", ".join(f"{k}={v:.4f}" for k, v in report.metrics.items())
        print(f"{name}: {report.n_cases} cases | {metrics} | failures={len(report.failures)}")
        for case_id in report.failures[:20]:
            print(f"  FAIL {case_id}")
        if report.opik_experiment:
            print(f"  opik experiment: {report.opik_experiment}")

        if args.cmd == "update-baseline":
            path = baseline_path(name, root)
            write_baseline(report, path)
            print(f"  baseline written: {path}")
            continue
        base_file = args.baseline or baseline_path(name, root)
        if not base_file.exists():
            print(f"  no baseline at {base_file}")
            continue
        regressions = compare(report, load_baseline(base_file), args.tolerance)
        for r in regressions:
            print(f"  REGRESSION {r}")
        if regressions and args.fail_under:
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
