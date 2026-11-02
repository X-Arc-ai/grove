"""Source types (D5): derived in code from the connector and the statement, never from a caller.

The order runs planned < said < reported_done < confirmed. A row's type comes from its connector
type and its statement class; an inferred or rolled-up row takes the weakest type among its
premises. A claim's type is the strongest among its active supporting rows.

The speaker kind on an observation is the caller's own classification, so it never raises a
row's type: a caller-sent `automated` on a chat line is read as any other chat line. An automated
sender confirming its own action needs ingest metadata and a capability row (D5), which the
truth-clock lane owns.
"""

from typing import Any

ORDER: tuple[str, ...] = ("planned", "said", "reported_done", "confirmed")
RANK = {source_type: rank for rank, source_type in enumerate(ORDER)}
REPORT_CLASSES = frozenset({"report_happened", "report_state"})
# Developer config (S27): what each connector type yields.
_ARTIFACTS = frozenset({"git", "web_pages"})
_PLANNED = frozenset({"calendar"})


def derive_row_source_type(connector_type: str, statement_class: str) -> str:
    if connector_type in _PLANNED:
        # The calendar's log setting is truth-clock's seam; without it a past entry stays planned.
        return "planned"
    if connector_type in _ARTIFACTS:
        return "confirmed"
    return "reported_done" if statement_class in REPORT_CLASSES else "said"


def weakest(source_types: list[str]) -> str:
    return min(source_types, key=RANK.__getitem__)


def claim_fields(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """derived_source_type, derivation, speaker and modality of a claim from its active supporting
    rows; every value is None when no row supports it. Observed rows win over rolled-up rows, and
    those over inferred rows. Ties go to the row recorded first."""
    for derivation in ("observed", "rolled_up", "inferred"):
        group = [row for row in rows if row.get("derivation") == derivation]
        if group:
            break
    else:
        return {"derived_source_type": None, "derivation": None, "speaker": None, "modality": None}
    best = max(RANK[row["source_type"]] for row in group)
    chosen = min(
        (row for row in group if RANK[row["source_type"]] == best),
        key=lambda row: (str(row.get("recorded")), row["id"]),
    )
    return {
        "derived_source_type": ORDER[best],
        "derivation": derivation,
        "speaker": chosen.get("speaker_kind"),
        "modality": chosen.get("modality"),
    }
