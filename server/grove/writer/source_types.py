"""Source types (D5): derived in code from the connector and the statement, never from a caller.

The order runs planned < said < reported_done < confirmed. A row's type comes from what code knows
about it: its connector type, its statement class, its modality, and three facts that code sets
and a caller cannot (S46): the speaker kind in the source's own metadata (an automated sender), the
calendar's log setting with the entry's date against the read, and the relay of the request that
stored the words (an answer or a correction, in the person's verbatim words). An inferred or
rolled-up row takes the weakest type among its premises. A claim's type is the strongest among
its active supporting rows.

The speaker kind on an observation is the caller's own classification, so it never raises a row's
type: a caller-sent `automated` on a chat line is read as any other chat line. Only a speaker kind
from the source's metadata (`automated=True` here) does.
"""

from dataclasses import dataclass
from typing import Any

ORDER: tuple[str, ...] = ("planned", "said", "reported_done", "confirmed")
RANK = {source_type: rank for rank, source_type in enumerate(ORDER)}
REPORT_CLASSES = frozenset({"report_happened", "report_state"})
ARTIFACT_CLASS = "artifact"
# Developer config (S27): what each connector type yields.
_ALWAYS_CONFIRMED = frozenset({"git", "web_pages", "identity"})
_ALWAYS_PLANNED = frozenset({"reminders"})
_ARTIFACT_CONNECTORS = frozenset({"notes_files_releases", "screenshots"})
_ANSWER_RELAYS = frozenset({"answer", "correction"})


@dataclass(frozen=True)
class RowFacts:
    """What code knows about one row beyond its connector type and statement class.

    `automated`: the source's metadata names an automated sender for this message.
    `logged`: a calendar entry dated before the read, on a day the person keeps as a log.
    `whole`: the row cites its whole source (the locator `whole`), so the source is the document.
    `relay`: the relay of the request that stored this source (remember, answer, correction).
    `verbatim`: that request carried the person's own words and the host message id."""

    modality: str = "text"
    whole: bool = False
    automated: bool = False
    logged: bool = False
    relay: str | None = None
    verbatim: bool = False


def derive_row_source_type(
    connector_type: str, statement_class: str, facts: RowFacts | None = None
) -> str:
    facts = facts or RowFacts()
    reported = "reported_done" if statement_class in REPORT_CLASSES else "said"
    if connector_type in _ALWAYS_PLANNED:
        return "planned"
    if connector_type == "calendar":
        # D5: a passing date never upgrades planned; only the person's log setting does.
        return "confirmed" if facts.logged else "planned"
    if facts.automated and (statement_class in REPORT_CLASSES or statement_class == ARTIFACT_CLASS):
        # The system that acted reports its own action (D5's table, line 1086).
        return "confirmed"
    if connector_type in _ALWAYS_CONFIRMED:
        return "confirmed"
    if statement_class == ARTIFACT_CLASS and connector_type in _ARTIFACT_CONNECTORS and facts.whole:
        # A document or a picture that is itself the record of an act (line 1087, 1170): the
        # whole stored file, never a line of a chat or a note, and never on the caller's word
        # about its modality.
        return "confirmed"
    if connector_type == "person_in_views":
        return "confirmed" if facts.relay in _ANSWER_RELAYS else reported
    if connector_type == "ai_host":
        if not facts.verbatim:
            # The host's own words: a statement by the host, which never confirms (line 1092).
            return "said"
        return "confirmed" if facts.relay in _ANSWER_RELAYS else reported
    return reported


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
