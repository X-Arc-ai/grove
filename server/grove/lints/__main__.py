"""Command line of the lints: exit 0 clean, 1 findings, 2 the lint could not run."""

import argparse
import sys

from grove.lints import clock_rule, no_names, writer_rule
from grove.lints._scan import LintError, repo_root

LINTS = {
    "no-names": no_names.check,
    "clock": clock_rule.check,
    "writer": writer_rule.check,
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m grove.lints")
    parser.add_argument("lint", choices=sorted(LINTS))
    parser.add_argument("--root", help="repository to walk; default: the one around this folder")
    parser.add_argument("--base", help="no-names: also check each commit in <base>..HEAD")
    args = parser.parse_args()

    try:
        findings = LINTS[args.lint](repo_root(args.root), args.base)
    except LintError as error:
        print(f"grove.lints: {error}", file=sys.stderr)
        return 2

    for finding in sorted(set(findings)):
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
