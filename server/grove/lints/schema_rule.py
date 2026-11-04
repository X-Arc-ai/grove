"""D14: declared free text stays in the places the forget cascade can reach.

Read the catalogue of the requested checkout, not the imported package's catalogue. This
matters for scratch controls and --root. Unknown types and unknown/empty enums fail closed.
The check covers schema declarations; it cannot infer arbitrary text hidden in a value
declared as an id, locator or developer configuration. Runtime writers enforce those types.
Findings contain paths, line numbers and rule codes only, never stored or planted text.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from grove.lints._scan import Finding, LintError

CATALOGUE = "server/grove/store/catalogue.py"
ENUMS = "server/grove/store/enums.py"

# Spec line 2561, including the registry status words of S31. A new home needs a
# corresponding cascade implementation and an explicit change to this boundary.
TEXT_HOMES = frozenset(
    {
        ("sources", "content"),
        ("items", "title"),
        ("items", "summary"),
        ("items", "body"),
        ("items", "properties"),
        ("items", "property_values"),
        ("items", "search_terms"),
        ("items", "status"),
        ("kinds", "status_vocabulary"),
        ("aliases", "phrase"),
        ("aliases", "normalised_phrase"),
        ("kinds", "name"),
        ("meanings", "name"),
        ("property_names", "name"),
        ("history_values", "value"),
        ("run_records", "proposed_operations"),
    }
)
# Digests are not a general-purpose text escape. Only these authority fields
# have the fixed lowercase hexadecimal contract enforced by their writer.
AUTHORITY_DIGEST_COLUMNS = frozenset(
    {
        ("authority_projections", "profile_digest"),
        ("authority_projections", "head_digest"),
        ("authority_projections", "state_digest"),
        ("authority_projections", "ledger_file_digest"),
        ("authority_projections", "ledger_rows_digest"),
        ("authority_operations", "profile_digest"),
        ("authority_operations", "intent_digest"),
        ("authority_operations", "prepare_head_digest"),
        ("authority_operations", "base_head_digest"),
        ("authority_operations", "base_state_digest"),
        ("authority_operations", "expected_result_digest"),
        ("authority_operations", "choice_digest"),
        ("authority_operations", "result_state_digest"),
        ("authority_operations", "result_ledger_file_digest"),
        ("authority_operations", "result_ledger_rows_digest"),
        ("authority_approval_uses", "choice_digest"),
        ("authority_approval_uses", "display_digest"),
    }
)
NON_TEXT_TYPES = frozenset(
    {
        "id",
        "ids",
        "value_id",
        "value_ids",
        "ref",
        "json_ids",
        "instant",
        "date",
        "int",
        "float",
        "bool",
        "locator",
        "code",
        "config",
    }
)


def _load(root: Path, relative: str, name: str) -> ModuleType:
    path = root / relative
    # Do not let a scratch checkout silently import the real checkout's schema.
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents) if p != root):
        raise LintError("schema input is absent or linked")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LintError("schema input cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        # Compile source directly: a same-size edit in a scratch control must not use
        # timestamp-based bytecode left by the previous arm of the control.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    except Exception:
        raise LintError("schema input cannot be read") from None
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def check(root: Path, base: str | None = None) -> list[Finding]:
    catalogue = _load(root, CATALOGUE, "_grove_schema_catalogue")
    enums = _load(root, ENUMS, "_grove_schema_enums")
    findings: list[Finding] = []
    try:
        tables = catalogue.TABLES
        codes = enums.ENUMS
        if not tables or not isinstance(codes, dict):
            raise ValueError
        names: set[str] = set()
        for table in tables:
            if table.name in names:
                findings.append(Finding(CATALOGUE, 1, "schema/duplicate-table"))
            names.add(table.name)
            props: set[str] = set()
            for column in table.columns:
                if not column.props or any(prop in props for prop in column.props):
                    findings.append(Finding(CATALOGUE, 1, "schema/duplicate-property"))
                props.update(column.props)
                kind = column.type
                if kind in {"text_home", "json_home"}:
                    if any((table.name, prop) not in TEXT_HOMES for prop in column.props):
                        findings.append(Finding(CATALOGUE, 1, "schema/text-outside-home"))
                elif kind == "digest":
                    if any(
                        (table.name, prop) not in AUTHORITY_DIGEST_COLUMNS for prop in column.props
                    ):
                        findings.append(Finding(CATALOGUE, 1, "schema/digest-outside-authority"))
                elif kind.startswith("enum:"):
                    values = codes.get(kind.removeprefix("enum:"))
                    if (
                        not isinstance(values, tuple | list)
                        or not values
                        or any(not isinstance(value, str) or not value for value in values)
                        or len(set(values)) != len(values)
                    ):
                        findings.append(Finding(CATALOGUE, 1, "schema/invalid-enum"))
                elif kind not in NON_TEXT_TYPES:
                    findings.append(Finding(CATALOGUE, 1, "schema/unknown-type"))
    except (AttributeError, TypeError, ValueError):
        raise LintError("schema declarations cannot be read") from None
    return findings
