"""Generate the tables of docs/store-map.md from the catalogue.

python -m grove.store.storemap rewrites the part between the markers. With --check it exits 1 when
the file differs from what the catalogue gives.
"""

import argparse
import sys
from pathlib import Path

from grove.store.catalogue import (
    B1_GROUPS,
    FULLTEXT_INDEXES,
    LATER_TABLES,
    PLATFORM_TABLES,
    TABLES,
    TYPE_CLASSES,
    Table,
)

BEGIN = "<!-- generated:begin -->"
END = "<!-- generated:end -->"
DOC = Path(__file__).resolve().parents[3] / "docs" / "store-map.md"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _code_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{v}`" for v in values)


def _schema_items(items) -> str:
    return "; ".join(f"`{i.name}` ({', '.join(i.props)})" for i in items) or "none"


def _indexes_text(t: Table) -> str:
    return (
        "; ".join(
            f"`{i.name}` ({', '.join(i.props)})" + (" text" if i.kind == "text" else "")
            for i in t.indexes
        )
        or "none"
    )


def _table_section(t: Table) -> list[str]:
    pattern = f"`()-[:{t.label}]-()`" if t.element == "relationship" else f"`:{t.label}`"
    lines = [
        f"### {t.name}",
        "",
        f"{pattern}, {t.element}. Scope {t.scope}. Restore group {t.group}. Owner {t.owner}.",
        "",
        f"Keys: {_schema_items(t.uniques)}.",
        "",
        f"Indexes: {_indexes_text(t)}.",
        "",
    ]
    fulltext = [f.name for f in FULLTEXT_INDEXES if t.label in f.labels]
    if fulltext:
        lines += [f"Full text: {_code_list(tuple(fulltext))}.", ""]
    if t.notes:
        lines += [t.notes, ""]
    lines += ["| Spec column | Property | Type class | Origin | Note |", "|---|---|---|---|---|"]
    for c in t.columns:
        cells = (
            c.spec or "(none)",
            _code_list(c.props),
            f"`{c.type}`",
            c.origin,
            c.note,
        )
        lines.append("| " + " | ".join(_cell(v) for v in cells) + " |")
    lines.append("")
    return lines


def _type_classes() -> list[str]:
    lines = ["## Type classes", "", "| Class | Meaning |", "|---|---|"]
    lines += [f"| `{name}` | {_cell(meaning)} |" for name, meaning in TYPE_CLASSES.items()]
    homes = [
        f"`{t.name}.{p}`"
        for t in TABLES
        for c in t.columns
        if c.type in {"text_home", "json_home"}
        for p in c.props
    ]
    lines += [
        "",
        "Text homes, the only columns of type `text_home` or `json_home` (line 2561, and the "
        f"status words of S31): {', '.join(homes)}.",
        "",
    ]
    return lines


def generate() -> str:
    """Return the generated part of docs/store-map.md, without the markers."""
    lines: list[str] = _type_classes()
    for title, tables in B1_GROUPS:
        lines += [f"## {title}", ""]
        for t in tables:
            lines += _table_section(t)
    lines += ["## Platform rows", ""]
    for t in PLATFORM_TABLES:
        lines += _table_section(t)
    lines += ["## Tables of later lanes", ""]
    for t in LATER_TABLES:
        lines += _table_section(t)
    lines += [
        "## Full-text indexes",
        "",
        "| Name | Labels | Properties | Analyzer |",
        "|---|---|---|---|",
    ]
    for f in FULLTEXT_INDEXES:
        lines.append(
            f"| `{f.name}` | {_code_list(f.labels)} | {_code_list(f.props)} | `{f.analyzer}` |"
        )
    lines.append("")
    return "\n".join(lines)


def render(current: str) -> str:
    """Return the file text with the generated part replaced."""
    head, rest = current.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n{generate()}\n{END}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file differs")
    args = parser.parse_args(argv)
    current = DOC.read_text(encoding="utf-8")
    wanted = render(current)
    if args.check:
        if current != wanted:
            print("docs/store-map.md differs from the catalogue", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(wanted, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
