"""D8's floor in the writer: a proposed word becomes a provisional word or an alias, never on a
caller's say-so, and a provisional word earns its place by distinct episodes.

Only this module and worlds.py write the registry labels (the writer lint holds it). The batch
loop calls the two handlers below like any other operation; `finish` runs at the end of the batch.

- `propose_word` (U C K): reuse first (a name that resolves is refused), the declared fields of the
  word type, the two nearest neighbours with what each cannot say, a worked example, and the two
  recorded same-word judgments of two models. Both judges saying "same" make the phrase an alias
  of the nearest neighbour; anything else mints the word provisional, because a provisional word
  is visible, dated and easy to merge, and a wrong alias changes every link written with it.
  A field-fit judgment that disputes a declared field, or no judgment of it, gives the safer value.
- `promote_word` (K P): K under the earning bar, P at once (the person's deliberate use).
- `finish`: the worked examples, then the usage counts of every word a new evidence row of this
  batch reached, and the promotion of a provisional word at the earning bar, in the same
  transaction.

Every mint, alias and promotion writes one :OntologyChange row with its inverse, and a change
entry of entity type registry_row, which the receipt prints with the symbol `o`. Nothing here
names a registry word: code reads declared fields and ids only.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from grove.store import catalogue
from grove.store.enums import ENUMS
from grove.store.normalise import normalise_phrase, reads_blank
from grove.store.primitives import (
    PRIMITIVE_IDS,
    PRIMITIVE_KIND_BELIEF,
    PRIMITIVE_MEANINGS,
)
from grove.writer.errors import Refused
from grove.writer.history import compact, row_json
from grove.writer.resolver import NONE, RESOLVED, resolve_word, usable

if TYPE_CHECKING:
    from grove.writer.registry import Registry

# Appendix C: the earning bar and the mint valve.
EARNING_BAR = 3
MINT_VALVE = 3
# O5: the horizon of a word whose temporal class falls to state with no horizon declared. The
# shortest starter horizon, so the safer value is checked soonest.
SAFER_HORIZON_DAYS = 30
MAX_HORIZON_DAYS = 3650

WORD_TYPES = ("kind", "meaning", "property_name")
LABELS = {"kind": "Kind", "meaning": "Meaning", "property_name": "PropertyName"}
TABLES = {
    "kind": "kinds",
    "meaning": "meanings",
    "property_name": "property_names",
    "alias": "aliases",
}
# Line 646: the status roles each lifecycle class fixes. A record and a bounded period have none.
CLASS_ROLES: dict[str, tuple[str, ...]] = {
    "work_with_state": ("open", "active", "blocked", "reported_done", "done", "cancelled"),
    "lasting_thing": ("active", "ended"),
    "record": (),
    "bounded_period": (),
}
# O13: line 3241 directs every meaning from its first endpoint to its second; a reversed phrasing
# is a reversed alias, never a second direction.
DIRECTIONS = ENUMS["direction"]
# O13: the value types of a property name. The spec gives no list.
VALUE_TYPES = ENUMS["value_type"]

SAME_WORD = "same_word"
FIELD_FIT = "field_fit"
SAME = "same"
DIFFERENT = "different"
FITS = "fits"
DOES_NOT_FIT = "does_not_fit"
OUTCOME_MINT = "mint"
OUTCOME_ALIAS = "alias"

_PROVISIONAL = "provisional"
_CANONICAL = "canonical"
_RETIRED = "retired"
_MINTED = "minted"
_ACTOR_WRITER = "writer"
_ENTITY = "registry_row"
_CODE = r"[a-z0-9_]{1,40}"

# What a caller may declare per word type; required ones are checked in _declared_*.
_DECLARED_KEYS = {
    "kind": frozenset({"lifecycle_class", "status_vocabulary"}),
    "meaning": frozenset(
        {
            "from_kinds",
            "to_kinds",
            "direction",
            "cardinality",
            "cardinality_group",
            "carries_consequences",
            "downstream_end",
            "decomposition",
            "rolls_up_completion",
            "temporal_class",
            "default_horizon",
        }
    ),
    "property_name": frozenset({"value_type", "temporal_class", "default_horizon"}),
}
# The fields the writer sets, never a caller (the Oct 2 drill): status, origin and the counts.
WRITER_SET = frozenset(
    {
        "id",
        "world_id",
        "name",
        "description",
        "reason",
        "status",
        "origin",
        "primitive",
        "generic",
        "uses",
        "distinct_episodes",
        "first_used",
        "last_used",
        "minted_at",
        "minted_by",
        "version",
        "touched_at",
        "worked_example",
        "nearest_neighbours_at_mint",
        "disputed_fields",
        "earning_episodes",
        "required_fields",
        "required_links",
        "propagating_fields",
        "scheduled_role",
    }
)

_ADD = {
    "kind": "CREATE (n:Kind) SET n += $row",
    "meaning": "CREATE (n:Meaning) SET n += $row",
    "property_name": "CREATE (n:PropertyName) SET n += $row",
    "alias": "CREATE (n:Alias) SET n += $row",
}
_SET = {
    "kind": "MATCH (n:Kind {id: $id}) SET n += $columns RETURN n",
    "meaning": "MATCH (n:Meaning {id: $id}) SET n += $columns RETURN n",
    "property_name": "MATCH (n:PropertyName {id: $id}) SET n += $columns RETURN n",
}
_ADD_CHANGE = "CREATE (c:OntologyChange) SET c += $row"
_SET_CHANGE = "MATCH (c:OntologyChange {id: $id}) SET c += $columns"
_ADD_CAPABILITY = "CREATE (c:Capability) SET c += $row"
_ANY_NAME = {
    "kind": "MATCH (n:Kind) RETURN n.id AS id, n.name AS name, n.status AS status",
    "meaning": "MATCH (n:Meaning) RETURN n.id AS id, n.name AS name, n.status AS status",
    "property_name": (
        "MATCH (n:PropertyName {kind: $kind}) RETURN n.id AS id, n.name AS name, n.status AS status"
    ),
}
_JUDGMENTS = (
    "MATCH (j:Judgment) WHERE j.id IN $ids "
    "OPTIONAL MATCH (c:ModelCall {id: j.model_call_id}) "
    "RETURN j.id AS id, j.verdict AS verdict, j.role AS role, j.subject AS subject, "
    "j.correlated AS correlated, j.model_call_id AS model_call_id, c.model AS model"
)
_LINK_WORDS = (
    "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.id IN $ids RETURN l.id AS id, l.meaning AS meaning"
)
_ITEM_WORDS = (
    "MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id, i.kind AS kind, "
    "i.properties AS properties"
)
# Ended rows count too: a row that a correction ended still shows that its episode spoke for the
# word, so ending a row and attaching another from the same conversation earns nothing (O8).
_EPISODE_ROWS = (
    "MATCH (e:Evidence {episode_id: $episode, polarity: 'supports'}) "
    "WHERE NOT e.id IN $fresh RETURN e.target_type AS target_type, e.target_id AS target_id"
)
_CREATED = (
    "MATCH (e:ChangeEntry {change_set_id: $change_set, field: 'row'}) "
    "WHERE e.entity_type IN ['item', 'link'] RETURN e.entity_type AS type, e.entity_id AS id"
)


# Judgment subjects ---------------------------------------------------------------------------


def pair_subject(first: str, second: str) -> str:
    """S32: a pair is the two references in sorted order, joined by `|`."""
    return "|".join(sorted((first, second)))


def same_word_value(word_type: str, name: str, reversed_: bool = False) -> dict[str, Any]:
    """What the history value of a same-word subject holds (O3, O15): the proposed name, its word
    type, and whether it reads its nearest neighbour from the other end. A verdict on one reading
    never counts for the other."""
    return {"name": name, "reversed": reversed_, "word_type": word_type}


def same_word_subject(value_ref: str, word_type: str, neighbour_id: str) -> str:
    """The subject of a same-word judgment (O3): the proposed word, which has no row yet and so is
    `value:<history value id>` holding its name (S32), paired with its nearest neighbour."""
    return pair_subject(value_ref, f"{word_type}:{neighbour_id}")


def field_fit_value(
    word_type: str, name: str, field_name: str, value: Any, kind_id: str | None = None
) -> dict[str, Any]:
    """What the history value of a field-fit subject holds (O3): the word, the kind of a property
    name, the field and the value declared for it, so a verdict on one declared value, or on a
    property name of another kind, never counts."""
    return {
        "field": field_name,
        "kind": kind_id,
        "name": name,
        "value": value,
        "word_type": word_type,
    }


# The pure checks: the writer and the planner share them -------------------------------------


@dataclass(frozen=True)
class Neighbour:
    row: dict[str, Any]
    distinction: str


@dataclass(frozen=True)
class CheckedProposal:
    """A proposal that passed every check that needs no judgment and no store write.
    `columns` holds the declared columns as the row stores them."""

    word_type: str
    name: str
    description: str
    reason: str
    kind_id: str | None
    neighbours: tuple[Neighbour, ...]
    columns: dict[str, Any]
    disputable: tuple[str, ...]
    capability: dict[str, str] | None
    # The declared value of each disputable field, as a field-fit history value holds it.
    field_values: dict[str, Any] = field(default_factory=dict)
    # O15: the phrase reads its nearest neighbour from the other end (meanings only).
    reversed: bool = False


MAX_NAME = 120
MAX_TEXT = 2000


def _short(text: str, name: str, limit: int = MAX_TEXT) -> str:
    if len(text) > limit:
        raise _bad(
            "too_long", f"{name} is at most {limit} characters", {"field": name, "limit": limit}
        )
    return text


def _bad(rule: str, message: str, details: dict[str, Any] | None = None) -> Refused:
    return Refused("invalid_arguments", rule, message, details or {})


def _text(value: Any, name: str, rule: str = "type") -> str:
    if not isinstance(value, str) or reads_blank(value):
        raise _bad(rule, f"{name} must be non-empty text", {"field": name})
    return value


def _enum(value: Any, name: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise _bad(
            "enum",
            f"{name} must be one of {list(allowed)}",
            {"field": name, "valid": list(allowed)},
        )
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _bad("type", f"{name} is true or false", {"field": name})
    return value


def _horizon(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_HORIZON_DAYS:
        raise _bad(
            "type",
            f"default_horizon is whole days from 1 to {MAX_HORIZON_DAYS}",
            {"field": "default_horizon"},
        )
    return value


def _primitive_names() -> set[str]:
    names = {normalise_phrase(str(row["name"])) for row in PRIMITIVE_MEANINGS.values()}
    names.add(normalise_phrase(str(PRIMITIVE_KIND_BELIEF["name"])))
    return names


def names_primitive(phrase: str) -> bool:
    """A phrase that is a primitive name, or one after a leading "is" or "are": the resolver drops
    the copula (S44), so "is superseded by" must not become a world word beside the primitive."""
    normalised = normalise_phrase(phrase)
    names = _primitive_names()
    first, _, rest = normalised.partition(" ")
    return normalised in names or (first in ("is", "are") and rest in names)


def _check_keys(word_type: str, declared: Any) -> dict[str, Any]:
    if not isinstance(declared, dict):
        raise _bad(
            "type", "declared is an object of the word's declared fields", {"field": "declared"}
        )
    for key in declared:
        if key in WRITER_SET:
            raise _bad("writer_field", f"a caller may not declare {key!r}", {"field": key})
        if key not in _DECLARED_KEYS[word_type]:
            raise _bad(
                "unknown_field",
                f"a {word_type} declares no {key!r}",
                {"field": key, "valid": sorted(_DECLARED_KEYS[word_type])},
            )
    return declared


def _vocabulary(lifecycle_class: str, entries: Any) -> list[dict[str, str]]:
    """Every status maps to exactly one role of its class (line 646); the writer refuses an
    unmapped status, and a work kind with no status could hold no item."""
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise Refused(
            "unmapped_status", "type", "status_vocabulary is a list of {status, role}", {}
        )
    roles = CLASS_ROLES[lifecycle_class]
    seen: set[str] = set()
    vocabulary = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"status", "role"}:
            raise Refused("unmapped_status", "type", "a status entry is {status, role}", {})
        status = entry.get("status")
        if not isinstance(status, str) or reads_blank(status) or not normalise_phrase(status):
            raise Refused("unmapped_status", "status_text", "a status is non-empty text", {})
        if entry.get("role") not in roles:
            raise Refused(
                "unmapped_status",
                "unmapped_status",
                f"status {status!r} maps to no role of the class {lifecycle_class}",
                {"status": status, "valid_roles": list(roles)},
            )
        key = normalise_phrase(status)
        if key in seen:
            raise Refused(
                "unmapped_status", "status_named_twice", f"status {status!r} is listed twice", {}
            )
        seen.add(key)
        vocabulary.append({"status": status, "role": entry["role"]})
    if lifecycle_class == "work_with_state" and not vocabulary:
        raise Refused(
            "unmapped_status",
            "no_vocabulary",
            "a kind of work with a state lists at least one status",
            {"valid_roles": list(roles)},
        )
    return vocabulary


def _endpoint(registry: "Registry", value: Any, name: str) -> str:
    """Any kind, or kind ids (a name or an alias resolves) and lifecycle classes, in the seed's
    JSON form. A kind minted earlier in this batch resolves too."""
    if not isinstance(value, dict):
        raise _bad(
            "type",
            f"{name} is {{'any': true}} or {{'kinds': [...], 'classes': [...]}}",
            {"field": name},
        )
    if value.get("any") is True and set(value) == {"any"}:
        return json.dumps({"any": True})
    if set(value) - {"kinds", "classes"}:
        raise _bad("unknown_field", f"{name} holds kinds and classes only", {"field": name})
    kinds, classes = value.get("kinds") or [], value.get("classes") or []
    if not isinstance(kinds, list) or not isinstance(classes, list) or not (kinds or classes):
        raise _bad("type", f"{name} names at least one kind or class", {"field": name})
    ids = []
    for ref in kinds:
        kind = registry.resolve_kind(_text(ref, f"{name}.kinds"))
        if kind["id"] not in ids:
            ids.append(kind["id"])
    for lifecycle_class in classes:
        _enum(lifecycle_class, f"{name}.classes", ENUMS["lifecycle_class"])
    return json.dumps({"kinds": ids, "classes": list(dict.fromkeys(classes))})


def _temporal(declared: dict[str, Any]) -> dict[str, Any]:
    temporal = _enum(declared.get("temporal_class"), "temporal_class", ENUMS["temporal_class"])
    horizon = declared.get("default_horizon")
    if temporal == "state":
        if horizon is None:
            raise _bad(
                "missing_field",
                "a state word declares its default horizon (D4)",
                {"field": "default_horizon"},
            )
        return {"temporal_class": temporal, "default_horizon": _horizon(horizon)}
    if horizon is not None:
        raise _bad(
            "type",
            "only a state word has a default horizon",
            {"field": "default_horizon", "temporal_class": temporal},
        )
    return {"temporal_class": temporal}


def _declared_kind(declared: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    lifecycle_class = _enum(
        declared.get("lifecycle_class"), "lifecycle_class", ENUMS["lifecycle_class"]
    )
    vocabulary = _vocabulary(lifecycle_class, declared.get("status_vocabulary"))
    columns = {
        "lifecycle_class": lifecycle_class,
        "status_vocabulary": json.dumps(vocabulary, ensure_ascii=False),
        "required_fields": "[]",
        "required_links": "[]",
        "propagating_fields": "[]",
    }
    return columns, ("status_vocabulary",) if vocabulary else ()


def _declared_meaning(
    registry: "Registry", declared: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    for key in ("from_kinds", "to_kinds", "direction", "cardinality", "carries_consequences"):
        if key not in declared:
            raise _bad("missing_field", f"a meaning declares {key}", {"field": key})
    consequences = _bool(declared["carries_consequences"], "carries_consequences")
    columns: dict[str, Any] = {
        "from_kinds": _endpoint(registry, declared["from_kinds"], "from_kinds"),
        "to_kinds": _endpoint(registry, declared["to_kinds"], "to_kinds"),
        "direction": _enum(declared["direction"], "direction", DIRECTIONS),
        "cardinality": _enum(declared["cardinality"], "cardinality", ENUMS["cardinality"]),
        "carries_consequences": consequences,
        "decomposition": _bool(declared.get("decomposition", False), "decomposition"),
        "rolls_up_completion": _bool(
            declared.get("rolls_up_completion", False), "rolls_up_completion"
        ),
        "generic": False,
        **_temporal(declared),
    }
    if consequences:
        columns["downstream_end"] = _enum(
            declared.get("downstream_end"), "downstream_end", ENUMS["downstream_end"]
        )
    elif declared.get("downstream_end") is not None:
        raise _bad(
            "type",
            "a meaning with no consequences has no downstream end",
            {"field": "downstream_end"},
        )
    group = declared.get("cardinality_group")
    if group is not None:
        if not isinstance(group, str) or re.fullmatch(_CODE, group) is None:
            raise _bad("type", "cardinality_group is a short code", {"field": "cardinality_group"})
        columns["cardinality_group"] = group
    disputable = []
    if columns["temporal_class"] != "state":
        disputable.append("temporal_class")
    if consequences:
        disputable.append("carries_consequences")
    return columns, tuple(disputable)


def _declared_property(declared: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    columns = {
        "value_type": _enum(declared.get("value_type"), "value_type", VALUE_TYPES),
        **_temporal(declared),
    }
    return columns, ("temporal_class",) if columns["temporal_class"] != "state" else ()


def _capability(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"connector_type", "capability"}:
        raise _bad(
            "type",
            "proposed_capability is {connector_type, capability}",
            {"field": "proposed_capability"},
        )
    return {
        "connector_type": _enum(value["connector_type"], "connector_type", ENUMS["connector_type"]),
        "capability": _enum(value["capability"], "capability", ENUMS["capability"]),
    }


def _words_of(registry: "Registry", word_type: str, kind_id: str | None) -> list[dict[str, Any]]:
    if word_type == "kind":
        return [row for row in registry.kinds.values() if usable(row)]
    if word_type == "meaning":
        return [row for row in registry.meanings.values() if usable(row)]
    return [row for row in registry.property_names if row.get("kind") == kind_id and usable(row)]


def _neighbours(
    registry: "Registry",
    word_type: str,
    value: Any,
    kind_id: str | None,
    fresh: frozenset[str] = frozenset(),
) -> tuple[Neighbour, ...]:
    """The two closest existing words, each with what it cannot say (line 1594). A registry with
    fewer than two words of the type asks for as many as it holds. A word minted earlier in the
    same batch (`fresh`) is no neighbour: no judge saw it, and the planner's registry, read before
    the batch, never holds it, so the planner and the writer ask for the same count."""
    words = [row for row in _words_of(registry, word_type, kind_id) if row["id"] not in fresh]
    wanted = min(2, len(words))
    if not isinstance(value, list) or len(value) != wanted:
        raise _bad(
            "neighbours_required",
            f"a proposal names its {wanted} nearest existing words, each with a distinction",
            {"field": "neighbours", "count": wanted},
        )
    found: list[Neighbour] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"word", "distinction"}:
            raise _bad(
                "neighbours_required", "a neighbour is {word, distinction}", {"field": "neighbours"}
            )
        ref = _text(entry["word"], "neighbours.word")
        distinction = _short(
            _text(entry["distinction"], "neighbours.distinction", rule="distinction_required"),
            "neighbours.distinction",
        )
        if ref in PRIMITIVE_IDS or names_primitive(ref):
            raise _bad("neighbour_unknown", "a primitive word is never a neighbour", {"word": ref})
        resolved = resolve_word(
            registry, word_type, ref, kind_id=kind_id if word_type == "property_name" else None
        )
        if (
            resolved.outcome != RESOLVED
            or resolved.target is None
            or resolved.target["id"] in fresh
        ):
            raise _bad(
                "neighbour_unknown",
                f"neighbour {ref!r} is not one existing {word_type}",
                {"word": ref, "candidates": list(resolved.candidates)},
            )
        if any(seen.row["id"] == resolved.target["id"] for seen in found):
            raise _bad(
                "neighbour_twice", "the two neighbours are two different words", {"word": ref}
            )
        found.append(Neighbour(resolved.target, distinction))
    return tuple(found)


def check_name(registry: "Registry", word_type: str, name: str, kind_id: str | None = None) -> str:
    """Reuse first: a name that reaches an existing word, by name, normal form or alias, or that a
    retired or split word holds, is refused; the caller writes with the existing word."""
    name = _text(name, "name")
    normalised = normalise_phrase(name)
    if not normalised:
        raise _bad(
            "empty_phrase",
            "the name is empty once case, spacing and punctuation fall away",
            {"field": "name"},
        )
    if names_primitive(name):
        raise _bad("primitive_word", "a primitive word is fixed in code", {"field": "name"})
    resolved = resolve_word(
        registry, word_type, name, kind_id=kind_id if word_type == "property_name" else None
    )
    if resolved.outcome != NONE:
        existing = resolved.target["name"] if resolved.target is not None else None
        raise _bad(
            "word_exists",
            f"{name!r} already names a {word_type}: write with the existing word",
            {"existing": existing, "candidates": list(resolved.candidates)},
        )
    rows = (
        registry.kinds.values()
        if word_type == "kind"
        else registry.meanings.values()
        if word_type == "meaning"
        else [row for row in registry.property_names if row.get("kind") == kind_id]
    )
    for row in rows:
        if normalise_phrase(row.get("name") or "") == normalised:
            raise _bad(
                "word_exists",
                f"{name!r} is held by a {row.get('status')} {word_type}",
                {"status": row.get("status")},
            )
    return name


def check_proposal(
    registry: "Registry", proposal: dict[str, Any], *, fresh: frozenset[str] = frozenset()
) -> CheckedProposal:
    """Every check of a proposal that needs no judgment: the name (reuse first), the declared
    fields, the neighbours and the texts. Raises Refused. The planner calls this before it spends
    a judge call, so a proposal the writer would refuse rides on the generic meaning instead.
    `fresh` holds the ids of the words the batch minted before this proposal."""
    word_type = _enum(proposal.get("word_type"), "word_type", WORD_TYPES)
    kind_id = None
    if word_type == "property_name":
        kind = registry.resolve_kind(_text(proposal.get("kind"), "kind"))
        kind_id = kind["id"]
    # Reuse first, before any other refusal: a proposal of an existing word is word_exists, so
    # the plan writes with the word and never sends its phrase to the generic meaning.
    name = check_name(registry, word_type, proposal.get("name"), kind_id)
    if word_type != "property_name" and "kind" in proposal:
        # The operation's shape check refuses any kind key on another word type, null too.
        raise _bad("unknown_field", "only a property name names its kind", {"field": "kind"})
    name = _short(name, "name", MAX_NAME)
    description = _short(_text(proposal.get("description"), "description"), "description")
    reason = _short(_text(proposal.get("reason"), "reason"), "reason")
    reversed_ = proposal.get("reversed", False)
    if not isinstance(reversed_, bool) or (reversed_ and word_type != "meaning"):
        raise _bad(
            "type",
            "reversed is true or false, and only a meaning reads from the other end",
            {"field": "reversed"},
        )
    declared = _check_keys(word_type, proposal.get("declared"))
    if word_type == "kind":
        columns, disputable = _declared_kind(declared)
    elif word_type == "meaning":
        columns, disputable = _declared_meaning(registry, declared)
    else:
        columns, disputable = _declared_property(declared)
        columns["kind"] = kind_id
        _usable_on_kind(registry, word_type, kind_id, columns)
    capability = _capability(proposal.get("proposed_capability"))
    if capability is not None:
        disputable = (*disputable, "proposed_capability")
    neighbours = _neighbours(registry, word_type, proposal.get("neighbours"), kind_id, fresh)
    values = {
        "temporal_class": columns.get("temporal_class"),
        "carries_consequences": columns.get("carries_consequences"),
        "status_vocabulary": json.loads(columns.get("status_vocabulary") or "[]"),
        "proposed_capability": capability,
    }
    return CheckedProposal(
        word_type,
        name,
        description,
        reason,
        kind_id,
        neighbours,
        columns,
        disputable,
        capability,
        {name_: values[name_] for name_ in disputable},
        reversed_,
    )


def _usable_on_kind(
    registry: "Registry", word_type: str, kind_id: str | None, columns: dict[str, Any]
) -> None:
    """A state property sits only on a record (D1, D4). A property name of state on any other kind
    could sit on no item of its kind, so it is refused, as declared or as settled by a dispute."""
    if word_type != "property_name" or columns.get("temporal_class") != "state":
        return
    kind = registry.kind(str(kind_id))
    if kind.get("lifecycle_class") != "record":
        raise Refused(
            "state_property_on_item",
            "state_property_on_item",
            "a state property sits only on a record: write a link of a state meaning or a "
            "measured-figure record instead",
            {"kind": kind_id, "lifecycle_class": kind.get("lifecycle_class")},
        )


def verdict_outcome(checked: "CheckedProposal", same_word: tuple[str, ...] | list[str]) -> str:
    """O4: an alias only when the word has a neighbour and every same-word verdict is same."""
    if checked.neighbours and same_word and all(verdict == SAME for verdict in same_word):
        return OUTCOME_ALIAS
    return OUTCOME_MINT


def disputed_of(checked: "CheckedProposal", fit: dict[str, list[str]]) -> frozenset[str]:
    """O5: a disputable field is disputed when no judgment fits it or one says it does not fit."""
    return frozenset(
        name for name in checked.disputable if not fit.get(name) or DOES_NOT_FIT in fit[name]
    )


def settle(
    registry: "Registry", checked: "CheckedProposal", outcome: str, disputed: frozenset[str]
) -> dict[str, Any]:
    """What the word stores after its judgments: an alias reads as its neighbour; a mint keeps its
    declared columns with the safer value of each disputed field. Raises Refused when the settled
    word would be unusable. The writer and the planner both call this, so a plan never holds an
    operation that the writer must decline for its judgments."""
    if outcome == OUTCOME_ALIAS:
        neighbour = checked.neighbours[0].row
        _alias_direction(registry, checked, neighbour)
        return dict(neighbour)
    columns = safer(checked.word_type, checked.columns, set(disputed))
    _usable_on_kind(registry, checked.word_type, checked.kind_id, columns)
    return columns


def safer(word_type: str, columns: dict[str, Any], disputed: set[str]) -> dict[str, Any]:
    """O5: a disputed field takes the safer value: state rather than permanent or event, no
    consequences, no capability. A status-role mapping has no safer value and stays as declared."""
    columns = dict(columns)
    if "temporal_class" in disputed and columns.get("temporal_class") != "state":
        columns["temporal_class"] = "state"
        columns["default_horizon"] = SAFER_HORIZON_DAYS
    if "carries_consequences" in disputed and columns.get("carries_consequences"):
        columns["carries_consequences"] = False
        columns.pop("downstream_end", None)
    return columns


# The batch state -----------------------------------------------------------------------------


@dataclass
class _Word:
    index: int
    op: dict[str, Any]
    word_type: str
    table: str
    row_id: str
    target_id: str
    kind_id: str | None
    message_key: str
    change_id: str
    outcome: str
    # The reason value id of the operation's row entry: the worked example's entry carries it,
    # so the two are one operation for the rebuild's version rule (S47).
    reason_id: str | None = None


@dataclass
class OntologyState:
    """What the ontology operations of one batch share with `finish`."""

    mints: int = 0
    words: list[_Word] = field(default_factory=list)
    # For each item, each property name id this batch wrote, with the episodes of the supporting
    # rows that the writing operations created (see note_written).
    written: dict[str, dict[str, set[str]]] = field(default_factory=dict)


def note_written(
    run: Any, item_id: str, kind_id: str, names: set[str], evidence_ids: list[str]
) -> None:
    """O8: create_item and update_item note each property name they wrote (every key of a new item
    that holds a value, and each key an update sent with a value that differs from the properties
    it merged onto: the head, or the as-of value for a late fact), with the episodes of the
    supporting rows that operation created. Only these are uses of a property name, and only
    those episodes count for it: evidence that another operation attached to the item does not."""
    if not names:
        return
    created = set(evidence_ids)
    episodes = {
        row["episode_id"]
        for row in run.created_evidence
        if row["id"] in created and row.get("polarity") == "supports" and row.get("episode_id")
    }
    item = _state(run).written.setdefault(item_id, {})
    for row in run.registry.property_names:
        if row.get("kind") == kind_id and row.get("name") in names:
            item.setdefault(row["id"], set()).update(episodes)


# Judgments -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdicts:
    outcome: str
    disputed: frozenset[str]
    judgment_ids: tuple[str, ...]


def _read_judgments(run: Any, judgment_ids: list[str]) -> list[dict[str, Any]]:
    rows = {row["id"]: row for row in run.tx.run(_JUDGMENTS, ids=judgment_ids)}
    missing = [judgment_id for judgment_id in judgment_ids if judgment_id not in rows]
    if missing:
        raise Refused(
            "unrecorded_verdict",
            "unrecorded_verdict",
            "a cited judgment is not a recorded judgment of this world",
            {"judgment_ids": missing},
        )
    for row in rows.values():
        if not row.get("model_call_id") or not row.get("model"):
            raise Refused(
                "unrecorded_verdict",
                "judgment_call",
                "a cited judgment names no recorded model call",
                {"judgment_id": row["id"]},
            )
    return [rows[judgment_id] for judgment_id in dict.fromkeys(judgment_ids)]


def _values(run: Any, value_ids: list[str]) -> dict[str, Any]:
    if not value_ids:
        return {}
    rows = run.tx.run(
        "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
        ids=sorted(set(value_ids)),
    )
    return {row["id"]: row["value"] for row in rows}


def _value_id(ref: str) -> str | None:
    head, _, rest = ref.partition(":")
    return rest if head == "value" and rest else None


def _decoded(held: Any) -> Any:
    try:
        return json.loads(held) if isinstance(held, str) else None
    except ValueError:
        return None


def _subject_field(
    checked: CheckedProposal, subject: str, values: dict[str, Any]
) -> tuple[str, str | None]:
    """What a cited subject judged (O3): ("pair", None) for the word against its nearest
    neighbour, ("field", name) for one declared field, ("other", None) for anything else."""
    parts = subject.split("|")
    if len(parts) == 2 and checked.neighbours and parts == sorted(parts):
        neighbour = f"{checked.word_type}:{checked.neighbours[0].row['id']}"
        others = [part for part in parts if part != neighbour]
        value_id = _value_id(others[0]) if len(others) == 1 else None
        held = _decoded(values.get(value_id) if value_id else None)
        if (
            isinstance(held, dict)
            and set(held) == {"name", "reversed", "word_type"}
            and held["word_type"] == checked.word_type
            and held["reversed"] is checked.reversed
            and isinstance(held["name"], str)
            and normalise_phrase(held["name"]) == normalise_phrase(checked.name)
        ):
            return "pair", None
        return "other", None
    value_id = _value_id(subject) if len(parts) == 1 else None
    decoded = _decoded(values.get(value_id) if value_id else None)
    if (
        isinstance(decoded, dict)
        and set(decoded) == {"field", "kind", "name", "value", "word_type"}
        and decoded["kind"] == checked.kind_id
        and decoded["field"] in checked.field_values
        and decoded["word_type"] == checked.word_type
        and isinstance(decoded["name"], str)
        and normalise_phrase(decoded["name"]) == normalise_phrase(checked.name)
        and decoded["value"] == checked.field_values[decoded["field"]]
    ):
        return "field", decoded["field"]
    return "other", None


def judge_verdicts(run: Any, checked: CheckedProposal, judgment_ids: list[str]) -> Verdicts:
    """O3, O4, O5: the outcome and the disputed fields, from recorded judgments only. A word with no
    neighbour (the first property name of a kind) has no same-word question: with no judgment, its
    disputable fields take their safer values."""
    if not judgment_ids and not checked.neighbours:
        return Verdicts(OUTCOME_MINT, frozenset(checked.disputable), ())
    if not judgment_ids:
        raise Refused(
            "second_judge_required",
            "judgments_required",
            "a proposal cites the recorded judgments of two models",
            {},
        )
    rows = _read_judgments(run, judgment_ids)
    refs = [part for row in rows for part in str(row.get("subject") or "").split("|")]
    values = _values(run, [value_id for ref in refs if (value_id := _value_id(ref))])
    same_word: list[dict[str, Any]] = []
    fit: dict[str, list[str]] = {}
    for row in rows:
        role, verdict = row.get("role"), row.get("verdict")
        judged, field_name = _subject_field(checked, str(row.get("subject") or ""), values)
        if judged == "pair":
            if role != SAME_WORD:
                raise Refused(
                    "unrecorded_verdict",
                    "judgment_role",
                    "a judgment of the word against its neighbour has the role same_word",
                    {"judgment_id": row["id"]},
                )
            if verdict not in (SAME, DIFFERENT):
                raise Refused(
                    "unrecorded_verdict",
                    "judgment_verdict",
                    "a same-word verdict is same or different",
                    {"judgment_id": row["id"]},
                )
            same_word.append(row)
        elif judged == "field":
            if role != FIELD_FIT:
                raise Refused(
                    "unrecorded_verdict",
                    "judgment_role",
                    "a judgment of a declared field has the role field_fit",
                    {"judgment_id": row["id"]},
                )
            if verdict not in (FITS, DOES_NOT_FIT):
                raise Refused(
                    "unrecorded_verdict",
                    "judgment_verdict",
                    "a field-fit verdict is fits or does_not_fit",
                    {"judgment_id": row["id"]},
                )
            fit.setdefault(str(field_name), []).append(verdict)
        else:
            raise Refused(
                "unrecorded_verdict",
                "judgment_subject",
                "a cited judgment did not judge this proposal",
                {"judgment_id": row["id"]},
            )
    if checked.neighbours:
        calls = {row["model_call_id"] for row in same_word}
        models = {row["model"] for row in same_word}
        correlated = any(row.get("correlated") is True for row in same_word)
        if len(calls) < 2 or (len(models) < 2 and not correlated):
            raise Refused(
                "second_judge_required",
                "two_models",
                "a proposal cites same-word judgments from two models, or a correlated second "
                "judge of the same model",
                {},
            )
    outcome = verdict_outcome(checked, [row["verdict"] for row in same_word])
    return Verdicts(outcome, disputed_of(checked, fit), tuple(dict.fromkeys(judgment_ids)))


# Writes --------------------------------------------------------------------------------------


def _state(run: Any) -> OntologyState:
    return run.ontology


def _message_key(run: Any, example: Any) -> str:
    """The message key of the worked example: of an observation of this batch, or of an active
    evidence row whose observation the batch lends to the use."""
    if (
        not isinstance(example, dict)
        or len(example) != 1
        or not ({"ev_id", "evidence_id"} & set(example))
    ):
        raise _bad(
            "type", "worked_example is {ev_id} or {evidence_id}", {"field": "worked_example"}
        )
    if "ev_id" in example:
        return run.observation(_text(example["ev_id"], "worked_example.ev_id"))["message_key"]
    rows = run.tx.run(
        "MATCH (e:Evidence {id: $id, active: true}) RETURN e.message_key AS message_key",
        id=_text(example["evidence_id"], "worked_example.evidence_id"),
    )
    if not rows:
        raise Refused(
            "missing_provenance",
            "evidence_not_found",
            "the worked example names no active evidence row of this world",
            {},
        )
    return rows[0]["message_key"]


def _change(
    run: Any,
    *,
    operation: str,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    inverse: dict[str, Any],
    reason_id: str | None,
    judgment_ids: tuple[str, ...] = (),
    actor: str | None = None,
) -> str:
    change_id = run.new_id()
    run.tx.run(
        _ADD_CHANGE,
        row=compact(
            {
                "id": change_id,
                "operation": operation,
                "rows_before": json.dumps(before, sort_keys=True, ensure_ascii=False, default=str),
                "rows_after": json.dumps(after, sort_keys=True, ensure_ascii=False, default=str),
                "ids_repointed": [],
                "inverse_operation": json.dumps(inverse, sort_keys=True),
                "reason": reason_id,
                "evidence_ids": [],
                "judgment_ids": list(judgment_ids),
                "actor": actor or run.stamp.actor,
                "at": run.at,
                # S46: an undo of this batch finds the row by it (O10's undone by).
                "change_set_id": run.stamp.change_set_id,
                "version": 1,
                "touched_at": run.at,
            }
        ),
    )
    return change_id


def _registry_row(
    run: Any, checked: CheckedProposal, status: str, disputed: set[str]
) -> dict[str, Any]:
    neighbours = [
        {"id": neighbour.row["id"], "distinction": run.text(neighbour.distinction)}
        for neighbour in checked.neighbours
    ]
    row = {
        "id": run.new_id(),
        "name": checked.name,
        "description": run.text(checked.description),
        "status": status,
        "origin": _MINTED,
        "reason": run.text(checked.reason),
        "nearest_neighbours_at_mint": json.dumps(neighbours, sort_keys=True),
        "minted_at": run.at,
        "minted_by": run.stamp.actor,
        "distinct_episodes": 0,
        "uses": 0,
        "primitive": False,
        "version": 1,
        "touched_at": run.at,
        "disputed_fields": json.dumps(sorted(disputed)) if disputed else None,
        **safer(checked.word_type, checked.columns, disputed),
    }
    if checked.word_type == "property_name":
        # B.1 gives property names no primitive column: no primitive row is a property name.
        row.pop("primitive")
    return compact(row)


def _end_fits(registry: "Registry", declared: dict[str, Any], allowed: dict[str, Any]) -> bool:
    """Whether every kind and class the proposal declares at one end fits that end of the
    neighbour. A proposal end of any kind fits only a neighbour end of any kind."""
    if allowed.get("any"):
        return True
    if declared.get("any"):
        return False
    kinds, classes = set(allowed.get("kinds") or []), set(allowed.get("classes") or [])
    for kind_id in declared.get("kinds") or []:
        if kind_id not in kinds and registry.kind(kind_id).get("lifecycle_class") not in classes:
            return False
    return all(lifecycle_class in classes for lifecycle_class in declared.get("classes") or [])


def _alias_direction(
    registry: "Registry", checked: CheckedProposal, neighbour: dict[str, Any]
) -> None:
    """O15: an alias made from a mint carries the reading the proposal stated. When the proposal's
    declared ends fit the neighbour only the other way round, the stated reading is wrong, and the
    alias would invert every link written with the phrase: refused."""
    if checked.word_type != "meaning":
        return
    ends = [json.loads(checked.columns["from_kinds"]), json.loads(checked.columns["to_kinds"])]
    if checked.reversed:
        ends.reverse()
    first = json.loads(neighbour.get("from_kinds") or '{"any": true}')
    second = json.loads(neighbour.get("to_kinds") or '{"any": true}')
    stated = _end_fits(registry, ends[0], first) and _end_fits(registry, ends[1], second)
    other = _end_fits(registry, ends[1], first) and _end_fits(registry, ends[0], second)
    if not stated and other:
        raise _bad(
            "alias_direction",
            "the declared ends fit the neighbour only read from the other end: state reversed",
            {"neighbour": neighbour["id"], "reversed": checked.reversed},
        )


def _add_to_registry(run: Any, word_type: str, row: dict[str, Any]) -> None:
    """The batch's registry learns the new row at once, so a later operation of this batch writes
    with the word by its name."""
    add_to_registry(run.registry, word_type, row)


def add_to_registry(registry: "Registry", word_type: str, row: dict[str, Any]) -> None:
    """A registry learns one row (a word, or an alias as word type `alias`). The plan's own copy
    of the registry learns each planned word the same way, so it checks later proposals as the
    writer will."""
    if word_type == "kind":
        registry.kinds[row["id"]] = row
    elif word_type == "meaning":
        registry.meanings[row["id"]] = row
    elif word_type == "property_name":
        registry.property_names.append(row)
    else:
        registry.aliases.append(row)


def propose_word(run: Any, index: int, op: dict[str, Any]) -> None:
    state = _state(run)
    fresh = frozenset(word.row_id for word in state.words if word.outcome == OUTCOME_MINT)
    checked = check_proposal(run.registry, op, fresh=fresh)
    run.summaries[index] = f"{checked.word_type} {checked.name!r}"
    message_key = _message_key(run, op.get("worked_example"))
    verdicts = judge_verdicts(run, checked, list(op.get("judgment_ids") or []))
    # The same settlement as the plan's: the alias direction guard, a usable mint.
    settle(run.registry, checked, verdicts.outcome, verdicts.disputed)
    reason_id = None
    extra: list[tuple[str, dict[str, Any]]] = []
    if verdicts.outcome == OUTCOME_ALIAS:
        neighbour = checked.neighbours[0].row
        reason_id = run.text(checked.reason)
        alias = compact(
            {
                "id": run.new_id(),
                "phrase": checked.name,
                "normalised_phrase": normalise_phrase(checked.name),
                "target_type": checked.word_type,
                "target_id": neighbour["id"],
                "reversed": checked.reversed,
                "added_at": run.at,
                "reason": reason_id,
                "status": _CANONICAL,
                "origin": _MINTED,
                "minted_at": run.at,
                "minted_by": run.stamp.actor,
                "version": 1,
                "touched_at": run.at,
            }
        )
        run.tx.run(_ADD["alias"], row=alias)
        _add_to_registry(run, "alias", alias)
        table, row, target_id = "aliases", alias, neighbour["id"]
    else:
        state.mints += 1
        if state.mints > MINT_VALVE:
            raise _bad(
                "mint_valve",
                f"a batch mints at most {MINT_VALVE} new words: write the rest with the generic "
                "meaning and keep the phrase",
                {"valve": MINT_VALVE},
            )
        row = _registry_row(run, checked, _PROVISIONAL, set(verdicts.disputed))
        reason_id = row["reason"]
        run.tx.run(_ADD[checked.word_type], row=row)
        _add_to_registry(run, checked.word_type, row)
        table, target_id = TABLES[checked.word_type], row["id"]
        capability = checked.capability
        if capability is not None and "proposed_capability" not in verdicts.disputed:
            capability_row = {
                "id": run.new_id(),
                "connector_type": capability["connector_type"],
                "registry_id": row["id"],
                "capability": capability["capability"],
                "origin": "minted_with_a_word",
                "reason": reason_id,
                "version": 1,
                "touched_at": run.at,
            }
            run.tx.run(_ADD_CAPABILITY, row=capability_row)
            extra.append(("capabilities", capability_row))
    after = [row_json(table, row, run.stamp.world_id)]
    inverse: dict[str, Any] = {"operation": "retire", "table": table, "id": row["id"]}
    for extra_table, extra_row in [(table, row), *extra]:
        # O10: every registry row the proposal wrote has its change entry, so a rebuild from
        # history restores it; the change log row holds each one and its inverse names it.
        run.entry(
            entity_type=_ENTITY,
            entity_id=extra_row["id"],
            field="row",
            new=row_json(extra_table, extra_row, run.stamp.world_id),
            world_time=run.at,
            reason_id=reason_id,
        )
    for extra_table, extra_row in extra:
        after.append(row_json(extra_table, extra_row, run.stamp.world_id))
        inverse.setdefault("with", []).append({"table": extra_table, "id": extra_row["id"]})
    change_id = _change(
        run,
        operation="propose",
        before=[],
        after=after,
        inverse=inverse,
        reason_id=reason_id,
        judgment_ids=verdicts.judgment_ids,
    )
    state.words.append(
        _Word(
            index=index,
            op=op,
            word_type=checked.word_type,
            table=table,
            row_id=row["id"],
            target_id=target_id,
            kind_id=checked.kind_id,
            message_key=message_key,
            change_id=change_id,
            outcome=verdicts.outcome,
            reason_id=reason_id,
        )
    )


def _find_word(run: Any, word_type: str, ref: str, kind_id: str | None) -> dict[str, Any]:
    if ref in PRIMITIVE_IDS or names_primitive(ref):
        raise _bad("primitive_word", "a primitive word is fixed in code", {"word": ref})
    rows = _words_of(run.registry, word_type, kind_id)
    by_id = {row["id"]: row for row in rows}
    if ref in by_id:
        return by_id[ref]
    resolved = resolve_word(
        run.registry, word_type, ref, kind_id=kind_id if word_type == "property_name" else None
    )
    if resolved.target is None or resolved.via not in ("name", "normalised_name"):
        raise Refused("not_found", "word_not_found", f"no {word_type} named {ref!r}", {"word": ref})
    return resolved.target


def _promote(
    run: Any, word_type: str, row: dict[str, Any], reason_id: str | None, actor: str
) -> None:
    columns = {
        "status": _CANONICAL,
        "version": int(row.get("version") or 1) + 1,
        "touched_at": run.at,
    }
    run.tx.run(_SET[word_type], id=row["id"], columns=columns)
    row.update(columns)
    run.entry(
        entity_type=_ENTITY,
        entity_id=row["id"],
        field="status",
        old=_PROVISIONAL,
        new=_CANONICAL,
        world_time=run.at,
        reason_id=reason_id,
    )
    _change(
        run,
        operation="promote",
        before=[{"table": TABLES[word_type], "id": row["id"], "status": _PROVISIONAL}],
        after=[{"table": TABLES[word_type], "id": row["id"], "status": _CANONICAL}],
        inverse={
            "operation": "promote",
            "table": TABLES[word_type],
            "id": row["id"],
            "status": _PROVISIONAL,
        },
        reason_id=reason_id,
        actor=actor,
    )


def promote_word(run: Any, index: int, op: dict[str, Any]) -> None:
    """K under the earning bar, P at once: the person's word is the strongest evidence (line 1603).
    A primitive word, a canonical one and an unusable one are refused."""
    word_type = _enum(op.get("word_type"), "word_type", WORD_TYPES)
    kind_id = None
    if word_type == "property_name":
        kind_id = run.registry.resolve_kind(_text(op.get("kind"), "kind"))["id"]
    row = _find_word(run, word_type, _text(op.get("word"), "word"), kind_id)
    run.summaries[index] = f"promote {word_type} {row.get('name')!r}"
    if row.get("primitive") or row.get("origin") == "starter":
        raise _bad(
            "primitive_word", "a starter or primitive row begins canonical", {"word": row["id"]}
        )
    if row.get("status") != _PROVISIONAL:
        raise _bad(
            "already_canonical",
            f"only a provisional word is promoted; this one is {row.get('status')}",
            {"status": row.get("status")},
        )
    kind = getattr(run.stamp, "caller_kind", None)
    if kind != "person" and int(row.get("distinct_episodes") or 0) < EARNING_BAR:
        raise _bad(
            "earning_bar",
            f"a word is promoted at {EARNING_BAR} distinct episodes, or by the person",
            {"distinct_episodes": int(row.get("distinct_episodes") or 0), "bar": EARNING_BAR},
        )
    _promote(run, word_type, row, run.text(_text(op.get("reason"), "reason")), run.stamp.actor)


# The end of the batch ------------------------------------------------------------------------


def _target_words(
    run: Any, targets: set[tuple[str, str]]
) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """For each (target type, target id), the words it uses: (word type, row id)."""
    found: dict[tuple[str, str], set[tuple[str, str]]] = {target: set() for target in targets}
    link_ids = sorted(target_id for kind, target_id in targets if kind == "link")
    item_ids = sorted(target_id for kind, target_id in targets if kind == "item")
    registry = run.registry
    if link_ids:
        for row in run.tx.run(_LINK_WORDS, ids=link_ids):
            if row["meaning"] in registry.meanings:
                found[("link", row["id"])].add(("meaning", row["meaning"]))
    if item_ids:
        for row in run.tx.run(_ITEM_WORDS, ids=item_ids):
            words = found[("item", row["id"])]
            if row["kind"] in registry.kinds:
                words.add(("kind", row["kind"]))
            for name in json.loads(row.get("properties") or "{}"):
                declared = next(
                    (
                        entry
                        for entry in registry.property_names
                        if entry.get("kind") == row["kind"] and entry.get("name") == name
                    ),
                    None,
                )
                if declared is not None:
                    words.add(("property_name", declared["id"]))
    return found


def _word_row(run: Any, word: tuple[str, str]) -> dict[str, Any] | None:
    word_type, row_id = word
    registry = run.registry
    if word_type == "kind":
        return registry.kinds.get(row_id)
    if word_type == "meaning":
        return registry.meanings.get(row_id)
    return next((row for row in registry.property_names if row["id"] == row_id), None)


def _uses_word(words: set[tuple[str, str]], item: _Word) -> bool:
    if item.outcome == OUTCOME_ALIAS:
        return (item.word_type, item.target_id) in words
    return (item.word_type, item.row_id) in words


def _worked_examples(run: Any, ops: list[Any]) -> None:
    state = _state(run)
    pending = [word for word in state.words if word.index not in run.refused]
    if not pending:
        return
    rows = [
        row
        for row in (*run.created_evidence, *run.cited_evidence)
        if row.get("polarity") == "supports"
    ]
    targets = {(row["target_type"], row["target_id"]) for row in rows}
    words = _target_words(run, targets)
    for word in pending:
        match = next(
            (
                row
                for row in rows
                if row.get("message_key") == word.message_key
                and _uses_word(words[(row["target_type"], row["target_id"])], word)
            ),
            None,
        )
        if match is None:
            run.refuse(
                word.index,
                ops[word.index],
                _bad(
                    "worked_example_unused",
                    "the worked example is the evidence row of a use of the word in this batch",
                    {"field": "worked_example"},
                ),
            )
            continue
        label = "alias" if word.outcome == OUTCOME_ALIAS else word.word_type
        if label == "alias":
            run.tx.run(
                "MATCH (n:Alias {id: $id}) SET n.worked_example = $evidence",
                id=word.row_id,
                evidence=match["id"],
            )
        else:
            run.tx.run(
                _SET[word.word_type], id=word.row_id, columns={"worked_example": match["id"]}
            )
        run.tx.run(_SET_CHANGE, id=word.change_id, columns={"evidence_ids": [match["id"]]})
        run.entry(
            entity_type=_ENTITY,
            entity_id=word.row_id,
            field="worked_example",
            new=match["id"],
            world_time=run.at,
            reason_id=word.reason_id,
            evidence_id=match["id"],
        )


def _ledger(row: dict[str, Any]) -> set[str]:
    held = row.get("earning_episodes")
    try:
        decoded = json.loads(held) if isinstance(held, str) else []
    except ValueError:
        decoded = []
    if not isinstance(decoded, list):
        # Only _usage writes the ledger, as a JSON list; anything else holds no episode.
        decoded = []
    return {episode for episode in decoded if isinstance(episode, str)}


def _usage(run: Any) -> None:
    """O8: the counts of every word the batch used, and the promotion of a provisional word at the
    earning bar. A kind or a meaning is used by every item or link of it that the batch created,
    and its episodes are those of the new supporting rows on its items and links. A property name
    is used where an operation of the batch wrote it, and its episodes are only those of the rows
    that operation created (note_written).

    A provisional word keeps the episodes it counted (`earning_episodes`), so an episode counts
    once for it whatever the batch did since: a row ended and attached again, a property removed
    from one item and set on another. A canonical word counts an episode that held no earlier
    supporting row, active or ended, on a target that uses the word now."""
    fresh = [row for row in run.created_evidence if row.get("polarity") == "supports"]
    created = run.tx.run(_CREATED, change_set=run.stamp.change_set_id)
    targets = {(row["target_type"], row["target_id"]) for row in fresh}
    new_targets = {(row["type"], row["id"]) for row in created}
    words = _target_words(run, targets | new_targets)

    uses: dict[tuple[str, str], int] = {}
    episodes: dict[tuple[str, str], set[str]] = {}
    for target in new_targets:
        for word in words[target]:
            if word[0] != "property_name":
                uses[word] = uses.get(word, 0) + 1
    for row in fresh:
        for word in words[(row["target_type"], row["target_id"])]:
            if word[0] != "property_name":
                episodes.setdefault(word, set()).add(row["episode_id"])
    for names in _state(run).written.values():
        for word_id, episode_ids in names.items():
            word = ("property_name", word_id)
            uses[word] = uses.get(word, 0) + 1
            episodes.setdefault(word, set()).update(episode_ids)

    fresh_ids = [row["id"] for row in fresh]
    new_episodes: dict[tuple[str, str], int] = {}
    ledgers: dict[tuple[str, str], list[str]] = {}
    prior_cache: dict[str, set[tuple[str, str]]] = {}
    for word, episode_ids in episodes.items():
        row = _word_row(run, word)
        if row is None:
            continue
        if row.get("status") == _PROVISIONAL:
            ledger = _ledger(row)
            gained = episode_ids - ledger
            new_episodes[word] = len(gained)
            if gained:
                ledgers[word] = sorted(ledger | gained)
            continue
        for episode_id in sorted(episode_ids):
            if episode_id not in prior_cache:
                prior = {
                    (row["target_type"], row["target_id"])
                    for row in run.tx.run(_EPISODE_ROWS, episode=episode_id, fresh=fresh_ids)
                    if row["target_type"] in ("item", "link")
                }
                prior_words = _target_words(run, prior)
                prior_cache[episode_id] = (
                    set().union(*prior_words.values()) if prior_words else set()
                )
            if word not in prior_cache[episode_id]:
                new_episodes[word] = new_episodes.get(word, 0) + 1

    for word in sorted(set(uses) | set(new_episodes)):
        row = _word_row(run, word)
        if row is None:
            continue
        columns: dict[str, Any] = {
            "uses": int(row.get("uses") or 0) + uses.get(word, 0),
            "distinct_episodes": int(row.get("distinct_episodes") or 0) + new_episodes.get(word, 0),
            "first_used": row.get("first_used") or run.at,
            "last_used": run.at,
        }
        if word in ledgers:
            columns["earning_episodes"] = json.dumps(ledgers[word])
        run.tx.run(_SET[word[0]], id=row["id"], columns=columns)
        row.update(columns)
        if row.get("status") == _PROVISIONAL and columns["distinct_episodes"] >= EARNING_BAR:
            _promote(run, word[0], row, None, _ACTOR_WRITER)


def finish(run: Any, ops: list[Any]) -> None:
    """The worked examples refuse what they must; the counts run only for a batch that commits."""
    _worked_examples(run, ops)


def count_usage(run: Any) -> None:
    _usage(run)


# A property value that names an item (line 1650) ---------------------------------------------

_ITEM_BY_VALUE = (
    "MATCH (i:Item) WHERE i.id = $value OR toLower(i.title) = $lower "
    "RETURN i.id AS id, i.title AS title LIMIT 5"
)
_ITEM_ALIAS_BY_VALUE = (
    "MATCH (a:Alias {target_type: 'item', normalised_phrase: $normalised}) "
    "RETURN a.target_id AS id, a.status AS status LIMIT 5"
)


def value_names_item(tx: Any, value: Any, own_id: str | None = None) -> str | None:
    """O14: the id of an existing item that a text property value names exactly, by its id, its
    title or one of its usable aliases, or None. A relationship stored as text can be neither
    walked nor corrected as a relationship, so the writer asks for a link instead. The item that
    holds the value (`own_id`) is never the one it names: create and update read the same."""
    if not isinstance(value, str) or reads_blank(value):
        return None
    normalised = normalise_phrase(value)
    if not normalised:
        return None
    for row in tx.run(_ITEM_BY_VALUE, value=value, lower=value.lower()):
        if row["id"] == own_id:
            continue
        if row["id"] == value or normalise_phrase(row.get("title") or "") == normalised:
            return row["id"]
    for row in tx.run(_ITEM_ALIAS_BY_VALUE, normalised=normalised):
        if usable(row) and row["id"] != own_id:
            return row["id"]
    return None


__all__ = [
    "CLASS_ROLES",
    "DIRECTIONS",
    "EARNING_BAR",
    "MINT_VALVE",
    "SAFER_HORIZON_DAYS",
    "VALUE_TYPES",
    "WRITER_SET",
    "CheckedProposal",
    "Neighbour",
    "OntologyState",
    "check_name",
    "check_proposal",
    "count_usage",
    "disputed_of",
    "field_fit_value",
    "finish",
    "judge_verdicts",
    "note_written",
    "pair_subject",
    "promote_word",
    "propose_word",
    "safer",
    "same_word_subject",
    "settle",
    "verdict_outcome",
    "names_primitive",
    "same_word_value",
    "value_names_item",
]


# The registry rows that D3's composite operations move (grove.writer.composite) -------------
# D8 (line 1641) keeps every Cypher statement that names a registry label in this module, so the
# writer lint can see each one: a merge's item aliases, and an undo's inverse of a registry entry.

REGISTRY_LABELS = ("Alias", "Kind", "Meaning", "PropertyName", "Capability")


def registry_label(run: Any, row_id: str) -> str:
    for label in REGISTRY_LABELS:
        if run.tx.run(f"MATCH (n:{label} {{id: $id}}) RETURN n.id AS id", id=row_id):
            return label
    raise Refused("not_found", "registry_row_not_found", f"no registry row {row_id}", {})


def lock_registry(run: Any, label: str, row_id: str) -> dict[str, Any]:
    rows = run.tx.run(
        f"MATCH (n:{label} {{id: $id}}) SET n.touched_at = $at RETURN n", id=row_id, at=run.at
    )
    if not rows:
        raise Refused("not_found", "registry_row_not_found", f"no registry row {row_id}", {})
    return rows[0]["n"]


def set_registry(run: Any, label: str, row_id: str, columns: dict[str, Any]) -> None:
    run.tx.run(f"MATCH (n:{label} {{id: $id}}) SET n += $columns", id=row_id, columns=columns)


def bump_registry(run: Any, label: str, row_id: str) -> None:
    run.tx.run(
        f"MATCH (n:{label} {{id: $id}}) SET n.version = coalesce(n.version, 0) + 1", id=row_id
    )


# The undo of a registry row's creation (D3's inverse, O10's retire) -------------------------
_LABEL_TABLES = {
    "Alias": "aliases",
    "Kind": "kinds",
    "Meaning": "meanings",
    "PropertyName": "property_names",
    "Capability": "capabilities",
}
_LINKS_USING = (
    "MATCH (:Item)-[l:LINK {meaning: $id}]->(:Item) WHERE l.active_key IS NOT NULL "
    "RETURN count(l) AS n"
)
_ITEMS_OF_KIND = "MATCH (i:Item {kind: $id}) WHERE i.end_class IS NULL RETURN count(i) AS n"
_PROPERTIES_OF_KIND = (
    "MATCH (i:Item {kind: $kind}) WHERE i.end_class IS NULL RETURN i.properties AS properties"
)
_ALIASES_OF = (
    "MATCH (a:Alias {target_id: $id}) WHERE coalesce(a.status, '') <> 'retired' "
    "RETURN count(a) AS n"
)


def _in_use(run: Any, label: str, row: dict[str, Any]) -> bool:
    """Whether an active item, link, property or alias uses the word (D8 line 1615)."""
    if run.tx.run(_ALIASES_OF, id=row["id"])[0]["n"]:
        return True
    if label == "Meaning":
        return bool(run.tx.run(_LINKS_USING, id=row["id"])[0]["n"])
    if label == "Kind":
        return bool(run.tx.run(_ITEMS_OF_KIND, id=row["id"])[0]["n"])
    if label == "PropertyName":
        for item in run.tx.run(_PROPERTIES_OF_KIND, kind=row.get("kind")):
            try:
                properties = json.loads(item["properties"] or "{}")
            except ValueError:
                properties = {}
            if isinstance(properties, dict) and properties.get(row.get("name")) is not None:
                return True
    return False


def check_retirable(run: Any, label: str, row: dict[str, Any]) -> None:
    """Refuses the retire that an undo writes for a registry row its change set created.

    A table with no status column has no ending to write, so the creation is not undone (rule
    `no_ending`; today the capabilities a mint wrote). A word that an active item, link, property
    or alias still uses is not retired (D8 line 1615, rule `word_in_use`): the undo inverts its
    own change set's entries newest first, so only rows of later batches are left to count."""
    columns = catalogue.table(_LABEL_TABLES[label]).columns
    if not any("status" in column.props for column in columns):
        raise Refused(
            "invalid_arguments",
            "no_ending",
            f"a {_LABEL_TABLES[label]} row has no ending, so its creation is not undone",
            {"row": row["id"]},
        )
    if label in ("Kind", "Meaning", "PropertyName") and _in_use(run, label, row):
        raise Refused(
            "version_conflict",
            "word_in_use",
            "a later item, link or alias uses this word: end or re-point it first",
            {"word": row["id"]},
        )


