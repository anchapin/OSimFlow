"""CI helpers for the real-E2E workflows (issue #1813).

Two subcommands:

``preflight``
    Verify that every required configuration value is present *before* any
    expensive setup runs.  Missing values are reported transparently: in
    ``--strict`` mode the command fails; otherwise it records an
    "unavailable, NOT verified" job summary and sets ``available=false`` on
    ``$GITHUB_OUTPUT`` so later steps skip instead of falsely passing.

``junit``
    Verify that a pytest JUnit XML report contains executed tests, i.e. is not
    empty or all-skipped, and publish the counts to the job summary.
"""

import argparse
import logging
import os
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("e2e_gate")


@dataclass(frozen=True)
class JunitCounts:
    tests: int
    failures: int
    errors: int
    skipped: int

    @property
    def executed(self) -> int:
        return self.tests - self.skipped


def missing_names(required: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Return the required names whose value is unset or blank."""
    return [name for name in required if not env.get(name, "").strip()]


def parse_junit(path: Path) -> JunitCounts:
    """Sum testcase counts over every ``<testsuite>`` in a JUnit report."""
    root = ET.parse(path).getroot()  # noqa: S314 - report is produced by our own pytest run
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
    return JunitCounts(**totals)


def judge_junit(counts: JunitCounts) -> str | None:
    """Return an error message when the report cannot prove real execution."""
    if counts.tests == 0:
        return "JUnit report contains zero tests"
    if counts.executed <= 0:
        return f"all {counts.tests} test(s) were skipped; nothing was executed"
    if counts.failures or counts.errors:
        return f"{counts.failures} failure(s) and {counts.errors} error(s) reported"
    return None


def _append(path_env: str, text: str) -> None:
    target = os.environ.get(path_env)
    if target:
        with Path(target).open("a", encoding="utf-8") as fh:
            fh.write(text)


def cmd_preflight(args: argparse.Namespace) -> int:
    missing = missing_names(args.require, os.environ)
    if not missing:
        _append("GITHUB_OUTPUT", "available=true\n")
        log.info("%s: all %d prerequisites configured", args.label, len(args.require))
        return 0
    listing = ", ".join(missing)
    if args.strict:
        _append(
            "GITHUB_STEP_SUMMARY",
            f"### {args.label}: FAILED\nMissing required configuration: `{listing}`\n",
        )
        log.error("%s: missing required configuration: %s", args.label, listing)
        return 1
    _append("GITHUB_OUTPUT", "available=false\n")
    _append(
        "GITHUB_STEP_SUMMARY",
        f"### {args.label}: UNAVAILABLE (not verified)\n"
        f"Missing configuration: `{listing}`. No real tests ran; this is NOT a "
        "successful run.\n",
    )
    log.warning("%s: UNAVAILABLE, missing: %s", args.label, listing)
    print(f"::warning title={args.label} unavailable::missing {listing}")
    return 0


def cmd_junit(args: argparse.Namespace) -> int:
    path = Path(args.report)
    if not path.is_file():
        log.error("JUnit report not found: %s", path)
        return 1
    counts = parse_junit(path)
    _append(
        "GITHUB_STEP_SUMMARY",
        f"### {args.label} JUnit\n"
        f"- github job: `{os.environ.get('GITHUB_JOB', 'n/a')}` "
        f"run `{os.environ.get('GITHUB_RUN_ID', 'n/a')}`\n"
        f"- tests={counts.tests} executed={counts.executed} "
        f"skipped={counts.skipped} failures={counts.failures} errors={counts.errors}\n",
    )
    problem = judge_junit(counts)
    if problem:
        log.error("%s: %s", args.label, problem)
        return 1
    log.info("%s: %d executed test(s) passed", args.label, counts.executed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--label", required=True)
    pre.add_argument("--require", nargs="+", required=True, metavar="ENV_NAME")
    pre.add_argument("--strict", action="store_true")
    pre.set_defaults(func=cmd_preflight)
    jun = sub.add_parser("junit")
    jun.add_argument("--label", required=True)
    jun.add_argument("--report", required=True)
    jun.set_defaults(func=cmd_junit)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
