"""Every B.1 table and platform row of the store, as data.

The migrations, docs/store-map.md, the world scoping and the schema lint all read this file. A
change to the store is one new numbered migration plus the matching row here.

Spec lines in notes refer to the architecture decisions document, a private maintainer document;
they are references for maintainers and the notes read without them. S numbers are the rulings of
docs/store-map.md.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    spec: str  # the B.1 column name; "" when B.1 does not name it
    props: tuple[str, ...]
    type: str
    origin: str = "b1"  # b1 | convention | ruling
    note: str = ""


@dataclass(frozen=True)
class Constraint:
    name: str
    props: tuple[str, ...]


@dataclass(frozen=True)
class Index:
    name: str
    props: tuple[str, ...]
    kind: str = "range"  # range | text


@dataclass(frozen=True)
class FulltextIndex:
    name: str
    labels: tuple[str, ...]
    props: tuple[str, ...]
    analyzer: str = "standard-folding"


@dataclass(frozen=True)
class Table:
    name: str
    label: str
    element: str  # node | relationship
    scope: str  # world | platform
    group: str  # projection | snapshot | ledger | platform
    owner: str
    columns: tuple[Column, ...]
    uniques: tuple[Constraint, ...]
    indexes: tuple[Index, ...] = ()
    notes: str = ""


# The type classes of Column.type. The schema lint of the forget lane reads them: a text_home or
# json_home column outside line 2561's homes is a bug.
TYPE_CLASSES: dict[str, str] = {
    "id": "UUIDv7 text from new_id(), with the exceptions of S4",
    "ids": "a list of ids",
    "value_id": "the id of a history_values row that holds the text",
    "value_ids": "a list of value ids",
    "ref": "S32: a subject reference, `<target type>:<id>`; a pair is two references, sorted, "
    "joined by a bar",
    "text_home": "free text in one of the homes of line 2561",
    "json_home": "a JSON string in one of the homes of line 2561",
    "json_ids": "a JSON string that holds only ids, codes and numbers",
    "enum:<name>": "a code of the closed list of that name in enums.py",
    "instant": "a ZONED DATETIME in UTC",
    "date": "a DATE from the clock provider",
    "int": "an integer",
    "float": "a float",
    "bool": "a boolean",
    "locator": "a place in a source, or a URL",
    "code": "a machine code, not free text",
    "config": "developer-written configuration",
}


def _snake(spec: str) -> str:
    return re.sub(r"\s+", "_", spec.strip().lower().replace("-", ""))


def _col(spec: str, type_: str, *props: str, note: str = "") -> Column:
    return Column(spec, props or (_snake(spec),), type_, "b1", note)


def _ruling(prop: str, type_: str, note: str, *, spec: str = "") -> Column:
    return Column(spec, (prop,), type_, "ruling", note)


def _convention(prop: str, type_: str, note: str, *, spec: str = "") -> Column:
    return Column(spec, (prop,), type_, "convention", note)


def _keys(table: str, *groups: tuple[str, ...]) -> tuple[Constraint, ...]:
    return tuple(Constraint(f"{table}_{'_'.join(g)}_unique", g) for g in groups)


def _ranges(table: str, *groups: tuple[str, ...]) -> tuple[Index, ...]:
    return tuple(Index(f"{table}_{'_'.join(g)}_idx", g) for g in groups)


_WORLD = _col("world_id", "id")
_WORLD_S5 = _ruling(
    "world_id", "id", "S5: line 3081 wins over the table's own list", spec="world_id"
)
_ID = _col("id", "id", note="UUIDv7 text from new_id() (S4)")


def _version_touched(*, version_in_b1: bool = False) -> tuple[Column, ...]:
    """The S12 pair for a row the writer updates in place."""
    touched = _ruling("touched_at", "instant", "S12, line 3081", spec="touched at")
    if version_in_b1:
        return (_col("version", "int"), touched)
    return (_ruling("version", "int", "S12, line 3081", spec="version"), touched)


def _world_clock(spec: str) -> tuple[Column, ...]:
    """A world clock of S3: two bounds, a precision, and the original words."""
    name = _snake(spec)
    return (
        Column(
            spec,
            (f"{name}_earliest", f"{name}_latest"),
            "instant",
            note="S3: UTC bounds; absent while unknown",
        ),
        Column(spec, (f"{name}_precision",), "enum:precision", note="S3"),
        _ruling(f"{name}_words", "value_id", "S3, line 915: the original words"),
    )


def _claim_columns() -> tuple[Column, ...]:
    """The clock and horizon columns that items and links share, in B.1 order."""
    return (
        *_world_clock("became true"),
        *_world_clock("stopped being true"),
        _col("end class", "enum:end_class"),
        _col("recorded", "instant", note="set by the writer at commit"),
        _col("last confirmed", "instant"),
        _col("confirmed against", "id", note="a verification id"),
        _col("re-check by", "instant"),
        _col(
            "re-check how",
            "json_ids",
            note="connector, locator or query, what to compare; or the code ask_the_person",
        ),
        _col("derived source type", "enum:source_type"),
        _col("derivation", "enum:derivation"),
    )


def _provenance() -> tuple[Column, ...]:
    return (
        _col("provenance", "id", "provenance_evidence_id", note="the creating evidence id"),
        _col("provenance", "value_id", "provenance_reason", note="the reason"),
    )


def _registry_columns(*, version_touched: tuple[Column, ...]) -> tuple[Column, ...]:
    """Columns of kinds that S26 also gives to meanings, in the order of line 3089."""
    return (
        _col("status", "enum:registry_status", note="starter rows are canonical at seed (B.4)"),
        _col("origin", "enum:registry_origin"),
        _col("reason", "value_id"),
        _col("worked example", "id", note="an evidence id"),
        _col(
            "nearest neighbours at mint",
            "json_ids",
            note="word ids, each stated distinction a value id",
        ),
        _col("minted at", "instant"),
        _col("minted by", "code", note="the agent, the person or the seed (line 1579)"),
        _col("distinct episodes", "int"),
        _col("uses", "int"),
        _col("first used", "instant"),
        _col("last used", "instant"),
        _col("primitive", "bool"),
        *version_touched,
        _ruling(
            "disputed_fields",
            "json_ids",
            "O5, line 1595: the declared fields the second model disputed, for the next pass",
        ),
        _ruling(
            "earning_episodes",
            "json_ids",
            "O8, line 1603: the episodes a provisional word counted toward the earning bar",
        ),
    )


# Graph and registry.

ITEMS = Table(
    name="items",
    label="Item",
    element="node",
    scope="world",
    group="projection",
    owner="D1, D4, D5",
    columns=(
        _ID,
        _WORLD,
        _col("kind", "id", note="a kind id"),
        _col("title", "text_home"),
        _col("summary", "text_home"),
        _col("body", "text_home"),
        _col(
            "properties",
            "json_home",
            note="a JSON string (S16); a state property only on a record, one per record",
        ),
        _ruling(
            "property_values",
            "text_home",
            "S16: LIST<STRING> derived from properties, read by the full-text indexes",
        ),
        _col("search terms", "text_home", note="LIST<STRING>, the term text only (D7, D10)"),
        _col(
            "status",
            "text_home",
            note="S31: a status word of the kind's vocabulary, a registry name an agent can mint",
        ),
        _ruling(
            "status_role",
            "enum:status_role",
            "S11: written with status; B.1 names it only in the index",
            spec="status role",
        ),
        _col("status since", "instant"),
        *_claim_columns(),
        _col("speaker", "enum:speaker_kind", note="of the evidence that sets the source type"),
        _col("modality", "enum:modality"),
        *_provenance(),
        _col("version", "int"),
        _col("touched at", "instant"),
    ),
    uniques=_keys("items", ("id",)),
    indexes=(
        *_ranges(
            "items",
            ("world_id", "kind", "status_role"),
            ("world_id", "became_true_earliest"),
            ("world_id", "became_true_latest"),
            ("world_id", "recorded"),
        ),
        Index("items_title_text", ("title",), "text"),
    ),
    notes=(
        "B.1's (world_id, became true) is two indexes, one per bound: a range index leaves out a "
        "row without the property, and a precision of before has no earliest bound. Full text and "
        "trigram are S17. Scheduled times live in properties (line 920)."
    ),
)

LINKS = Table(
    name="links",
    label="LINK",
    element="relationship",
    scope="world",
    group="projection",
    owner="D1, D3, D4, D6",
    columns=(
        _ID,
        _WORLD,
        _col("from item", "id", note="repeats the start node for index use (S7)"),
        _col("meaning", "id", note="a meaning id, never a relationship type (S7)"),
        _col("to item", "id", note="repeats the end node for index use (S7)"),
        *_claim_columns(),
        *_provenance(),
        _col("version", "int"),
        _ruling("touched_at", "instant", "S12, line 3081", spec="touched at"),
        _ruling(
            "active_key",
            "code",
            "S7: world, from, meaning, to joined by a bar; present only while active; a mark "
            "adds its trigger change set",
        ),
        _ruling("original_phrase", "value_id", "S8: the phrase of a generic link"),
    ),
    uniques=_keys("links", ("id",), ("active_key",)),
    indexes=_ranges(
        "links",
        ("world_id", "from_item", "meaning"),
        ("world_id", "to_item", "meaning"),
    ),
    notes=(
        "unique active (world_id, from item, meaning, to item) is active_key (S7). Neo4j has no "
        "partial index, so the two range indexes also hold ended links, and a query for active "
        "links filters on active_key IS NOT NULL. A mark is a link of the primitive meaning "
        "affected by, plus one impacts row."
    ),
)

KINDS = Table(
    name="kinds",
    label="Kind",
    element="node",
    scope="world",
    group="projection",
    owner="D1, D8",
    columns=(
        _ID,
        _col("world_id", "id", note="'' for primitive rows (S5)"),
        _col("name", "text_home"),
        _col("description", "value_id"),
        _col("lifecycle class", "enum:lifecycle_class"),
        _col(
            "status vocabulary with each status's role",
            "json_home",
            "status_vocabulary",
            note="S31: ordered list of {status, role}; each status word is a registry name",
        ),
        _col("required fields", "json_ids"),
        _col("required links", "json_ids"),
        *_registry_columns(version_touched=_version_touched(version_in_b1=True)),
        _ruling(
            "propagating_fields",
            "json_ids",
            "S25, line 1238: date fields, each with a role of start, due or end",
        ),
    ),
    uniques=_keys("kinds", ("id",), ("world_id", "name")),
)

MEANINGS = Table(
    name="meanings",
    label="Meaning",
    element="node",
    scope="world",
    group="projection",
    owner="D1, D6, D8",
    columns=(
        _ID,
        _col("world_id", "id", note="'' for primitive rows (S5)"),
        _col("name", "text_home", note="a registry column of kinds (S26)"),
        _col("description", "value_id", note="a registry column of kinds (S26)"),
        *_registry_columns(version_touched=_version_touched(version_in_b1=True)),
        _col("from kinds", "json_ids", note="kind ids, a lifecycle class or any"),
        _col("to kinds", "json_ids", note="kind ids, a lifecycle class or any"),
        _col(
            "direction", "enum:direction", note="line 3241: from the first endpoint to the second"
        ),
        _col("cardinality and group", "enum:cardinality", "cardinality"),
        _col(
            "cardinality and group",
            "code",
            "cardinality_group",
            note="absent on every starter meaning",
        ),
        _col("carries consequences", "bool"),
        _col("downstream end", "enum:downstream_end"),
        _col("decomposition", "bool"),
        _col("rolls up completion", "bool"),
        _col("temporal class", "enum:temporal_class"),
        _col("default horizon", "int", note="days; absent when none"),
        _ruling(
            "generic",
            "bool",
            "S25: lets code find the generic connection meaning (lines 1596, 1630)",
        ),
    ),
    uniques=_keys("meanings", ("id",), ("world_id", "name")),
    notes="S26: lifecycle class, status vocabulary, required fields, required links stay on kinds.",
)

PROPERTY_NAMES = Table(
    name="property_names",
    label="PropertyName",
    element="node",
    scope="world",
    group="projection",
    owner="D8",
    columns=(
        _ID,
        _WORLD,
        _col("kind", "id", note="a kind id"),
        _col("name", "text_home"),
        _col("value type", "enum:value_type"),
        _col("temporal class", "enum:temporal_class"),
        _col("default horizon", "int", note="days; absent when none"),
        _col("description", "value_id"),
        _col("status", "enum:registry_status"),
        _col("origin", "enum:registry_origin"),
        _col("reason", "value_id"),
        _col("worked example", "id", note="an evidence id"),
        _col("counts", "int", "distinct_episodes", "uses", note="line 1580"),
        _col("counts", "instant", "first_used", "last_used", note="line 1580"),
        _convention("minted_at", "instant", "line 1579: on all registry rows", spec="minted at"),
        _convention("minted_by", "code", "line 1579: on all registry rows", spec="minted by"),
        *_version_touched(),
        _ruling(
            "scheduled_role",
            "enum:scheduled_role",
            "S25, lines 832, 2067: marks a property as a scheduled time",
        ),
        _ruling(
            "nearest_neighbours_at_mint",
            "json_ids",
            "O1, line 1578: the two neighbours of a minted property name, as on kinds",
        ),
        _ruling(
            "disputed_fields",
            "json_ids",
            "O5, line 1595: the declared fields the second model disputed, for the next pass",
        ),
        _ruling(
            "earning_episodes",
            "json_ids",
            "O8, line 1603: the episodes a provisional word counted toward the earning bar",
        ),
    ),
    uniques=_keys("property_names", ("id",), ("world_id", "kind", "name")),
)

ALIASES = Table(
    name="aliases",
    label="Alias",
    element="node",
    scope="world",
    group="projection",
    owner="D1, D7, D8",
    columns=(
        _ID,
        _WORLD,
        _col("phrase", "text_home"),
        _col("normalised phrase", "text_home"),
        _col("target type", "enum:alias_target_type"),
        _col("target id", "id"),
        _col("reversed", "bool", note="S26: false on non-meaning aliases; never defaulted"),
        _col("valid from and to", "instant", "valid_from", "valid_to", note="role aliases (D7)"),
        _col("worked example", "id", note="an evidence id"),
        _col("added at", "instant"),
        _col("reason", "value_id"),
        _ruling("status", "enum:registry_status", "S26, line 1572"),
        _ruling("origin", "enum:registry_origin", "S26, line 1573"),
        _ruling("minted_at", "instant", "S26, line 1579"),
        _ruling("minted_by", "code", "S26, line 1579"),
        *_version_touched(),
    ),
    uniques=_keys(
        "aliases", ("id",), ("world_id", "normalised_phrase", "target_type", "target_id")
    ),
    indexes=(Index("aliases_normalised_phrase_text", ("normalised_phrase",), "text"),),
    notes="B.1's target is the pair target_type, target_id. Trigram on the phrase is S17.",
)

ONTOLOGY_CHANGE_LOG = Table(
    name="ontology_change_log",
    label="OntologyChange",
    element="node",
    scope="world",
    group="snapshot",
    owner="D8",
    columns=(
        _ID,
        _WORLD,
        _col("operation", "enum:ontology_operation"),
        _col(
            "rows before and after",
            "json_ids",
            "rows_before",
            "rows_after",
            note="ids and field values of registry rows, text fields as value ids",
        ),
        _col("ids re-pointed", "ids"),
        _col("inverse operation", "json_ids"),
        _col("reason", "value_id"),
        _col("evidence ids", "ids"),
        _col("judgment ids", "ids", note="the two-model verdicts the operation cites"),
        _col("actor", "code"),
        _col("at", "instant"),
        _col("undone by", "id"),
        # O10: undone by changes after the row is written, so the row carries the S12 pair.
        *_version_touched(),
    ),
    uniques=_keys("ontology_change_log", ("id",)),
    indexes=_ranges("ontology_change_log", ("world_id", "at")),
)

DISTINCT_PAIRS = Table(
    name="distinct_pairs",
    label="DistinctPair",
    element="node",
    scope="world",
    group="snapshot",
    owner="D2, D10",
    columns=(
        _WORLD,
        _col("item a", "id"),
        _col("item b", "id"),
        _col("reason", "value_id"),
        _col("evidence id", "id"),
        _col("recorded at", "instant"),
        _col("recorded by", "code"),
    ),
    uniques=_keys("distinct_pairs", ("world_id", "item_a", "item_b")),
)

AREAS = Table(
    name="areas",
    label="Area",
    element="node",
    scope="world",
    group="snapshot",
    owner="D8, D11",
    columns=(
        _ID,
        _WORLD,
        _col("hub item", "id", note="several areas can share a hub (line 2145)"),
        _col("partition rule", "json_ids", note="meaning ids or second-level hub ids"),
        _col("status", "enum:area_status"),
        _col("merged into", "id"),
        _col("member count", "int"),
        _col("packet size in tokens", "int"),
        _col("dirt score", "int"),
        _col("last audited at", "instant"),
        _col("audit count", "int"),
        _col("never audited", "bool"),
        _col("last findings", "ids", note="finding ids"),
        *_version_touched(),
    ),
    uniques=_keys("areas", ("id",)),
    indexes=_ranges(
        "areas",
        ("world_id", "hub_item"),
        ("world_id", "dirt_score"),
        ("world_id", "last_audited_at"),
    ),
    notes="S28: B.1 has no membership column. The map lists this as an open point.",
)

# Sources and evidence.

CONNECTORS = Table(
    name="connectors",
    label="Connector",
    element="node",
    scope="world",
    group="snapshot",
    owner="D5, D10, D13",
    columns=(
        _ID,
        _WORLD,
        _col("type", "enum:connector_type"),
        _col("auth reference", "code"),
        _col("scope", "json_ids", note="an allowlist compiled into the read (D14)"),
        _col("settings", "json_ids", note="calendar log mode, calendar id, from date, days logged"),
        _col("batch rule", "code"),
        _col(
            "staleness limit",
            "int",
            note="days; absent means the type's Appendix C value (S49), always fresh for git",
        ),
        _col("expected read interval", "int", note="seconds (D7)"),
        _col("watermark", "locator"),
        _col("last attempted read", "instant"),
        _col("last successful read", "instant"),
        _col(
            "last outcome with its reason as a code",
            "enum:connector_outcome",
            "last_outcome",
            note="line 2391: denied and failed are the two cases of could not read",
        ),
        _col("items taken in over 7 days", "int"),
        _col("oldest unprocessed item", "instant"),
        _col(
            "counters (read, held nothing, extracted, denied, failed)",
            "int",
            "read_count",
            "held_nothing_count",
            "extracted_count",
            "denied_count",
            "failed_count",
        ),
        *_version_touched(),
    ),
    uniques=_keys("connectors", ("id",)),
    indexes=_ranges("connectors", ("world_id", "type")),
    notes="S27: parser, locator format, source-type mapping and speaker rules are code, not rows.",
)

CAPABILITIES = Table(
    name="capabilities",
    label="Capability",
    element="node",
    scope="world",
    group="projection",
    owner="D5, D11",
    columns=(
        _ID,
        _col("world_id", "id", note="'' for defaults (S5)"),
        _col("connector type", "enum:connector_type"),
        _col("a registry id", "id", "registry_id", note="absent on a selector row"),
        _col(
            "a declared-field selector",
            "json_ids",
            "selector",
            note="lifecycle class, statement class, temporal class; keys sorted, so equal "
            "selectors have equal text",
        ),
        _col("capability", "enum:capability"),
        _col("staleness limit", "int", note="days, where it differs"),
        _col("origin", "enum:capability_origin"),
        _col("reason", "value_id"),
        _ruling(
            "accepted_judgment_id",
            "id",
            "S49: the second model's field_fit judgment that accepts a row minted with a word",
        ),
        *_version_touched(),
    ),
    uniques=_keys("capabilities", ("id",)),
    indexes=_ranges(
        "capabilities",
        ("connector_type", "registry_id"),
        ("connector_type", "selector"),
    ),
)

SOURCES = Table(
    name="sources",
    label="Source",
    element="node",
    scope="world",
    group="snapshot",
    owner="D2",
    columns=(
        _ID,
        _WORLD,
        _col("connector id", "id"),
        _col("external locator", "locator"),
        _col("received at", "instant"),
        _col("the screened content or a pointer to it", "text_home", "content"),
        _col("the screened content or a pointer to it", "locator", "content_pointer"),
        _col("parser version", "code"),
        _col("untrusted", "bool"),
        _ruling(
            "speakers",
            "json_ids",
            "S46: message key to {kind, locator, sender, original_author}, set by the connector",
        ),
        _ruling(
            "document",
            "bool",
            "S46: the connector stored an uploaded file or picture, not typed text",
        ),
        _ruling(
            "calendar_events",
            "json_ids",
            "S46: bounded retained-message facts: scheduled_at, timezone, status and updated_at",
        ),
        _col("forget state", "enum:forget_state", note="changed in place by a forget (line 2525)"),
        *_version_touched(),
    ),
    uniques=_keys("sources", ("id",)),
    indexes=_ranges("sources", ("world_id", "connector_id", "external_locator")),
)

EPISODES = Table(
    name="episodes",
    label="Episode",
    element="node",
    scope="world",
    group="snapshot",
    owner="D2, D10",
    columns=(
        _ID,
        _WORLD,
        _col("connector id", "id"),
        _col("source ids", "ids"),
        _col(
            "first and last message key",
            "code",
            "first_message_key",
            "last_message_key",
        ),
        _col("start and end time", "instant", "start_time", "end_time"),
        _col("cut rule", "code"),
        _col("read at", "instant"),
        _col("run id", "id"),
        *_version_touched(),
    ),
    notes="A later cut of the same window keeps the id and extends the range (line 761), so S12 "
    "gives the row a version.",
    uniques=_keys("episodes", ("id",), ("world_id", "connector_id", "first_message_key")),
)

EVIDENCE = Table(
    name="evidence",
    label="Evidence",
    element="node",
    scope="world",
    group="projection",
    owner="D2, D5, D9",
    columns=(
        _ID,
        _WORLD,
        _col("episode id", "id"),
        _col("source id", "id"),
        _col("locator", "locator"),
        _col("message key", "code"),
        _col("source time", "instant"),
        _ruling("source_offset_minutes", "int", "S3, line 914: the source's own UTC offset"),
        _ruling("person_timezone", "code", "S3, line 914: an IANA code"),
        _col("target type", "enum:evidence_target_type"),
        _col("target id", "id", note="a belief's id is its item's id (S13)"),
        _col("polarity", "enum:polarity"),
        _col("statement class", "enum:statement_class"),
        _col("source type", "enum:source_type", note="derived by the writer"),
        _col("derivation", "enum:derivation"),
        _col("premises", "ids", note="inferred rows only"),
        _col("speaker kind", "enum:speaker_kind"),
        _col("speaker item", "id"),
        _col("original author", "id", note="an item id, for forwarded content"),
        _ruling("sender", "code", "S46: the sender code of the source's metadata"),
        _col("modality", "enum:modality"),
        _col("active", "bool"),
        _col("recorded", "instant"),
        _col("ended at", "instant"),
        _col("end reason", "enum:evidence_end_reason", note="S15"),
        _col("request id", "id"),
        *_version_touched(),
    ),
    uniques=_keys(
        "evidence",
        ("id",),
        ("world_id", "message_key", "target_id", "polarity"),
    ),
    indexes=_ranges(
        "evidence",
        ("world_id", "target_type", "target_id", "polarity", "active"),
        ("episode_id",),
        ("source_id", "locator"),
    ),
)

HOLDS = Table(
    name="holds",
    label="Hold",
    element="node",
    scope="world",
    group="snapshot",
    owner="D10",
    columns=(
        _ID,
        _WORLD,
        _col("episode id", "id"),
        _col("locator", "locator"),
        _col("reason", "value_id"),
        _col("proposed in run", "id"),
        _col("created at", "instant"),
        _col("expires at", "instant"),
        _col("state", "enum:hold_state"),
        _col(
            "promoted to",
            "ids",
            note="line 2191, promoted_to_refs[]: the item, link and evidence rows it became",
        ),
        _col("closed at", "instant"),
        _col("close reason", "value_id"),
        *_version_touched(),
    ),
    uniques=_keys("holds", ("id",)),
    indexes=_ranges("holds", ("world_id", "state", "expires_at")),
)

UNRESOLVED_MENTIONS = Table(
    name="unresolved_mentions",
    label="UnresolvedMention",
    element="node",
    scope="world",
    group="snapshot",
    owner="D10",
    columns=(
        _ID,
        _WORLD,
        _col("episode id", "id"),
        _col("locator", "locator"),
        _col(
            "candidate item ids with scores",
            "json_ids",
            "candidate_item_ids",
            note="list of {item id, score}",
        ),
        _col(
            "context used",
            "json_ids",
            note="the rule that decided, with the item ids it read",
        ),
        _col("waiting links", "ids", note="op ids"),
        _col("state", "enum:mention_state"),
        _col("resolved to", "id"),
        _col("question id", "id"),
        _col("expires at", "instant"),
        _ruling("expiry_reason", "code", "S25, lines 2004, 2105"),
        *_version_touched(),
    ),
    uniques=_keys("unresolved_mentions", ("id",)),
    indexes=_ranges("unresolved_mentions", ("world_id", "state", "expires_at")),
)

SCREENING_RECEIPTS = Table(
    name="screening_receipts",
    label="ScreeningReceipt",
    element="node",
    scope="world",
    group="snapshot",
    owner="D14",
    columns=(
        _col("source id", "id"),
        _WORLD,
        _col("counts per class", "json_ids", note="never values"),
        _col("screened at", "instant"),
        _col("screened where", "enum:screened_where", note="S27: one reachable value"),
    ),
    uniques=_keys("screening_receipts", ("world_id", "source_id")),
)

# History and requests.

CHANGE_ENTRIES = Table(
    name="change_entries",
    label="ChangeEntry",
    element="node",
    scope="world",
    group="snapshot",
    owner="D3",
    columns=(
        _col("sequence", "int", note="per world, allocated on :World (S10)"),
        _WORLD,
        _col("entity type", "enum:entity_type"),
        _col("entity id", "id"),
        _col("field", "code"),
        _col("old value id", "value_id"),
        _col("new value id", "value_id"),
        _col("recorded at", "instant"),
        _col("world time", "instant"),
        _col("reason", "value_id"),
        _col("evidence id", "id"),
        _col("actor", "code"),
        _col("request id", "id"),
        _col("run id", "id"),
        _col("commit id", "id", note="UUIDv7; one commit may hold several change sets (S10)"),
        _col("change set id", "id", note="UUIDv7 (S10)"),
        _col("inverse of", "id", note="the change set this entry undoes"),
        _col("history only", "bool", note="a late update that did not move the head (D1)"),
    ),
    uniques=_keys("change_entries", ("world_id", "sequence")),
    indexes=_ranges(
        "change_entries",
        ("world_id", "entity_type", "entity_id", "sequence"),
        ("change_set_id",),
        ("request_id",),
        ("run_id",),
    ),
    notes="Append-only: the writer adds rows and nothing updates or deletes them (line 838).",
)

HISTORY_VALUES = Table(
    name="history_values",
    label="HistoryValue",
    element="node",
    scope="world",
    group="snapshot",
    owner="D3, D14",
    columns=(
        _ID,
        _WORLD,
        _col("value", "text_home", note="the value or the forgotten marker"),
        _col("forgotten at", "instant"),
        _ruling(
            "item_id",
            "id",
            "S18: only on a row that also has the label PastValue",
        ),
    ),
    uniques=_keys("history_values", ("id",)),
    notes=(
        "A row that holds an old value of an item's content column also has the label :PastValue "
        "(S18). No text is copied, so a forget that replaces the value clears the index."
    ),
)

REQUESTS = Table(
    name="requests",
    label="Request",
    element="node",
    scope="world",
    group="snapshot",
    owner="D2",
    columns=(
        _col("id", "id", note="the caller's idempotency key, unique per world (S9, S41)"),
        _WORLD,
        _col("caller", "code", note="the actor code of the first writer"),
        _col(
            "source id of the request's text",
            "id",
            "source_id",
            note="only a row with one can be claimed (S41)",
        ),
        _col("run id", "id"),
        _col("status", "enum:request_status"),
        _col("attempts", "int"),
        _col("created at", "instant"),
        _col("committed at", "instant"),
        _col(
            "receipt",
            "json_ids",
            note="ids and codes; a title is read fresh when shown. "
            "A declined row holds its stored decline here (S42)",
        ),
        _col("last error code", "code"),
        *_version_touched(),
        _ruling("commit_id", "id", "S41: the one commit of this request"),
        _ruling("change_set_id", "id", "S41: the one change set of this request"),
        _ruling("fingerprint", "code", "S41: sha256 of the committed batch"),
        _ruling("declined_fingerprint", "code", "S41: sha256 of the batch last declined"),
        _ruling("repairs", "int", "S41: 1 once the one repair attempt is spent (line 748)"),
        _ruling("retry_at", "instant", "S41: when a queued or failed row is due again"),
        _ruling("lease_until", "instant", "S41: when an applying row is due again"),
        _ruling("claimed_by", "code", "S41: the worker that holds the lease"),
        _ruling("content_key", "code", "S41: what a second enqueue under this key compares"),
        _ruling("verbatim", "bool", "S41: B.3 remember, for the later pass"),
        _ruling("host_message_id", "locator", "S41: B.3 remember, for the later pass"),
        _ruling("said_at", "instant", "S41: B.3 remember, for the later pass"),
        _ruling("about", "ids", "S41: B.3 remember, for the later pass"),
        _ruling("relay", "enum:relay", "S48: remember, answer or correction"),
    ),
    uniques=_keys("requests", ("world_id", "id")),
    indexes=_ranges("requests", ("world_id", "status")),
    notes=(
        "A request commits once, in one batch, and `saved` moves in the same transaction as that "
        "commit (S41). A filter in MATCH or WHERE reads with no lock, so a transition checks the "
        "row after it holds the row's write lock (S1)."
    ),
)

DECLINES = Table(
    name="declines",
    label="Decline",
    element="node",
    scope="world",
    group="snapshot",
    owner="D2",
    columns=(
        _WORLD_S5,
        _col("request id", "id", note="a requests id (S9)"),
        _col("op id", "code"),
        _col("rule", "code"),
        _col(
            "near-duplicate candidate ids with scores",
            "json_ids",
            "nearduplicate_candidate_ids",
            note="list of {item id, score}",
        ),
        _col("override used", "bool"),
        _ruling("code", "code", "S42: the typed error code of the refusal"),
        _ruling("recorded_at", "instant", "S42: when the decline was logged"),
    ),
    uniques=_keys("declines", ("world_id", "request_id", "op_id")),
    notes=(
        "Ids and codes only: a refusal's message and details are never stored, and an op id that "
        "could hold text is stored by its hash (S42). `op_id` is `batch` for a fault of the whole "
        "batch."
    ),
)

JOBS = Table(
    name="jobs",
    label="Job",
    element="node",
    scope="world",
    group="snapshot",
    owner="A.1, D10, D15",
    columns=(
        _ID,
        _WORLD,
        _col("lane", "enum:job_lane"),
        _col("agent or job type", "code"),
        _col("trigger", "code"),
        _col("input references", "json_ids"),
        _col("priority", "int"),
        _col("state", "enum:job_state"),
        _col("attempts", "int"),
        _col("claimed by", "code"),
        _col("claimed at", "instant"),
        _col("run after", "instant"),
        *_version_touched(),
    ),
    uniques=_keys("jobs", ("id",)),
    indexes=_ranges("jobs", ("world_id", "lane", "state", "priority", "run_after")),
    notes=(
        "There is no SKIP LOCKED. A filter in MATCH or WHERE reads with no lock, so two runners "
        "that filter on state = queued both claim one job. A claim must check the state after "
        "it holds the node's write lock (S1): first a write to another property of the job, "
        "then WITH, then the check, then the change; or the check inside a CASE on the right "
        "side of SET, with the caller comparing the returned claimed_by. The runner claims a job "
        "only under the world lock (`grove.writer.runs`), and `tests/runner/test_runs.py` holds "
        "the two-transaction race test."
    ),
)

RUN_RECORDS = Table(
    name="run_records",
    label="RunRecord",
    element="node",
    scope="world",
    group="snapshot",
    owner="3.5, D12, D15",
    columns=(
        _ID,
        _WORLD,
        _col("agent", "code"),
        _col("trigger", "code"),
        _col("input ids", "ids"),
        _col("model", "code"),
        _col("effort", "enum:effort"),
        _col(
            "tokens in, cached and out",
            "int",
            "tokens_in",
            "tokens_cached",
            "tokens_out",
        ),
        _col(
            "proposed operations",
            "json_home",
            note="a JSON string (S16); kept 30 days, then ids and counts",
        ),
        _col("receipts", "json_ids"),
        _col("decline ids", "ids", note="op ids"),
        _col("outcome", "code"),
        _col("token and time caps", "int", "token_cap", "time_cap"),
        _col("started", "instant"),
        _col("finished", "instant"),
        _col(
            "scorecard measures before and after",
            "json_ids",
            "scorecard_before",
            "scorecard_after",
            note="consolidation runs only",
        ),
        *_version_touched(),
    ),
    uniques=_keys("run_records", ("id",)),
    indexes=_ranges("run_records", ("world_id", "agent", "started")),
)

MODEL_CALLS = Table(
    name="model_calls",
    label="ModelCall",
    element="node",
    scope="world",
    group="snapshot",
    owner="D15",
    columns=(
        _ID,
        _WORLD,
        _col("run id", "id"),
        _col("job", "code"),
        _col("model", "code"),
        _col("effort", "enum:effort"),
        _col(
            "input, cached and output tokens",
            "int",
            "input_tokens",
            "cached_tokens",
            "output_tokens",
            note="reasoning included",
        ),
        _col("cost from the price table", "float", "cost"),
        _col("receipt reference", "id"),
        _col("at", "instant"),
        _ruling(
            "day",
            "date",
            "S24: a DATE from the clock provider; B.1 names it only in the index",
            spec="day",
        ),
        _ruling("audio_seconds", "float", "S24, line 2684: voice calls"),
        _ruling("route", "code", "S39: meta or modal"),
        _ruling(
            "outcome",
            "code",
            "S39: ok, invalid_output, rate_limited, provider_error, client_closed",
        ),
        _ruling(
            "reasoning_tokens", "int", "S39, line 2694: the reasoning share of the output tokens"
        ),
        _ruling("latency_ms", "int", "S39, line 2694: from the request to the answer"),
        _ruling("usage_estimated", "bool", "S39: true when the tokens are an estimate"),
    ),
    uniques=_keys("model_calls", ("id",)),
    indexes=_ranges("model_calls", ("world_id", "day")),
    notes="Written only by the model-call layer (S20), with the budget it spends in the same "
    "transaction (S39).",
)

JUDGMENTS = Table(
    name="judgments",
    label="Judgment",
    element="node",
    scope="world",
    group="snapshot",
    owner="D2, D4, D8, D9",
    columns=(
        _ID,
        _WORLD,
        _col("model call id", "id"),
        _col("role", "enum:judgment_role"),
        _col(
            "subject",
            "ref",
            note="S32: the claim, belief, word or pair judged; a word with no row yet is "
            "`value:<id>`",
        ),
        _col("the evidence row", "id", "evidence_id", note="where there is one"),
        _ruling(
            "source_id",
            "id",
            "S46: the stored source a judge read when no evidence row exists yet",
        ),
        _ruling("locator", "locator", "S46: where in that source"),
        _col("verdict", "enum:verdict"),
        _col("stated confidence", "float"),
        _col("correlated", "bool", note="true when the second verdict is the same model family"),
        _col("at", "instant"),
    ),
    uniques=_keys("judgments", ("id",)),
    indexes=_ranges("judgments", ("world_id", "subject", "role"), ("model_call_id",)),
    notes="Written only by the model-call layer (S20).",
)

BUDGETS = Table(
    name="budgets",
    label="Budget",
    element="node",
    scope="world",
    group="snapshot",
    owner="D15",
    columns=(
        _WORLD,
        _col("day", "date", note="S24: from the clock provider"),
        _col("ceiling", "float"),
        _col("spent", "float"),
        _col("state", "enum:budget_state"),
    ),
    uniques=_keys("budgets", ("world_id", "day")),
    notes="Written by the model-call layer, with each model call, and by the operator's ceiling "
    "(S39).",
)

# Truth, consequence and belief.

VERIFICATIONS = Table(
    name="verifications",
    label="Verification",
    element="node",
    scope="world",
    group="snapshot",
    owner="D4, D11",
    columns=(
        _ID,
        _WORLD,
        _col("claim", "id", "claim_id", note="an item or link id; a belief's item id (S13)"),
        _col("run", "id", "run_id"),
        _convention(
            "at",
            "instant",
            "the index (world_id, claim id, at) names it; neither column list does",
            spec="at",
        ),
        _col(
            "per piece of evidence: evidence row id, fetched at, time spoken for, source type, "
            "judgment id, weight",
            "json_ids",
            "evidence_pieces",
            note="one JSON list of those six fields: a list property cannot hold null or a map",
        ),
        _col("second judge's judgment ids", "ids", "second_judge_judgment_ids"),
        _col("their correlation", "bool", "correlated"),
        _col("agree", "float", note="computed by the writer"),
        _col("disagree", "float", note="computed by the writer"),
        _col("ratio", "float", note="computed by the writer"),
        _col("band", "enum:verification_band", note="computed by the writer (S47)"),
        _col("load-bearing", "bool", note="as the writer found it"),
        _col("disposition", "enum:disposition"),
        _col("override reason", "value_id"),
        _col("re-check by before and after", "instant", "recheck_by_before", "recheck_by_after"),
        _col("reason", "value_id"),
        _col("request id", "id"),
        _col("commit id", "id"),
    ),
    uniques=_keys("verifications", ("id",)),
    indexes=_ranges("verifications", ("world_id", "claim_id", "at"), ("run_id",)),
    notes="Columns are D4's (line 985); every check is one appended row.",
)

HORIZON_STATS = Table(
    name="horizon_stats",
    label="HorizonStat",
    element="node",
    scope="world",
    group="snapshot",
    owner="D4",
    columns=(
        _WORLD,
        _col("meaning or property name", "id"),
        _col("checks", "int"),
        _col("moves", "int"),
        _col("days at risk", "float"),
        _col("suggested horizon", "int", note="days"),
        _col("updated at", "instant"),
    ),
    uniques=_keys("horizon_stats", ("world_id", "meaning_or_property_name")),
)

IMPACTS = Table(
    name="impacts",
    label="Impact",
    element="node",
    scope="world",
    group="snapshot",
    owner="D6",
    columns=(
        _col("link id", "id", note="the mark's link id"),
        _WORLD,
        _col("trigger change set", "id"),
        _col("rule", "enum:cascade_rule"),
        _col("depth", "int"),
        _col("path", "ids", note="link ids"),
        _col("stakes", "enum:stakes"),
        _col("trigger source type", "enum:source_type"),
        _col("state", "enum:impact_state"),
        _col("proposal id", "id"),
        _col("explanation", "value_id"),
        _col("explained by", "code"),
        _col("resolved by", "code"),
        _col("resolved at", "instant"),
        _col("resolution reason", "value_id"),
        _ruling(
            "affected_item",
            "id",
            "S25: repeats the mark's from item; B.1 names it only in the unique key",
            spec="affected item",
        ),
        _ruling("flag", "enum:impact_flag", "S25, line 1250"),
        *_version_touched(),
    ),
    uniques=_keys(
        "impacts",
        ("link_id",),
        ("world_id", "trigger_change_set", "affected_item"),
    ),
)

CASCADE_PROPOSALS = Table(
    name="cascade_proposals",
    label="CascadeProposal",
    element="node",
    scope="world",
    group="snapshot",
    owner="D6",
    columns=(
        _ID,
        _WORLD,
        _col("rule", "enum:cascade_rule"),
        _col("mark ids", "ids", note="impacts link ids"),
        _col("target item", "id"),
        _col("suggested operations", "json_ids", note="text values as value ids"),
        _col("stakes", "enum:stakes"),
        _col("state", "enum:proposal_state"),
        _col("decided by", "code"),
        _col("decision reason", "value_id"),
        _col("resulting commit", "id"),
        _ruling("proposal_type", "enum:proposal_type", "S25, lines 1252, 1264, 1265"),
        _ruling("detail", "json_ids", "S25: ids, codes and counts"),
        *_version_touched(),
    ),
    uniques=_keys("cascade_proposals", ("id",)),
    indexes=_ranges("cascade_proposals", ("world_id", "state")),
)

BELIEFS = Table(
    name="beliefs",
    label="Belief",
    element="node",
    scope="world",
    group="projection",
    owner="D9",
    columns=(
        _col("item id", "id", note="a belief's id is its item's id (S13)"),
        _WORLD,
        _col("state", "enum:belief_state"),
        _col("state since", "instant"),
        _col("origin", "enum:belief_origin"),
        _col("supporting episodes", "ids", note="distinct episode ids"),
        _col("contradicting episodes", "ids", note="distinct episode ids"),
        _col("last support at", "instant", note="the episode's own time"),
        _col("last contradiction at", "instant", note="the episode's own time"),
        _col("externally testable", "bool"),
        _col("re-test how", "json_ids"),
        _col("held by the person", "instant", note="when the person last decided the state"),
        _col("successor", "id"),
        _col("retrieved", "int"),
        _col("last retrieved", "instant"),
        _col("cited", "int"),
        _ruling("end_class", "enum:belief_end_class", "S25, line 1752"),
        *_version_touched(),
    ),
    uniques=_keys("beliefs", ("item_id",)),
    notes="The statement is the item's title and body. The clocks and the horizon are the item's.",
)

CONTRADICTIONS = Table(
    name="contradictions",
    label="Contradiction",
    element="node",
    scope="world",
    group="snapshot",
    owner="D9",
    columns=(
        _ID,
        _WORLD,
        _col("target", "id"),
        _col("standing side", "id", "standing_target_id"),
        _col("standing side", "int", "standing_change_id", note="a change entry sequence (S10)"),
        _col("fresh side", "id", "fresh_evidence_id", note="S14: absent when the judges disagree"),
        _ruling("fresh_source_id", "id", "S14: what was read when no evidence row exists"),
        _ruling("fresh_locator", "locator", "S14: what was read when no evidence row exists"),
        _col("detected by", "code"),
        _col("detected at", "instant"),
        _col("request id", "id"),
        _col("status", "enum:contradiction_status"),
        _col("resolution", "value_id"),
        _col("resolved at", "instant"),
        _col("resolved by", "code"),
        _col("question id", "id"),
        *_version_touched(),
    ),
    uniques=_keys("contradictions", ("id",)),
    indexes=_ranges("contradictions", ("world_id", "status", "detected_at")),
)

SUPPORT_CHANGED = Table(
    name="support_changed",
    label="SupportChanged",
    element="node",
    scope="world",
    group="snapshot",
    owner="D9",
    columns=(
        _WORLD,
        _col("belief id", "id", note="the belief's item id (S13)"),
        _col("cause change id", "int", note="a change entry sequence (S10)"),
        _col("queued at", "instant"),
        _col("taken at", "instant"),
    ),
    uniques=_keys("support_changed", ("world_id", "belief_id")),
)

QUESTIONS = Table(
    name="questions",
    label="Question",
    element="node",
    scope="world",
    group="snapshot",
    owner="D11",
    columns=(
        _ID,
        _WORLD,
        _col("subject items", "ids"),
        _col("ask", "value_id"),
        _col("class", "enum:question_class"),
        _col("legwork", "ids", "legwork_sources", note="the sources checked"),
        _col("legwork", "value_id", "legwork_reason", note="why none settles it"),
        _col("answer options", "json_ids", note="item ids, fixed codes or value ids"),
        _col("raised by", "code"),
        _col("raised at", "instant"),
        _col("priority", "int"),
        _col("state", "enum:question_state"),
        _col("answer source id", "id", note="an answer is a source row, never text here"),
        _col("applied at", "instant"),
        _col("receipt", "json_ids", note="ids and codes; a title is read fresh when shown"),
        _ruling("unblocks", "ids", "S25, line 3197"),
        _ruling("withdraw_reason", "value_id", "S25, line 2161"),
        *_version_touched(),
    ),
    uniques=_keys("questions", ("id",)),
    indexes=_ranges("questions", ("world_id", "state", "priority")),
)

AREA_AUDITS = Table(
    name="area_audits",
    label="AreaAudit",
    element="node",
    scope="world",
    group="snapshot",
    owner="D11",
    columns=(
        _WORLD_S5,
        _col("run id", "id"),
        _col("area id", "id"),
        _col("started", "instant"),
        _col("finished", "instant"),
        _col("tests run", "enum:growth_test", note="a list of codes"),
        _col("finding ids", "ids"),
        _col("operation ids proposed", "ids"),
        _col("receipts", "json_ids"),
        _col("question ids", "ids"),
    ),
    uniques=_keys("area_audits", ("world_id", "run_id")),
    indexes=_ranges("area_audits", ("area_id",)),
)

WALK_ROTATION = Table(
    name="walk_rotation",
    label="WalkRotation",
    element="node",
    scope="world",
    group="snapshot",
    owner="D12",
    columns=(
        _WORLD,
        _col("area id", "id"),
        _col("last explored", "instant"),
        _col("last finding id", "id"),
    ),
    uniques=_keys("walk_rotation", ("world_id", "area_id")),
)

# Health and privacy.

CHECKS = Table(
    name="checks",
    label="Check",
    element="node",
    scope="world",
    group="snapshot",
    owner="D13",
    columns=(
        _col("id", "id", note="D13 calls it check id; one per check per world"),
        _ruling(
            "world_id", "id", "S35: the real world id; one row per check per world", spec="world_id"
        ),
        _col("the person's question", "config", "question", note="S29, repeated on each row"),
        _col("measure", "config", note="S29, repeated on each row"),
        _col("cadence", "config", note="S29, repeated on each row"),
        _col("bands with the reason for each", "config", "bands", note="S29, repeated on each row"),
        _col("state", "enum:check_state"),
        _col("state", "float", "state_gap", note="the gap of short of target"),
        _col("value", "float"),
        _col("interval", "float", "interval_low", "interval_high", note="95% interval"),
        _col("measured at", "instant"),
        _col("state since", "instant"),
    ),
    uniques=_keys("checks", ("id",)),
    notes=(
        "Columns are D13's (line 2424). A check's state and value measure one world (lines "
        "2398, 3202), so each world has its own row (S35)."
    ),
)

AUDIT_RUNS = Table(
    name="audit_runs",
    label="AuditRun",
    element="node",
    scope="world",
    group="snapshot",
    owner="D13",
    columns=(
        _col("id", "id", note="D13 calls it run id"),
        _WORLD_S5,
        _col("check id", "id"),
        _col("seed", "int"),
        _col("strata and counts", "json_ids"),
        _col(
            "controls planted",
            "int",
            note="S29: a count; the controls stay with the harness",
        ),
        _col("grader model and effort", "code", "grader_model"),
        _col("grader model and effort", "enum:effort", "grader_effort"),
        _col("second grader", "code"),
        _col("cost", "float"),
        _col("status", "enum:audit_run_status"),
        _col("started", "instant"),
        _col("finished", "instant"),
        _ruling("invalid", "bool", "S25, lines 2376, 2450"),
    ),
    uniques=_keys("audit_runs", ("id",)),
    indexes=_ranges("audit_runs", ("check_id", "started")),
    notes="Columns are D13's (line 2425).",
)

AUDIT_GRADES = Table(
    name="audit_grades",
    label="AuditGrade",
    element="node",
    scope="world",
    group="snapshot",
    owner="D13",
    columns=(
        _WORLD_S5,
        _col("run id", "id"),
        _col(
            "subject",
            "ref",
            note="S32: an item, link or question reference, or "
            "`event:<connector id>:<message key>`",
        ),
        _col("stratum", "code"),
        _col("grade", "enum:audit_grade"),
        _col("evidence", "id", "evidence_connector", note="the connector"),
        _col("evidence", "locator", "evidence_locator", note="the locator"),
        _col("evidence", "instant", "evidence_fetched_at", note="fetched at"),
        _col("grader", "code"),
        _col("second grade", "enum:audit_grade"),
        _col("agreement", "bool"),
        _ruling("rationale", "value_id", "S25, line 2527"),
    ),
    uniques=_keys("audit_grades", ("world_id", "run_id", "subject")),
    notes="Columns are D13's (line 2426). Ids and locators only.",
)

FINDINGS = Table(
    name="findings",
    label="Finding",
    element="node",
    scope="world",
    group="snapshot",
    owner="D13",
    columns=(
        _col("id", "id", note="D13 calls it finding id"),
        _WORLD_S5,
        _col("run id", "id"),
        _col("subject", "id"),
        _col("defect class", "code"),
        _col("owner", "code"),
        _col("opened", "instant"),
        _col("due", "instant"),
        _col("status", "enum:finding_status"),
        _col("closing run id", "id"),
        _ruling("declined_reason", "value_id", "S25, line 2427"),
        *_version_touched(),
    ),
    uniques=_keys("findings", ("id",)),
    indexes=_ranges("findings", ("world_id", "status", "due")),
    notes="Columns are D13's (line 2427). Ids only, never the claim's text.",
)

FORGET_LEDGER = Table(
    name="forget_ledger",
    label="ForgetLedger",
    element="node",
    scope="world",
    group="ledger",
    owner="D14",
    columns=(
        _ID,
        _WORLD,
        _col("target type", "enum:forget_target_type"),
        _col("internal ids and locators", "ids", "internal_ids", note="never values or titles"),
        _col("internal ids and locators", "locator", "locators", note="never values or titles"),
        _col("requested by", "code"),
        _col("approved at", "instant"),
        _col("receipt", "json_ids", note="counts per place, never the value"),
        _col("the date after which no backup holds the value", "date", "backups_clear_after"),
    ),
    uniques=_keys("forget_ledger", ("id",)),
    indexes=_ranges("forget_ledger", ("world_id",)),
    notes="The copy kept outside the dump belongs to the forget lane (S23).",
)

OAUTH_CLIENTS = Table(
    name="oauth_clients",
    label="OAuthClient",
    element="node",
    scope="platform",
    group="snapshot",
    owner="A.3, D14",
    columns=(
        _col(
            "client id",
            "locator",
            note="S32: a UUIDv7 from dynamic registration, or the URL of a Client ID Metadata "
            "Document (lines 3036, 3148)",
        ),
        _col("world_id", "id", note="S45: always absent; the oauth_grants row holds it"),
        _ruling("user_id", "id", "S45: always absent; the oauth_grants row holds it"),
        _col(
            "host",
            "config",
            note="the client's self-declared client_name, control characters removed, at most "
            "100 characters; a host's registration metadata (line 2561)",
        ),
        _col("registration type", "enum:registration_type"),
        _col(
            "redirect URIs",
            "config",
            note="a list of URIs; a host's registration metadata (line 2561)",
        ),
        _col("scopes granted", "code", note="S45: always absent; the oauth_grants row holds it"),
        _col("granted at", "instant", note="S45: always absent; the oauth_grants row holds it"),
        _col("revoked at", "instant", note="S45: always absent; the oauth_grants row holds it"),
        _ruling(
            "token_endpoint_auth_method",
            "code",
            "none, client_secret_post or client_secret_basic; a metadata document client is none",
        ),
        _ruling(
            "client_secret_hash",
            "code",
            "the sha256 hex of the client secret; absent for a public client (S45)",
        ),
        _ruling("registered_at", "instant", "when the row was made"),
        _ruling("fetched_at", "instant", "a metadata document client's last fetch"),
    ),
    uniques=_keys("oauth_clients", ("client_id",)),
    notes=(
        "A B.1 table read through the platform query layer: a client exists before any world "
        "consents to it. It restores with the snapshot tables (S21). A person's consent is an "
        "oauth_grants row (S45). Only server/grove/writer/oauth.py writes it."
    ),
)

# Platform rows beyond B.1. Each owner lane adds the columns it needs by a new migration and row.

_PLATFORM_OWNER = "platform"


def _platform(
    name: str,
    label: str,
    owner: str,
    columns: tuple[Column, ...],
    uniques: tuple[Constraint, ...],
    indexes: tuple[Index, ...] = (),
    notes: str = "",
) -> Table:
    return Table(
        name, label, "node", "platform", "platform", owner, columns, uniques, indexes, notes
    )


def _p(prop: str, type_: str, note: str = "") -> Column:
    return _convention(prop, type_, note)


WORLDS = _platform(
    "worlds",
    "World",
    f"{_PLATFORM_OWNER}; signin",
    (
        _p("id", "id", "the world_id of every row in the world"),
        _p("owner_user_id", "id"),
        _p("created_at", "instant"),
        _p("next_sequence", "int", "S10: starts at 0; advanced by one direct SET"),
        _p("seed_version", "code"),
        _p(
            "self_item_id",
            "id",
            "S40: the person's own item, set in the transaction that creates it; absent before",
        ),
    ),
    _keys("worlds", ("id",)),
    _ranges("worlds", ("owner_user_id",)),
    "The signin lane owns any further column.",
)

USERS = _platform(
    "users",
    "User",
    f"{_PLATFORM_OWNER}; signin",
    (
        _p("id", "id"),
        _p(
            "email_hash",
            "code",
            "S40: sha256 of the salted address; secrets and contact details stay out of the row",
        ),
        _p("created_at", "instant"),
    ),
    _keys("users", ("id",), ("email_hash",)),
    notes="The signin lane owns any further column.",
)

SESSIONS = _platform(
    "sessions",
    "Session",
    f"{_PLATFORM_OWNER}; signin",
    (
        _p("id", "id"),
        _p("user_id", "id"),
        _p("token_hash", "code"),
        _p("method", "code", "S40: google or email; a planted test session has none"),
        _p("created_at", "instant"),
        _p("expires_at", "instant"),
        _p("revoked_at", "instant"),
    ),
    _keys("sessions", ("id",), ("token_hash",)),
    _ranges("sessions", ("user_id",)),
    "The signin lane owns any further column.",
)

OAUTH_TOKENS = _platform(
    "oauth_tokens",
    "OAuthToken",
    f"{_PLATFORM_OWNER}; hosts-a",
    (
        _p("id", "id"),
        _p("client_id", "locator", "an oauth_clients client id (S32)"),
        _p("user_id", "id"),
        _p("world_id", "id", "S5: tokens are bound to one world (line 3038), the grant's world"),
        _p("token_hash", "code", "S45: the sha256 hex of the token; the token itself is not kept"),
        _p("token_type", "code", "access or refresh"),
        _p("scopes", "code", "a list of scope codes"),
        _p("created_at", "instant"),
        _p("expires_at", "instant"),
        _p("revoked_at", "instant"),
        _p(
            "family_id",
            "id",
            "the id of the code the chain began with; every refresh of that chain keeps it",
        ),
        _p("grant_id", "id", "an oauth_grants id"),
        _p("resource", "config", "the canonical <base>/mcp"),
        _p("used_at", "instant", "a refresh token that was rotated"),
    ),
    _keys("oauth_tokens", ("id",), ("token_hash",)),
    _ranges("oauth_tokens", ("client_id",), ("user_id",), ("family_id",)),
    "The hosts-a lane owns any further column. Only server/grove/writer/oauth.py writes it.",
)

AUTHORIZATION_CODES = _platform(
    "authorization_codes",
    "AuthCode",
    f"{_PLATFORM_OWNER}; hosts-a",
    (
        _p("id", "id"),
        _p("client_id", "locator", "an oauth_clients client id (S32)"),
        _p("user_id", "id"),
        _p("world_id", "id"),
        _p("code_hash", "code", "S45: the sha256 hex of the code; the code itself is not kept"),
        _p("code_challenge", "code", "PKCE S256"),
        _p("redirect_uri", "config"),
        _p("scopes", "code", "a list of scope codes"),
        _p("created_at", "instant"),
        _p("expires_at", "instant"),
        _p("used_at", "instant"),
        _p("grant_id", "id", "an oauth_grants id"),
        _p("resource", "config", "the canonical <base>/mcp; absent when the request named none"),
        _p("redirect_given", "bool", "the authorize request named redirect_uri"),
        _p(
            "reused_at",
            "instant",
            "S45: the first redeem after the code was used; its family gets no more tokens",
        ),
    ),
    _keys("authorization_codes", ("id",), ("code_hash",)),
    notes="The hosts-a lane owns any further column. Only server/grove/writer/oauth.py writes it.",
)

EMAIL_CODES = _platform(
    "email_codes",
    "EmailCode",
    f"{_PLATFORM_OWNER}; signin",
    (
        _p("id", "id"),
        _p("email_hash", "code"),
        _p("code_hash", "code"),
        _p("attempts", "int"),
        _p("created_at", "instant"),
        _p("expires_at", "instant"),
        _p("used_at", "instant"),
    ),
    _keys("email_codes", ("id",)),
    _ranges("email_codes", ("email_hash",)),
    "The signin lane owns any further column.",
)

UPLOAD_TOKENS = _platform(
    "upload_tokens",
    "UploadToken",
    f"{_PLATFORM_OWNER}; ingest-a",
    (
        _p("id", "id"),
        _p("user_id", "id"),
        _p("world_id", "id", "an upload token serves one world"),
        _p("token_hash", "code"),
        _p("created_at", "instant"),
        _p("expires_at", "instant"),
        _p("revoked_at", "instant"),
    ),
    _keys("upload_tokens", ("id",), ("token_hash",)),
    notes="The ingest-a lane owns any further column.",
)

MIGRATIONS = _platform(
    "migrations",
    "Migration",
    f"{_PLATFORM_OWNER}; foundation",
    (
        _p("name", "code", "scope/file"),
        _p("scope", "code", "the default scope is grove"),
        _p("checksum", "code"),
        _p("applied_at", "instant"),
    ),
    _keys("migrations", ("name",)),
)

# Tables beyond B.1 that a later lane adds for its own feature. Each has its own migration.

MAP_POSITIONS = Table(
    name="map_positions",
    label="MapPosition",
    element="node",
    scope="world",
    group="snapshot",
    owner="views-a",
    columns=(
        _convention("id", "id", "UUIDv7 text from new_id() (S4)"),
        _convention("world_id", "id", "the world of the item"),
        _convention("item_id", "id", "an item id of the same world"),
        _convention("x", "float", "the map's own units; finite, at most 1e7 either way"),
        _convention("y", "float", "the map's own units; finite, at most 1e7 either way"),
        _convention(
            "placed_at", "instant", "from the clock provider, when the map first placed it"
        ),
    ),
    uniques=_keys("map_positions", ("id",), ("world_id", "item_id")),
    notes=(
        "Where the map drew an item first. Insert-only: a position is never moved, so an item "
        "keeps its place after a reload and after new items arrive. Only "
        "server/grove/writer/positions.py writes it. One row per item: the (world_id, item_id) "
        "key turns a second placement that races the first into a kept one. A view preference, "
        "not a claim about the world, so it has no history and restores with the snapshot."
    ),
)

OAUTH_GRANTS = Table(
    name="oauth_grants",
    label="OAuthGrant",
    element="node",
    scope="platform",
    group="platform",
    owner="hosts-a",
    columns=(
        _convention("id", "id", "UUIDv7 text from new_id() (S4)"),
        _convention("client_id", "locator", "an oauth_clients client id (S32)"),
        _convention("world_id", "id", "the world the person let the host reach"),
        _convention("user_id", "id", "the person who consented last"),
        _convention("scopes_granted", "code", "a list of scope codes"),
        _convention("granted_at", "instant", "the last consent"),
        _convention("revoked_at", "instant", "absent while the grant is live"),
    ),
    uniques=_keys("oauth_grants", ("id",), ("client_id", "world_id")),
    indexes=_ranges("oauth_grants", ("world_id",)),
    notes=(
        "S45: a person's consent to a host, one row per (client, world). A new consent to the "
        "same host from the same world finds the row, sets its scopes and time and clears "
        "revoked_at. A revoke sets revoked_at and revokes every token of the grant in the same "
        "transaction. Only server/grove/writer/oauth.py writes it. A platform row: it restores "
        "as it was (S21)."
    ),
)

LATER_TABLES: tuple[Table, ...] = (MAP_POSITIONS, OAUTH_GRANTS)

# B.1's five groups, in B.1 order.
B1_GROUPS: tuple[tuple[str, tuple[Table, ...]], ...] = (
    (
        "The graph and its registry",
        (
            ITEMS,
            LINKS,
            KINDS,
            MEANINGS,
            PROPERTY_NAMES,
            ALIASES,
            ONTOLOGY_CHANGE_LOG,
            DISTINCT_PAIRS,
            AREAS,
        ),
    ),
    (
        "Sources and evidence",
        (
            CONNECTORS,
            CAPABILITIES,
            SOURCES,
            EPISODES,
            EVIDENCE,
            HOLDS,
            UNRESOLVED_MENTIONS,
            SCREENING_RECEIPTS,
        ),
    ),
    (
        "History and requests",
        (
            CHANGE_ENTRIES,
            HISTORY_VALUES,
            REQUESTS,
            DECLINES,
            JOBS,
            RUN_RECORDS,
            MODEL_CALLS,
            JUDGMENTS,
            BUDGETS,
        ),
    ),
    (
        "Truth, consequence and belief",
        (
            VERIFICATIONS,
            HORIZON_STATS,
            IMPACTS,
            CASCADE_PROPOSALS,
            BELIEFS,
            CONTRADICTIONS,
            SUPPORT_CHANGED,
            QUESTIONS,
            AREA_AUDITS,
            WALK_ROTATION,
        ),
    ),
    (
        "Health and privacy",
        (
            CHECKS,
            AUDIT_RUNS,
            AUDIT_GRADES,
            FINDINGS,
            FORGET_LEDGER,
            OAUTH_CLIENTS,
        ),
    ),
)

PLATFORM_TABLES: tuple[Table, ...] = (
    WORLDS,
    USERS,
    SESSIONS,
    OAUTH_TOKENS,
    AUTHORIZATION_CODES,
    EMAIL_CODES,
    UPLOAD_TOKENS,
    MIGRATIONS,
)

_B1: tuple[Table, ...] = tuple(t for _, group in B1_GROUPS for t in group)

TABLES: tuple[Table, ...] = _B1 + PLATFORM_TABLES + LATER_TABLES
B1_TABLES: tuple[str, ...] = tuple(t.name for t in _B1)

# S17. Line 2560 names the text properties of grove_value_index.
FULLTEXT_INDEXES: tuple[FulltextIndex, ...] = (
    FulltextIndex(
        "grove_items_lexical",
        ("Item",),
        ("world_id", "title", "summary", "body", "property_values", "search_terms"),
    ),
    FulltextIndex(
        "grove_aliases_lexical",
        ("Alias",),
        ("world_id", "phrase", "normalised_phrase"),
    ),
    FulltextIndex("grove_past_values", ("PastValue",), ("world_id", "value")),
    FulltextIndex(
        "grove_value_index",
        ("Item", "Alias", "HistoryValue", "Kind", "Meaning", "PropertyName", "RunRecord"),
        (
            "world_id",
            "title",
            "summary",
            "body",
            "property_values",
            "search_terms",
            "phrase",
            "normalised_phrase",
            "value",
            "name",
            "proposed_operations",
        ),
    ),
)

WORLD_LABELS: frozenset[str] = frozenset(
    {t.label for t in TABLES if t.scope == "world" and t.element == "node"} | {"PastValue"}
)
PLATFORM_LABELS: frozenset[str] = frozenset(
    t.label for t in TABLES if t.scope == "platform" and t.element == "node"
)
RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    t.label for t in TABLES if t.element == "relationship"
)

_BY_NAME = {t.name: t for t in TABLES}


def table(name: str) -> Table:
    return _BY_NAME[name]
