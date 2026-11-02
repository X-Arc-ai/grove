"""D8 reuse first (lines 1587-1596, tests 1680-1696) and D10 layer 3 (line 2010) at the resolver.

Each test follows one rule of DESIGN section 6: synonyms reach one word and mint nothing, a
reversed alias swaps the ends, an empty phrase is refused (S34), an ambiguous phrase is never
picked, a retired or split target is unusable, a property name resolves per kind, an unmapped link
phrase rides on the generic meaning with its phrase kept, and the candidate band keeps ended items
and leaves out a distinct pair. Every name is invented.
"""

import json
from datetime import timedelta
from typing import Any

import pytest

from grove.store.normalise import normalise_phrase
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined
from grove.writer.errors import Refused
from grove.writer.registry import Registry
from grove.writer.resolver import (
    AMBIGUOUS,
    NONE,
    RESOLVED,
    Candidate,
    generic_meaning,
    lexical_candidates,
    resolve_link_phrase,
    resolve_word,
)
from grove.writer.testing import plant_node
from grove.writer.tx import WorldTx, WorldWriter
from tests.writer.helpers import (
    T0,
    apply,
    assert_only_decline_log,
    clock_at,
    count_rows,
    create_item,
    create_link,
    created_id,
    iso,
    new_op_id,
    observation,
    read,
    supports,
)

RECHECK = iso(T0 + timedelta(days=30))
REGISTRY_LABELS = ("Kind", "Meaning", "Alias", "PropertyName")
GENERIC = "related to"


# --- local builders -------------------------------------------------------------------------


def registry_of(driver, world_id: str) -> Registry:
    return Registry.load(WorldQuery(driver, world_id))


def registry_counts(driver, world_id: str) -> dict[str, int]:
    counts = count_rows(driver, world_id)
    return {label: counts.get(label, 0) for label in REGISTRY_LABELS}


def plant_alias(
    driver,
    world_id: str,
    target_type: str,
    target_id: str,
    phrase: str,
    *,
    reversed_: bool = False,
    status: str = "canonical",
) -> dict:
    """An alias row as the ontology lane mints one; only that lane writes these for real."""
    return plant_node(
        driver,
        world_id,
        "Alias",
        target_type=target_type,
        target_id=target_id,
        phrase=phrase,
        normalised_phrase=normalise_phrase(phrase),
        reversed=reversed_,
        status=status,
        origin="minted",
        minted_by="test",
        version=1,
    )


def plant_meaning(driver, world_id: str, name: str, *, status: str = "canonical") -> dict:
    return plant_node(
        driver,
        world_id,
        "Meaning",
        name=name,
        status=status,
        origin="minted",
        minted_by="test",
        from_kinds=json.dumps({"any": True}),
        to_kinds=json.dumps({"any": True}),
        direction="from_to",
        cardinality="many",
        carries_consequences=False,
        decomposition=False,
        rolls_up_completion=False,
        temporal_class="permanent",
        generic=False,
        primitive=False,
        version=1,
    )


def plant_property(driver, world_id: str, kind_id: str, name: str) -> dict:
    return plant_node(
        driver,
        world_id,
        "PropertyName",
        kind=kind_id,
        name=name,
        value_type="text",
        temporal_class="permanent",
        status="canonical",
        origin="minted",
        minted_by="test",
        version=1,
    )


def refusal_of(driver, world_id: str, ops: list[dict], evidence: list[dict]):
    """Apply a batch that must be declined by one refusal; assert only the log was written."""
    before = count_rows(driver, world_id)
    with pytest.raises(BatchDeclined) as caught:
        apply(driver, world_id, ops, evidence)
    assert_only_decline_log(driver, world_id, before, caught.value.refusals)
    (refusal,) = caught.value.refusals
    return refusal


def links(driver, world_id: str) -> list[dict]:
    rows = read(
        driver,
        world_id,
        "MATCH (a:Item)-[l:LINK]->(b:Item) RETURN l AS link, a.id AS src, b.id AS dst",
    )
    return [{**row["link"], "_src": row["src"], "_dst": row["dst"]} for row in rows]


