"""The primitive rows: five registry rows with fixed ids that no ontology operation can touch.

Code identifies these rows by id, never by name (spec lines 189, 637, 1562). The spec gives no id
values, so the build fixes them here. Each row has world_id PRIMITIVE_WORLD and primitive = true.
"""

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

# Line 3257: the four meanings carry no consequences. The seed writes these rows; the fields not
# set here stay absent, because the spec declares none for them.
PRIMITIVE_MEANINGS: dict[str, dict[str, object]] = {
    MEANING_DESCRIBES: {"name": "describes", "carries_consequences": False},
    MEANING_SUPERSEDED_BY: {"name": "superseded by", "carries_consequences": False},
    MEANING_CORRECTED_BY: {"name": "corrected by", "carries_consequences": False},
    MEANING_AFFECTED_BY: {"name": "affected by", "carries_consequences": False},
}

# D9 (lines 1742 to 1757): a belief is an item of this kind, its state lives on the beliefs table,
# and the kind has no status vocabulary. D9 names no lifecycle class. Line 639 requires one, and a
# lasting thing is the only class that allows a correction with no status.
PRIMITIVE_KIND_BELIEF: dict[str, object] = {
    "name": "belief",
    "lifecycle_class": "lasting_thing",
}