# O10: the change log of D8's ontology operations names the undo that reversed each row.
_LOG_OF = "MATCH (c:OntologyChange) WHERE c.change_set_id = $change_set RETURN c.id AS id"
_SET_LOG = (
    "MATCH (c:OntologyChange {id: $id}) SET c.undone_by = $by, "
    "c.change_set_id = $change_set, c.version = coalesce(c.version, 1) + 1, c.touched_at = $at"
)
_ENTRIES_OF = "MATCH (e:ChangeEntry {change_set_id: $change_set}) RETURN e ORDER BY e.sequence"
_LEGACY_LOGS = (
    "MATCH (c:OntologyChange) WHERE c.change_set_id IS NULL "
    "AND c.at >= $low AND c.at <= $high RETURN c"
)
_LEGACY_ENTRIES = (
    "MATCH (e:ChangeEntry) WHERE e.entity_type = 'registry_row' AND e.reason IS NULL "
    "AND e.inverse_of IS NULL AND e.recorded_at >= $low AND e.recorded_at <= $high RETURN e"
)


def _logged_ids(row: dict[str, Any]) -> set[str]:
    ids = set()
    for column in ("rows_before", "rows_after"):
        for part in json.loads(row.get(column) or "[]"):
            held = part.get("row", part)
            if held.get("id"):
                ids.add(held["id"])
    return ids