def item_row(driver, world_id: str, item_id: str) -> dict:
    (row,) = read(driver, world_id, "MATCH (i:Item {id: $id}) RETURN i", id=item_id)
    return row["i"]


@pytest.fixture
def cast(driver, seeded, chat) -> dict[str, Any]:
    """Four people, a place and an organisation, cited from line 1 of the chat."""
    world_id = seeded["world_id"]
    titles = {
        "rowan": ("person", "Rowan"),
        "ilse": ("person", "Ilse"),
        "tamsin": ("person", "Tamsin"),
        "okonkwo": ("person", "Okonkwo"),
        "harbour": ("place", "Harbour hall"),
        "boats": ("organisation", "Okonkwo Boats"),
    }
    ops = [create_item(kind, title, "e0") for kind, title in titles.values()]
    receipt = apply(driver, world_id, ops, [observation(chat, "e0", 1)])
    ids = {key: created_id(receipt, nth=n) for n, key in enumerate(titles)}
    return {**seeded, "chat": chat, **ids}


# --- D8 synonyms: one meaning, nothing minted -------------------------------------------------

SYNONYMS = [
    ("relates to", "alias"),
    ("related_to", "normalised_name"),
    ("is related to", "normalised_name_after_copula"),
    ("connected with", "alias"),
]


@pytest.mark.parametrize(("phrase", "via"), SYNONYMS)
def test_a_synonym_resolves_to_the_connection_meaning(driver, seeded, phrase, via):
    registry = registry_of(driver, seeded["world_id"])

    found = resolve_word(registry, "meaning", phrase)

    assert found.outcome == RESOLVED
    assert found.target["id"] == seeded["meanings"][GENERIC]
    assert found.reversed is False
    assert found.via == via


def test_the_synonyms_write_one_meaning_and_mint_nothing(driver, cast):
    world_id = cast["world_id"]
    before = registry_counts(driver, world_id)
    targets = ["ilse", "tamsin", "okonkwo", "harbour"]
    ops = [
        create_link(cast["rowan"], phrase, cast[target], "e1", recheck_by=RECHECK)
        for (phrase, _), target in zip(SYNONYMS, targets, strict=True)
    ]

    apply(driver, world_id, ops, [observation(cast["chat"], "e1", 2)])

    written = links(driver, world_id)
    assert sorted(link["_dst"] for link in written) == sorted(cast[key] for key in targets)
    assert {link["meaning"] for link in written} == {cast["meanings"][GENERIC]}
    assert all(link.get("original_phrase") is None for link in written)
    assert registry_counts(driver, world_id) == before


def test_a_past_tense_phrase_stays_unmatched(driver, seeded):
    registry = registry_of(driver, seeded["world_id"])

    assert resolve_word(registry, "meaning", "was owned by").outcome == NONE
    found = resolve_word(registry, "meaning", "are owned by")
    assert found.target["id"] == seeded["meanings"]["owned by"]
    assert found.via == "normalised_name_after_copula"


def test_a_name_wins_over_an_alias_of_the_same_phrase(driver, seeded):
    world_id = seeded["world_id"]
    plant_alias(driver, world_id, "meaning", seeded["meanings"]["depends on"], "blocks")

    found = resolve_word(registry_of(driver, world_id), "meaning", "Blocks")

    assert (found.outcome, found.via) == (RESOLVED, "normalised_name")
    assert found.target["id"] == seeded["meanings"]["blocks"]


# --- direction --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "meaning", "reversed_", "via"),
    [
        ("owns", "owned by", True, "alias"),
        ("is owned by", "owned by", False, "normalised_name_after_copula"),
        ("is needed by", "depends on", True, "alias_after_copula"),
        ("needs", "depends on", False, "alias"),
    ],
)
def test_a_reversed_alias_says_so(driver, seeded, phrase, meaning, reversed_, via):
    found = resolve_word(registry_of(driver, seeded["world_id"]), "meaning", phrase)

    assert found.target["id"] == seeded["meanings"][meaning]
    assert (found.reversed, found.via) == (reversed_, via)


