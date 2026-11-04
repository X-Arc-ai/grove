"""Capabilities (D5): which source can prove which claim, as rows, with defaults over fields.

Resolution has two steps, and code names no meaning in either (spec lines 1094 to 1106):

1. A specific row of the world for (connector type, registry id), where the registry id is the
   claim's kind (an item) or meaning (a link). A row minted with a word applies only once the
   second model accepted it: its `accepted_judgment_id` names a recorded `field_fit` judgment
   with the verdict `fits` (S54).
2. Else the default for what the source is, keyed on the claim's declared fields: its lifecycle
   class (an item) or temporal class (a link), and on the evidence row's own fields (S54).

A source "is" one of the classes below, read from code-set fields of the evidence row and its
connector type, never from a name. Only a row typed `confirmed` can confirm: a default for a
planned or said row is never `confirms`.

The defaults are developer config, held in code like the primitive rows (a world transaction cannot
read world '' rows), and seeded as store-wide `:Capability` rows so B.1's table holds them too.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CONFIRMS = "confirms"
SHOWS_SAID = "shows_said"
SHOWS_PLANNED = "shows_planned"
NONE = "none"

# Appendix C's staleness limits, in days, by connector type. None is always fresh: git and a
# claim's own re-check command, and a calendar entry, which speaks only to its own date. A
# connector row's own staleness limit wins; a missing type fails closed at the shortest limit.
DEFAULT_STALENESS_DAYS: dict[str, int | None] = {
    "whatsapp": 14,
    "voice_notes": 14,
    "past_ai_conversations": 30,
    "notes_files_releases": 7,
    "screenshots": 7,
    "web_pages": 7,
    "git": None,
    "calendar": None,
    "reminders": 7,
    "identity": 30,
    "person_in_views": 14,
    "ai_host": 14,
}
FAIL_CLOSED_DAYS = 3

# What a confirmed row is, by its code-set fields.
CALENDAR_LOG = "calendar_log"
AUTOMATED = "automated"
IDENTITY = "identity"
GIT = "git"
PAGE = "page"
ARTIFACT = "artifact"
PERSON = "person"
STATEMENT = "statement"
PLANNED = "planned"

_CHATS = frozenset({"whatsapp", "voice_notes", "past_ai_conversations"})


@dataclass(frozen=True)
class Default:
    """One default row: what a source class may do for claims with these declared fields."""

    id: str
    source_class: str
    capability: str
    lifecycle_classes: tuple[str, ...] = ()
    temporal_classes: tuple[str, ...] = ()
    reason: str = ""

    def selector(self) -> str:
        import json

        return json.dumps(
            {
                "source_class": self.source_class,
                "lifecycle_class": list(self.lifecycle_classes),
                "temporal_class": list(self.temporal_classes),
            },
            sort_keys=True,
        )


def _id(n: int) -> str:
    return f"00000000-0000-7000-8001-{n:012d}"


_ITEMS_ALL = ("lasting_thing", "work_with_state", "record", "bounded_period")
_LINKS_ALL = ("state", "permanent", "event")

# The default table of D5 ("Connector, by what it is"), as rows. A class with no row for a claim's
# declared fields gives `none`.
DEFAULTS: tuple[Default, ...] = (
    Default(
        _id(1),
        STATEMENT,
        SHOWS_SAID,
        _ITEMS_ALL,
        _LINKS_ALL,
        "a chat, a voice note or a past AI conversation shows that a statement was made",
    ),
    Default(
        _id(2),
        AUTOMATED,
        CONFIRMS,
        ("record",),
        (),
        "an automated message confirms only its sender's own action, on items whose evidence "
        "names that sender",
    ),
    Default(
        _id(3),
        PAGE,
        CONFIRMS,
        _ITEMS_ALL,
        _LINKS_ALL,
        "a fetched page confirms what its text states, quoted in the judge's excerpt",
    ),
    Default(
        _id(4),
        ARTIFACT,
        CONFIRMS,
        _ITEMS_ALL,
        _LINKS_ALL,
        "a document or a picture that is itself the artifact confirms what it shows",
    ),
    Default(
        _id(5),
        IDENTITY,
        CONFIRMS,
        ("lasting_thing",),
        (),
        "an identity source confirms identity fields only, never where someone lives",
    ),
    Default(
        _id(6),
        CALENDAR_LOG,
        CONFIRMS,
        ("record", "bounded_period", "work_with_state"),
        (),
        "under the person's log setting, a logged past entry confirms that it happened",
    ),
    Default(
        _id(7),
        PLANNED,
        SHOWS_PLANNED,
        _ITEMS_ALL,
        _LINKS_ALL,
        "a reminder, a to-do or an unlogged calendar entry shows an intention, never an occurrence",
    ),
    Default(
        _id(8),
        GIT,
        CONFIRMS,
        ("record", "work_with_state"),
        (),
        "git confirms that a commit or a merge exists, never that a feature is live",
    ),
    Default(
        _id(9),
        PERSON,
        CONFIRMS,
        _ITEMS_ALL,
        _LINKS_ALL,
        "the person's own answer or correction confirms their own life and decisions",
    ),
)
DEFAULT_IDS = tuple(row.id for row in DEFAULTS)


def source_class(row: Mapping[str, Any], connector_type: str) -> str:
    """What an evidence row's source is, from code-set fields only."""
    source_type = row.get("source_type")
    if source_type == "planned":
        return PLANNED
    if source_type != "confirmed":
        return STATEMENT
    if connector_type == "calendar":
        return CALENDAR_LOG
    if connector_type == "identity":
        return IDENTITY
    if connector_type == "git":
        return GIT
    if connector_type == "web_pages":
        return PAGE
    if connector_type in ("person_in_views", "ai_host"):
        return PERSON
    if row.get("statement_class") == "artifact" and row.get("speaker_kind") != "automated":
        return ARTIFACT
    if row.get("speaker_kind") == "automated":
        return AUTOMATED
    if connector_type in _CHATS:
        return STATEMENT
    return ARTIFACT