def _legacy_log_ids(run: Any, entries: list[dict[str, Any]], change_set: str) -> list[str]:
    """Bind old log rows by their operation's reason, registry ids and exact instant.

    Automatic promotions have no reason. If their remaining fields cannot distinguish
    two old batches, refuse instead of marking another operation as undone. The range
    query handles Neo4j's two zone forms; Python compares the actual instants.
    """
    registry = [
        entry
        for entry in entries
        if entry["entity_type"] == _ENTITY and not entry.get("inverse_of")
    ]
    if not registry:
        return []
    at = registry[0]["recorded_at"]
    bounds = {"low": at - timedelta(seconds=1), "high": at + timedelta(seconds=1)}
    found = []
    unreasoned = None
    for result in run.tx.run(_LEGACY_LOGS, **bounds):
        row = result["c"]
        if row["at"] != at:
            continue
        ids = _logged_ids(row)
        if not any(
            entry["entity_id"] in ids and entry.get("reason") == row.get("reason")
            for entry in registry
        ):
            continue
        if row.get("reason") is None:
            if unreasoned is None:
                unreasoned = [held["e"] for held in run.tx.run(_LEGACY_ENTRIES, **bounds)]
            candidates = {
                entry["change_set_id"]
                for entry in unreasoned
                if entry["recorded_at"] == at and entry["entity_id"] in ids
            }
            if candidates != {change_set}:
                raise Refused(
                    "version_conflict",
                    "ontology_log_ambiguous",
                    "the old ontology log cannot distinguish these change sets",
                    {"row": row["id"]},
                )
        found.append(row["id"])
    return found


def mark_undone(run: Any, entries: list[dict[str, Any]], change_set: str) -> None:
    """O10's `undone_by`, after an undo of `change_set` (whose entries are `entries`).

    Follow every inverse_of branch: one batch can undo several change sets, or undo one
    while it writes a new ontology operation. Odd depth leaves an operation reversed;
    even depth restores it. Each log write moves the S12 pair without a change entry.
    """
    pending = [(change_set, entries, 1)]
    seen = set()
    while pending:
        base, held, undos = pending.pop()
        if base in seen:
            continue
        seen.add(base)
        by = run.stamp.change_set_id if undos % 2 == 1 else None
        ids = [row["id"] for row in run.tx.run(_LOG_OF, change_set=base)]
        ids.extend(_legacy_log_ids(run, held, base))
        for row_id in ids:
            run.tx.run(_SET_LOG, id=row_id, by=by, at=run.at, change_set=base)
        for below in sorted({entry["inverse_of"] for entry in held if entry.get("inverse_of")}):
            earlier = [row["e"] for row in run.tx.run(_ENTRIES_OF, change_set=below)]
            pending.append((below, earlier, undos + 1))