def test_owns_is_stored_as_owned_by_with_the_ends_swapped(driver, cast):
    world_id = cast["world_id"]
    op = create_link(cast["rowan"], "owns", cast["harbour"], "e1", recheck_by=RECHECK)

    apply(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])

    (link,) = links(driver, world_id)
    assert link["meaning"] == cast["meanings"]["owned by"]
    assert (link["_src"], link["_dst"]) == (cast["harbour"], cast["rowan"])


# --- an empty normal form is refused (S34) ----------------------------------------------------

EMPTY = ["___", "...", "-_-"]


@pytest.mark.parametrize("phrase", EMPTY)
@pytest.mark.parametrize("word_type", ["kind", "meaning"])
def test_resolve_word_refuses_an_empty_phrase(driver, seeded, phrase, word_type):
    with pytest.raises(Refused) as refused:
        resolve_word(registry_of(driver, seeded["world_id"]), word_type, phrase)

    assert (refused.value.code, refused.value.rule) == ("invalid_arguments", "empty_phrase")


@pytest.mark.parametrize("phrase", EMPTY)
def test_the_writer_refuses_an_empty_kind_meaning_or_property(driver, cast, phrase):
    world_id = cast["world_id"]
    evidence = [observation(cast["chat"], "e1", 2)]
    ops = [
        create_item(phrase, "The grey hull", "e1"),
        create_link(cast["rowan"], phrase, cast["ilse"], "e1", recheck_by=RECHECK),
        create_item("person", "Rowan Tamsin", "e1", properties={phrase: "a value"}),
    ]

    for op in ops:
        refusal = refusal_of(driver, world_id, [op], evidence)
        assert (refusal.code, refusal.rule) == ("invalid_arguments", "empty_phrase")


def test_link_phrase_and_candidates_refuse_an_empty_phrase(driver, seeded):
    world_id = seeded["world_id"]
    with pytest.raises(Refused) as link:
        resolve_link_phrase(registry_of(driver, world_id), "...")
    with pytest.raises(Refused) as band:
        lexical_candidates(WorldQuery(driver, world_id), seeded["kinds"]["task"], "___")

    assert link.value.rule == band.value.rule == "empty_phrase"


# --- an ambiguous phrase is never picked ------------------------------------------------------


@pytest.fixture
def tied_to(driver, seeded) -> dict[str, Any]:
    """One phrase that is an alias of two usable meanings."""
    world_id = seeded["world_id"]
    for meaning in ("depends on", "part of"):
        plant_alias(driver, world_id, "meaning", seeded["meanings"][meaning], "tied to")
    return seeded


def test_an_alias_of_two_meanings_is_ambiguous(driver, tied_to):
    found = resolve_word(registry_of(driver, tied_to["world_id"]), "meaning", "Tied_to")

    assert (found.outcome, found.target, found.via) == (AMBIGUOUS, None, "alias")
    expected = {tied_to["meanings"]["depends on"], tied_to["meanings"]["part of"]}
    assert {candidate["id"] for candidate in found.candidates} == expected
    assert {candidate["name"] for candidate in found.candidates} == {"depends on", "part of"}


def test_the_writer_refuses_an_ambiguous_meaning(driver, cast, tied_to):
    world_id = cast["world_id"]
    op = create_link(cast["rowan"], "tied to", cast["ilse"], "e1", recheck_by=RECHECK)

    refusal = refusal_of(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])

    assert (refusal.code, refusal.rule) == ("unknown_meaning", "ambiguous_phrase")
    named = {candidate["id"] for candidate in refusal.details["candidates"]}
    assert named == {cast["meanings"]["depends on"], cast["meanings"]["part of"]}


def test_the_writer_refuses_an_ambiguous_kind(driver, seeded, chat):
    world_id = seeded["world_id"]
    for kind in ("task", "plan"):
        plant_alias(driver, world_id, "kind", seeded["kinds"][kind], "job")
    op = create_item("job", "Paint the mast", "e1", status="open", recheck_by=RECHECK)

    refusal = refusal_of(driver, world_id, [op], [observation(chat, "e1", 2)])

    assert (refusal.code, refusal.rule) == ("unknown_kind", "ambiguous_phrase")
    named = {candidate["id"] for candidate in refusal.details["candidates"]}
    assert named == {seeded["kinds"]["task"], seeded["kinds"]["plan"]}


