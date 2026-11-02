"""`python -m grove.screening counts`: check the rename map and the denylist by counts only.

It prints {"rename_forms": n, "labels": n, "denylist_terms": n} and nothing else, so it can check
the real files without any form, label or term reaching a terminal or a log. It writes nothing.
Exit 0 on success, 2 when a file cannot be loaded (the error names the variable or flag only).
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from grove.config import DENYLIST_VAR, RENAME_MAP_VAR, ConfigError, load_screening_settings
from grove.screening.names import load_denylist, load_rename_map


def counts(rename: Path | None, denylist: Path | None) -> dict[str, int]:
    """A flag replaces its variable; a missing flag falls back to the variable."""
    rename_source, denylist_source = "--rename", "--denylist"
    if rename is None or denylist is None:
        settings = load_screening_settings()
        if rename is None:
            rename, rename_source = settings.rename_map, RENAME_MAP_VAR
        if denylist is None:
            denylist, denylist_source = settings.denylist, DENYLIST_VAR
    names = load_rename_map(rename, rename_source)
    terms = load_denylist(denylist, denylist_source)
    return {
        "rename_forms": names.form_count,
        "labels": names.label_count,
        "denylist_terms": terms.term_count,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m grove.screening")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("counts", help="print the counts of the screening files")
    command.add_argument("--rename", type=Path, help=f"the rename map; default ${RENAME_MAP_VAR}")
    command.add_argument("--denylist", type=Path, help=f"the denylist; default ${DENYLIST_VAR}")
    args = parser.parse_args(argv)

    try:
        found = counts(args.rename, args.denylist)
    except ConfigError as error:
        print(f"grove.screening: {error}", file=sys.stderr)
        return 2
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
