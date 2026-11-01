"""The primitive rows: five registry rows with fixed ids that no ontology operation can touch.

Code identifies these rows by id, never by name (spec lines 189, 637, 1562). The spec gives no id
values, so the build fixes them here. Each row has world_id PRIMITIVE_WORLD and primitive = true.
"""

import json

# '' and not null: null escapes a composite uniqueness constraint, and '' does not (S5).
PRIMITIVE_WORLD = ""

MEANING_DESCRIBES = "00000000-0000-7000-8000-000000000001"
MEANING_SUPERSEDED_BY = "00000000-0000-7000-8000-000000000002"
MEANING_CORRECTED_BY = "00000000-0000-7000-8000-000000000003"
MEANING_AFFECTED_BY = "00000000-0000-7000-8000-000000000004"
KIND_BELIEF = "00000000-0000-7000-8000-000000000005"

PRIMITIVE_MEANING_IDS: tuple[str, ...] = (
    MEANING_DESCRIBES,
    MEANING_SUPERSEDED_BY,
    MEANING_CORRECTED_BY,
    MEANING_AFFECTED_BY,
)
PRIMITIVE_IDS: tuple[str, ...] = (*PRIMITIVE_MEANING_IDS, KIND_BELIEF)

# Ruling S30 (lines 650, 1270, 1757, 1763, 3257): a meaning row declares its ends, direction,
# cardinality, consequence flag and temporal class, and the writer reads them. Endpoints take the
# seed's JSON form. The spec states no cardinality for these four, and `many` refuses nothing the
# writer's own operations allow; none carries consequences, so none has a downstream end or horizon.
_ANY_END = json.dumps({"any": True})
_DECLARED: dict[str, object] = {
    "direction": "from_to",
    "cardinality": "many",
    "carries_consequences": False,
    "decomposition": False,
    "rolls_up_completion": False,
    "generic": False,
    "status": "canonical",
}

PRIMITIVE_MEANINGS: dict[str, dict[str, object]] = {
    # Line 1757: from the belief to each subject, which may be of any kind.
    MEANING_DESCRIBES: {
        **_DECLARED,
        "name": "describes",
        "from_kinds": json.dumps({"kinds": [KIND_BELIEF], "classes": []}),
        "to_kinds": _ANY_END,
        "temporal_class": "permanent",
    },
    MEANING_SUPERSEDED_BY: {
        **_DECLARED,
        "name": "superseded by",
        "from_kinds": _ANY_END,
        "to_kinds": _ANY_END,
        "temporal_class": "permanent",
    },
    MEANING_CORRECTED_BY: {
        **_DECLARED,
        "name": "corrected by",
        "from_kinds": _ANY_END,
        "to_kinds": _ANY_END,
        "temporal_class": "permanent",
    },
    # Line 1270: from the affected item to the trigger item.
    MEANING_AFFECTED_BY: {
        **_DECLARED,
        "name": "affected by",
        "from_kinds": _ANY_END,
        "to_kinds": _ANY_END,
        "temporal_class": "event",
    },
}

# D9 (lines 1742 to 1763): a belief is an item of this kind, its state lives on the beliefs table,
# and the kind has no status vocabulary. D9 names no lifecycle class. Line 639 requires one, and a
# lasting thing is the only class that allows a correction with no status. Line 1763: a belief
# mints only with a `describes` link, which is what required_links holds (S30).
PRIMITIVE_KIND_BELIEF: dict[str, object] = {
    "name": "belief",
    "lifecycle_class": "lasting_thing",
    "status_vocabulary": "[]",
    "required_fields": "[]",
    "required_links": json.dumps([MEANING_DESCRIBES]),
    "propagating_fields": "[]",
    "status": "canonical",
}