def test_the_writer_refuses_an_ambiguous_property_name(driver, seeded, chat):
    world_id = seeded["world_id"]
    person = seeded["kinds"]["person"]
    for name in ("nickname", "handle"):
        target = plant_property(driver, world_id, person, name)
        plant_alias(driver, world_id, "property_name", target["id"], "called")
    op = create_item("person", "Rowan Ilse", "e1", properties={"called": "Ro"})

    refusal = refusal_of(driver, world_id, [op], [observation(chat, "e1", 2)])

    assert (refusal.code, refusal.rule) == ("invalid_arguments", "ambiguous_phrase")


def test_a_second_target_that_is_retired_leaves_one_usable(driver, seeded):
    world_id = seeded["world_id"]
    gone = plant_meaning(driver, world_id, "fastened to", status="retired")
    plant_alias(driver, world_id, "meaning", gone["id"], "tied to")
    plant_alias(driver, world_id, "meaning", seeded["meanings"]["part of"], "tied to")

    found = resolve_word(registry_of(driver, world_id), "meaning", "tied to")

    assert found.outcome == RESOLVED
    assert found.target["id"] == seeded["meanings"]["part of"]


# --- a retired or split target behind a canonical alias is unusable ---------------------------


@pytest.mark.parametrize("status", ["retired", "split"])
def test_a_canonical_alias_of_an_unusable_meaning_reaches_nothing(driver, cast, status):
    # a1 checked only the alias row, so this alias reached the retired meaning.
    world_id = cast["world_id"]
    gone = plant_meaning(driver, world_id, "escorted by", status=status)
    plant_alias(driver, world_id, "meaning", gone["id"], "chaperoned by")
    registry = registry_of(driver, world_id)

    assert resolve_word(registry, "meaning", "chaperoned by").outcome == NONE
    assert resolve_word(registry, "meaning", "escorted by").outcome == NONE
    op = create_link(cast["rowan"], "chaperoned by", cast["ilse"], "e1", recheck_by=RECHECK)
    refusal = refusal_of(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])
    assert (refusal.code, refusal.rule) == ("unknown_meaning", "unknown_meaning")


@pytest.mark.parametrize("status", ["retired", "split"])
def test_a_canonical_alias_of_an_unusable_kind_reaches_nothing(driver, seeded, chat, status):
    world_id = seeded["world_id"]
    gone = plant_node(
        driver, world_id, "Kind", name="vessel", lifecycle_class="lasting_thing", status=status
    )
    plant_alias(driver, world_id, "kind", gone["id"], "boat")
    op = create_item("boat", "The grey hull", "e1")

    refusal = refusal_of(driver, world_id, [op], [observation(chat, "e1", 2)])

    assert (refusal.code, refusal.rule) == ("unknown_kind", "unknown_kind")


def test_a_retired_alias_of_a_usable_meaning_reaches_nothing(driver, seeded):
    world_id = seeded["world_id"]
    plant_alias(
        driver, world_id, "meaning", seeded["meanings"]["blocks"], "stalls", status="retired"
    )

    assert resolve_word(registry_of(driver, world_id), "meaning", "stalls").outcome == NONE


# --- a property name resolves per kind --------------------------------------------------------