@dataclass(frozen=True)
class Claim:
    """The declared fields of a claim that a capability reads."""

    claim_type: str  # item or link
    registry_id: str  # the item's kind, or the link's meaning
    lifecycle_class: str | None = None
    temporal_class: str | None = None
    # Senders named by the claim's own active evidence rows (an automated message's scope).
    senders: frozenset[str] = frozenset()
    # True for work of someone other than the person: an item of the person's kind is linked
    # from this work item, and the person's own item is not (D5: "reports others' work").
    others_work: bool = False


@dataclass(frozen=True)
class Resolved:
    capability: str
    origin: str
    row_id: str
    staleness_days: int | None
    source_class: str


def default_for(source: str, claim: Claim) -> Default | None:
    for row in DEFAULTS:
        if row.source_class != source:
            continue
        if claim.claim_type == "item" and claim.lifecycle_class in row.lifecycle_classes:
            return row
        if claim.claim_type == "link" and claim.temporal_class in row.temporal_classes:
            return row
    return None


def staleness_days(connector: Mapping[str, Any], connector_type: str) -> int | None:
    limit = connector.get("staleness_limit")
    if limit is not None:
        return int(limit)
    if connector_type in DEFAULT_STALENESS_DAYS:
        return DEFAULT_STALENESS_DAYS[connector_type]
    return FAIL_CLOSED_DAYS


def resolve(
    row: Mapping[str, Any],
    connector: Mapping[str, Any],
    connector_type: str,
    claim: Claim,
    specific: list[Mapping[str, Any]],
    accepted: set[str],
) -> Resolved:
    """The capability of one evidence row for one claim. `specific` holds the world's rows for
    this connector type and the claim's registry id; `accepted` the ids of the judgments that
    accept a minted row."""
    kind = source_class(row, connector_type)
    limit = staleness_days(connector, connector_type)
    for candidate in specific:
        if candidate.get("origin") == "minted_with_a_word" and (
            candidate.get("accepted_judgment_id") not in accepted
        ):
            continue
        own_limit = candidate.get("staleness_limit")
        capability = candidate.get("capability") or NONE
        if capability == CONFIRMS and row.get("source_type") != "confirmed":
            capability = NONE
        return Resolved(
            capability,
            candidate.get("origin") or "the_person",
            candidate["id"],
            int(own_limit) if own_limit is not None else limit,
            kind,
        )
    found = default_for(kind, claim)
    if found is None:
        return Resolved(NONE, "default", "", limit, kind)
    capability = found.capability
    if kind == AUTOMATED and (not row.get("sender") or row.get("sender") not in claim.senders):
        capability = NONE
    if kind == PERSON and claim.others_work:
        capability = NONE
    return Resolved(capability, "default", found.id, limit, kind)