def _row(row_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": row_id, "status": "canonical", **fields}


def test_a_property_alias_of_another_kind_is_no_match_and_no_miss():
    # a1 took the first alias of the phrase of any kind, then missed when it was another kind's.
    alias = {"target_type": "property_name", "normalised_phrase": "owner name", "reversed": False}
    registry = Registry(
        kinds={},
        meanings={},
        aliases=[
            {**alias, "id": "a1", "status": "canonical", "target_id": "p-org"},
            {**alias, "id": "a2", "status": "canonical", "target_id": "p-place"},
        ],
        property_names=[
            _row("p-org", kind="k-org", name="proprietor"),
            _row("p-place", kind="k-place", name="landlord"),
        ],
    )

    assert registry.property_name("k-place", "owner_name")["id"] == "p-place"
    assert registry.property_name("k-org", "Owner name")["id"] == "p-org"
    assert registry.property_name("k-person", "owner name") is None


def test_owner_owned_by_and_owner_name_are_one_property(driver, seeded):
    # Line 1689. "owned_by" is also the starter meaning "owned by": a property alias never
    # reaches a meaning, and a meaning alias never reaches a property.
    world_id = seeded["world_id"]
    organisation = seeded["kinds"]["organisation"]
    owner = plant_property(driver, world_id, organisation, "owner")
    for phrase in ("owned by", "owner name"):
        plant_alias(driver, world_id, "property_name", owner["id"], phrase)
    registry = registry_of(driver, world_id)

    for phrase in ("owner", "owned_by", "owner_name"):
        assert registry.property_name(organisation, phrase)["id"] == owner["id"]
    found = resolve_word(registry, "meaning", "owned_by")
    assert found.target["id"] == seeded["meanings"]["owned by"]


def test_each_kind_stores_the_property_under_its_own_name(driver, seeded, chat):
    world_id = seeded["world_id"]
    for kind, name in (("organisation", "proprietor"), ("place", "landlord")):
        target = plant_property(driver, world_id, seeded["kinds"][kind], name)
        plant_alias(driver, world_id, "property_name", target["id"], "owner name")
    ops = [
        create_item("organisation", "Ilse Ropes", "e1", properties={"owner name": "Ilse"}),
        create_item("place", "Tamsin quay", "e1", properties={"owner_name": "Tamsin"}),
    ]

    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 2)])

    ropes, quay = (item_row(driver, world_id, created_id(receipt, nth=n)) for n in (0, 1))
    assert json.loads(ropes["properties"]) == {"proprietor": "Ilse"}
    assert json.loads(quay["properties"]) == {"landlord": "Tamsin"}


def test_resolve_word_needs_a_kind_for_a_property_name_only(driver, seeded):
    registry = registry_of(driver, seeded["world_id"])
    with pytest.raises(ValueError):
        resolve_word(registry, "property_name", "owner")
    with pytest.raises(ValueError):
        resolve_word(registry, "meaning", "owns", kind_id=seeded["kinds"]["person"])
    with pytest.raises(ValueError):
        resolve_word(registry, "item", "owns")


# --- an unmapped link phrase rides on the generic meaning (line 1596) -------------------------


def test_a_resolved_link_phrase_keeps_no_phrase(driver, seeded):
    word = resolve_link_phrase(registry_of(driver, seeded["world_id"]), "relies on")

    assert word.outcome == RESOLVED
    assert word.meaning["id"] == seeded["meanings"]["depends on"]
    assert (word.original_phrase, word.candidates) == (None, ())


def test_an_unmapped_link_phrase_rides_on_the_generic_meaning(driver, seeded):
    word = resolve_link_phrase(registry_of(driver, seeded["world_id"]), "sits near")

    assert word.outcome == NONE
    assert word.meaning["id"] == seeded["meanings"][GENERIC]
    assert word.meaning["carries_consequences"] is False
    assert (word.reversed, word.original_phrase, word.candidates) == (False, "sits near", ())


def test_an_ambiguous_link_phrase_rides_on_the_generic_meaning(driver, tied_to):
    word = resolve_link_phrase(registry_of(driver, tied_to["world_id"]), "tied to")

    assert word.outcome == AMBIGUOUS
    assert word.meaning["id"] == tied_to["meanings"][GENERIC]
    assert word.meaning["carries_consequences"] is False
    assert word.original_phrase == "tied to"
    expected = {tied_to["meanings"]["depends on"], tied_to["meanings"]["part of"]}
    assert {candidate["id"] for candidate in word.candidates} == expected


def test_the_generic_word_is_written_with_its_phrase_kept(driver, cast):
    world_id = cast["world_id"]
    word = resolve_link_phrase(registry_of(driver, world_id), "sits near")
    op = create_link(
        cast["tamsin"],
        word.meaning["name"],
        cast["harbour"],
        "e1",
        original_phrase=word.original_phrase,
        recheck_by=RECHECK,
    )

    apply(driver, world_id, [op], [observation(cast["chat"], "e1", 2)])

    (link,) = links(driver, world_id)
    assert link["meaning"] == cast["meanings"][GENERIC]
    (phrase,) = read(
        driver,
        world_id,
        "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value",
        id=link["original_phrase"],
    )
    assert phrase["value"] == "sits near"


def test_with_no_usable_generic_row_an_unmapped_phrase_reaches_nothing(driver, seeded):
    # The fallback is found by the declared field, so a world without it never falls back to
    # some other meaning, least of all one that carries consequences.
    world_id = seeded["world_id"]
    with_generic = registry_of(driver, world_id)
    rows = {**with_generic.meanings}
    generic_id = seeded["meanings"][GENERIC]
    rows[generic_id] = {**rows[generic_id], "status": "retired"}
    without = Registry(with_generic.kinds, rows, with_generic.aliases, [])

    assert generic_meaning(without) is None
    assert resolve_link_phrase(without, "sits near") is None
    assert resolve_link_phrase(without, "relies on").meaning["name"] == "depends on"


def test_a_generic_row_that_carries_consequences_is_no_fallback(driver, seeded):
    world_id = seeded["world_id"]
    loaded = registry_of(driver, world_id)
    rows = {**loaded.meanings}
    generic_id = seeded["meanings"][GENERIC]
    rows[generic_id] = {**rows[generic_id], "carries_consequences": True}

    assert resolve_link_phrase(Registry(loaded.kinds, rows, loaded.aliases, []), "x y") is None


# --- lexical candidates: the 0.3 band, ended items flagged, distinct pairs left out ------------


def end_item(item_id: str, ev_id: str) -> dict[str, Any]:
    return {
        "op": "end_item",
        "op_id": new_op_id(),
        "item": item_id,
        "end_class": "ended_in_the_world",
        "stopped_being_true": clock_at(T0),
        "evidence_refs": supports(ev_id),
        "reason": "the message says it is over",
    }


def cancel(item_id: str, ev_id: str) -> dict[str, Any]:
    return {
        "op": "change_status",
        "op_id": new_op_id(),
        "item": item_id,
        "status": "cancelled",
        "evidence_refs": supports(ev_id),
        "reason": "Rowan called it off",
    }


@pytest.fixture
def deposits(driver, seeded, chat) -> dict[str, Any]:
    """Tasks around a venue deposit: two live, one ended, one cancelled, one far off."""
    world_id = seeded["world_id"]
    work = {"status": "open", "recheck_by": RECHECK}
    titles = {
        "today": "Pay the venue deposit today",
        "paid": "Venue deposit paid",
        "caterer": "Pay the caterer deposit",
        "venue": "Book the venue",
        "invites": "Print the invitations",
    }
    ops = [create_item("task", title, "e1", **work) for title in titles.values()]
    ops.append(create_item("observation", "Pay the venue deposit", "e1"))
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 1)])
    ids = {key: created_id(receipt, nth=n) for n, key in enumerate(titles)}
    ids["note"] = created_id(receipt, nth=len(titles))
    apply(
        driver,
        world_id,
        [end_item(ids["paid"], "e2"), cancel(ids["caterer"], "e2")],
        [observation(chat, "e2", 2)],
    )
    return {**seeded, "chat": chat, **ids}


def test_candidates_are_the_band_best_first_with_ended_items_flagged(driver, deposits):
    query = WorldQuery(driver, deposits["world_id"])

    found = lexical_candidates(query, deposits["kinds"]["task"], "Pay the venue deposit")

    assert found == [
        Candidate(deposits["today"], 0.846, False),
        Candidate(deposits["paid"], 0.64, True),
        Candidate(deposits["caterer"], 0.533, True),
        Candidate(deposits["venue"], 0.37, False),
    ]


def test_the_gate_skips_an_ended_item_that_the_band_keeps(driver, deposits):
    # The gate weighs only items with no end class; the band keeps them for a late fact.
    world_id = deposits["world_id"]
    query = WorldQuery(driver, world_id)
    ended = item_row(driver, world_id, deposits["paid"])
    assert ended["end_class"] == "ended_in_the_world"

    found = lexical_candidates(query, deposits["kinds"]["task"], "Venue deposit paid!")
    op = create_item("task", "Venue deposit paid!", "e3", status="open", recheck_by=RECHECK)
    apply(driver, world_id, [op], [observation(deposits["chat"], "e3", 3)])

    assert found[0] == Candidate(deposits["paid"], 1.0, True)


def test_an_item_alias_scores_like_its_title(driver, deposits):
    world_id = deposits["world_id"]
    plant_alias(driver, world_id, "item", deposits["invites"], "Book the venue for the offsite")

    found = lexical_candidates(
        WorldQuery(driver, world_id), deposits["kinds"]["task"], "Offsite: book a venue"
    )

    assert found == [
        Candidate(deposits["invites"], 0.655, False),
        Candidate(deposits["venue"], 0.44, False),
    ]


@pytest.fixture
def revisions(driver, seeded, chat) -> dict[str, Any]:
    """Revision 8 recorded as distinct from revision 7, and an unrelated summary."""
    world_id = seeded["world_id"]
    evidence = [observation(chat, "e1", 1)]
    first = created_id(
        apply(
            driver,
            world_id,
            [create_item("observation", "Release notes revision 7", "e1")],
            evidence,
        )
    )
    distinct = [{"item": first, "reason": "the next revision of it"}]
    ops = [
        create_item("observation", "Release notes revision 8", "e1", distinct_from=distinct),
        create_item("observation", "Release notes summary", "e1"),
    ]
    receipt = apply(driver, world_id, ops, evidence)
    second, summary = created_id(receipt, nth=0), created_id(receipt, nth=1)
    return {**seeded, "first": first, "second": second, "summary": summary}


def test_a_distinct_pair_partner_is_left_out(driver, revisions):
    query = WorldQuery(driver, revisions["world_id"])
    kind = revisions["kinds"]["observation"]

    every = lexical_candidates(query, kind, "Release notes revision 9")
    kept = lexical_candidates(
        query, kind, "Release notes revision 9", exclude_distinct_from=revisions["second"]
    )

    assert {candidate.item_id for candidate in every} == {
        revisions["first"],
        revisions["second"],
        revisions["summary"],
    }
    assert [candidate.item_id for candidate in kept] == [revisions["summary"]]


def test_without_a_pair_only_the_item_itself_is_left_out(driver, revisions):
    query = WorldQuery(driver, revisions["world_id"])
    kind = revisions["kinds"]["observation"]

    kept = lexical_candidates(
        query, kind, "Release notes revision 9", exclude_distinct_from=revisions["summary"]
    )

    assert {candidate.item_id for candidate in kept} == {revisions["first"], revisions["second"]}


def test_the_resolver_reads_a_world_tx_like_a_world_query(driver, seeded, chat):
    world_id = seeded["world_id"]
    task = seeded["kinds"]["task"]
    op = create_item("task", "Book the venue", "e1", status="open", recheck_by=RECHECK)
    item_id = created_id(apply(driver, world_id, [op], [observation(chat, "e1", 1)]))

    def work(tx: WorldTx) -> tuple[list[Candidate], Registry]:
        return lexical_candidates(tx, task, "Book a venue"), Registry.load(tx)

    found, registry = WorldWriter(driver, world_id).transaction(work)

    assert found == [Candidate(item_id, 0.647, False)]
    assert found == lexical_candidates(WorldQuery(driver, world_id), task, "Book a venue")
    read_back = registry_of(driver, world_id)
    assert (registry.kinds, registry.meanings) == (read_back.kinds, read_back.meanings)
    assert sorted(row["id"] for row in registry.aliases) == sorted(
        row["id"] for row in read_back.aliases
    )


def test_a_blank_phrase_is_a_shape_fault_before_the_resolver(driver, seeded, chat):
    # Blank text is refused by the shape check (rule type); S34's rule is for a phrase that is
    # not blank but normalises to nothing.
    op = create_item("  ", "The grey hull", "e1")

    refusal = refusal_of(driver, seeded["world_id"], [op], [observation(chat, "e1", 2)])

    assert (refusal.code, refusal.rule) == ("invalid_arguments", "type")
