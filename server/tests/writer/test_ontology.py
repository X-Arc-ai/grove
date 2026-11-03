"""D8's floor through the writer (lane ontology): synonyms, direction, a new relation that earns its
place, the two recorded judgments, the declared fields, the valve and the change log.

The judgments are recorded through the real judgments writer, against planted model calls, so the
gate reads exactly what the model-call layer stores. Every name here is invented.
"""

import json
from datetime import timedelta
from typing import Any

import pytest

from grove.store.connection import DATABASE
from grove.store.normalise import normalise_phrase
from grove.store.query import WorldQuery
from grove.writer import BatchDeclined, render_receipt
from grove.writer.judgments import JudgmentRow, record_judgments, store_subject_value
from grove.writer.ontology import (
    EARNING_BAR,
    SAFER_HORIZON_DAYS,
    field_fit_value,
    same_word_subject,
    same_word_value,
)
from grove.writer.registry import Registry
from grove.writer.resolver import resolve_link_phrase
from grove.writer.testing import plant_node
from tests.writer.helpers import (
    CURATOR,
    LEARNER,
    PERSON,
    T0,
    Caller,
    apply,
    at_minute,
    count_rows,
    create_item,
    create_link,
    iso,
    make_source,
    new_op_id,
    observation,
    read,
    supports,
)

SPARK = "muse-spark-1.3"
GLIMMER = "muse-glimmer-30b"
RECHECK = iso(T0 + timedelta(days=30))
CONNECTOR = Caller(
    kind="consolidation", scopes=frozenset({"grove.read", "grove.propose"}), actor="agent:connector"
)
GODPARENT = "is godparent of"
PERMANENT_PERSON_TO_PERSON = {
    "from_kinds": {"kinds": ["person"]},
    "to_kinds": {"kinds": ["person"]},
    "direction": "from_to",
    "cardinality": "many",
    "carries_consequences": False,
    "temporal_class": "permanent",
}
NEIGHBOURS = [
    {"word": "related to", "distinction": "related to records a connection and no role in it"},
    {"word": "member of", "distinction": "member of joins a person to an organisation"},
]


# Builders ------------------------------------------------------------------------------------


def meaning_row(driver, world_id: str, name: str) -> dict[str, Any] | None:
    rows = read(driver, world_id, "MATCH (m:Meaning {name: $n}) RETURN m", n=name)
    return rows[0]["m"] if rows else None


def model_call(driver, world_id: str, model: str) -> str:
    return plant_node(driver, world_id, "ModelCall", model=model, job="second_opinion", at=T0)["id"]


def judged(
    driver,
    world_id: str,
    *,
    name: str = GODPARENT,
    word_type: str = "meaning",
    neighbour: str = "related to",
    verdicts: tuple[str, str] = ("different", "different"),
    models: tuple[str, str] = (SPARK, GLIMMER),
    correlated: bool = False,
    fields: dict[str, tuple[Any, str]] | None = None,
    reversed_: bool = False,
) -> list[str]:
    """Two same-word judgments, one per model call, and the second judge's field-fit verdicts:
    `fields` maps a field to (its declared value, the verdict)."""
    neighbour_row = meaning_row(driver, world_id, neighbour) if word_type == "meaning" else None
    neighbour_id = neighbour_row["id"] if neighbour_row else neighbour
    name_ref = store_subject_value(
        driver, world_id, json.dumps(same_word_value(word_type, name, reversed_))
    )
    subject = same_word_subject(name_ref, word_type, neighbour_id)
    ids: list[str] = []
    for index, (verdict, model) in enumerate(zip(verdicts, models, strict=True)):
        rows = [JudgmentRow("same_word", subject, verdict, correlated=correlated and index == 1)]
        if index == 1:
            for field_name, (value, fit) in (fields or {}).items():
                ref = store_subject_value(
                    driver,
                    world_id,
                    json.dumps(field_fit_value(word_type, name, field_name, value)),
                )
                rows.append(JudgmentRow("field_fit", ref, fit))
        ids += record_judgments(driver, world_id, model_call(driver, world_id, model), rows)
    return ids


def propose(ev_id: str, judgment_ids: list[str], **over: Any) -> dict[str, Any]:
    op = {
        "op": "propose_word",
        "op_id": new_op_id(),
        "word_type": "meaning",
        "name": GODPARENT,
        "description": "The first person stands as a godparent to the second.",
        "reason": "No meaning in the registry says that one person is a godparent of another.",
        "worked_example": {"ev_id": ev_id},
        "neighbours": NEIGHBOURS,
        "declared": PERMANENT_PERSON_TO_PERSON,
        "judgment_ids": judgment_ids,
    }
    op.update(over)
    return op


def people(ev_id: str) -> list[dict[str, Any]]:
    return [
        create_item("person", "Ilse Marrow", ev_id, temp_id="$ilse"),
        create_item("person", "Teodor Quill", ev_id, temp_id="$teo"),
    ]


def mint_godparent(driver, world_id: str, chat: dict[str, str], **judge: Any):
    ids = judged(driver, world_id, fields={"temporal_class": ("permanent", "fits")}, **judge)
    ops = [*people("e1"), propose("e1", ids), create_link("$ilse", GODPARENT, "$teo", "e1")]
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    return receipt, ids


def godparent_link(driver, world_id: str) -> dict[str, Any]:
    meaning = meaning_row(driver, world_id, GODPARENT)
    rows = read(
        driver, world_id, "MATCH (:Item)-[l:LINK {meaning: $m}]->(:Item) RETURN l", m=meaning["id"]
    )
    assert len(rows) == 1
    return rows[0]["l"]


def refused(driver, world_id, ops, evidence, caller=LEARNER) -> list:
    with pytest.raises(BatchDeclined) as declined:
        apply(driver, world_id, ops, evidence, caller=caller)
    return declined.value.refusals


def meanings_count(driver, world_id: str) -> int:
    return read(driver, world_id, "MATCH (m:Meaning) RETURN count(m) AS c")[0]["c"]


# Synonyms and direction ----------------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["relates to", "related_to", "is related to", "connected with"])
def test_a_synonym_of_the_connection_meaning_mints_nothing_and_writes_with_it(
    driver, seeded, chat, phrase
):
    world_id = seeded["world_id"]
    before = meanings_count(driver, world_id)
    generic = next(
        row
        for row in Registry.load(WorldQuery(driver, world_id)).meanings.values()
        if row.get("generic")
    )

    word = resolve_link_phrase(Registry.load(WorldQuery(driver, world_id)), phrase)
    assert word is not None and word.meaning["id"] == generic["id"] and word.original_phrase is None

    ops = [
        create_item("person", "Ilse Marrow", "e1", temp_id="$ilse"),
        create_item("organisation", "Quill Rowing Club", "e1", temp_id="$club"),
        create_link("$ilse", phrase, "$club", "e1", recheck_by=RECHECK),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    links = read(driver, world_id, "MATCH (:Item)-[l:LINK]->(:Item) RETURN l")
    assert [link["l"]["meaning"] for link in links] == [generic["id"]]
    assert "original_phrase" not in links[0]["l"]
    assert meanings_count(driver, world_id) == before

    # Reuse first: the same phrase proposed as a new word is refused, so it can never be minted.
    refusals = refused(
        driver,
        world_id,
        [propose("e2", judged(driver, world_id, name=phrase), name=phrase)],
        [observation(chat, "e2", 2)],
    )
    assert [(r.code, r.rule) for r in refusals] == [("invalid_arguments", "word_exists")]
    assert meanings_count(driver, world_id) == before


def test_a_reversed_phrase_swaps_the_endpoints_and_mints_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    before = meanings_count(driver, world_id)
    owned_by = next(
        row
        for row in Registry.load(WorldQuery(driver, world_id)).meanings.values()
        if any(
            alias.get("normalised_phrase") == "owns"
            and alias.get("reversed")
            and alias["target_id"] == row["id"]
            for alias in Registry.load(WorldQuery(driver, world_id)).aliases
        )
    )
    ops = [
        create_item("person", "Ilse Marrow", "e1", temp_id="$ilse"),
        create_item(
            "plan",
            "Replant the orchard",
            "e1",
            temp_id="$plan",
            status="proposed",
            recheck_by=RECHECK,
        ),
        create_link("$ilse", "owns", "$plan", "e1", recheck_by=RECHECK),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    link = read(
        driver, world_id, "MATCH (a:Item)-[l:LINK]->(b:Item) RETURN a.title AS a, b.title AS b, l"
    )[0]
    assert (link["a"], link["b"]) == ("Replant the orchard", "Ilse Marrow")
    assert link["l"]["meaning"] == owned_by["id"]

    refusals = refused(
        driver,
        world_id,
        [propose("e2", judged(driver, world_id, name="owns"), name="owns")],
        [observation(chat, "e2", 2)],
    )
    assert [(r.code, r.rule) for r in refusals] == [("invalid_arguments", "word_exists")]
    assert meanings_count(driver, world_id) == before


# A new relation earns its place ---------------------------------------------------------------


def test_a_new_relation_is_minted_provisional_with_its_reason_example_ends_and_neighbours(
    driver, seeded, chat
):
    world_id = seeded["world_id"]
    receipt, judgment_ids = mint_godparent(driver, world_id, chat)

    row = meaning_row(driver, world_id, GODPARENT)
    registry = Registry.load(WorldQuery(driver, world_id))
    person = next(kind for kind in registry.kinds.values() if kind["name"] == "person")
    assert row["status"] == "provisional" and row["origin"] == "minted"
    assert row["minted_by"] == LEARNER.actor and row["primitive"] is False
    assert json.loads(row["from_kinds"]) == {"kinds": [person["id"]], "classes": []}
    assert json.loads(row["to_kinds"]) == {"kinds": [person["id"]], "classes": []}
    assert row["temporal_class"] == "permanent" and row["carries_consequences"] is False
    assert "disputed_fields" not in row

    texts = {
        value["id"]: value["value"]
        for value in (r["v"] for r in read(driver, world_id, "MATCH (v:HistoryValue) RETURN v"))
    }
    assert texts[row["reason"]].startswith("No meaning in the registry")
    assert texts[row["description"]].startswith("The first person")
    neighbours = json.loads(row["nearest_neighbours_at_mint"])
    named = {meaning_row(driver, world_id, n["word"])["id"] for n in NEIGHBOURS}
    assert {entry["id"] for entry in neighbours} == named
    assert {texts[entry["distinction"]] for entry in neighbours} == {
        n["distinction"] for n in NEIGHBOURS
    }

    link = godparent_link(driver, world_id)
    evidence = read(
        driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN e.id AS id", id=link["id"]
    )
    assert row["worked_example"] == evidence[0]["id"]
    assert row["uses"] == 1 and row["distinct_episodes"] == 1

    changes = read(driver, world_id, "MATCH (c:OntologyChange) RETURN c")
    assert len(changes) == 1
    change = changes[0]["c"]
    assert change["operation"] == "propose" and change["actor"] == LEARNER.actor
    assert json.loads(change["inverse_operation"]) == {
        "operation": "retire",
        "table": "meanings",
        "id": row["id"],
    }
    assert sorted(change["judgment_ids"]) == sorted(judgment_ids)
    assert change["evidence_ids"] == [row["worked_example"]]
    assert json.loads(change["rows_after"])[0]["row"]["id"] == row["id"]
    assert change["version"] == 1

    text = render_receipt(driver, world_id, receipt)
    assert f'o mint meaning "{GODPARENT}" (provisional)' in text


def test_a_word_stays_provisional_through_restatements_of_one_episode_and_earns_at_three(
    driver, seeded, chat
):
    world_id = seeded["world_id"]
    mint_godparent(driver, world_id, chat)
    link = godparent_link(driver, world_id)

    # The same episode restated three times: three new messages of the one chat window.
    for line in (2, 3, 4):
        ev = f"e{line}"
        attach = {
            "op": "attach_evidence",
            "op_id": new_op_id(),
            "target": link["id"],
            "ev_id": ev,
            "polarity": "supports",
        }
        apply(driver, world_id, [attach], [observation(chat, ev, line)], caller=LEARNER)
    row = meaning_row(driver, world_id, GODPARENT)
    assert (row["status"], row["distinct_episodes"]) == ("provisional", 1)
    evidence = read(
        driver, world_id, "MATCH (e:Evidence {target_id: $id}) RETURN count(e) AS c", id=link["id"]
    )
    assert evidence[0]["c"] == 4

    # A second distinct episode: still provisional below the bar.
    second = make_source(
        driver, world_id, "whatsapp", ["a second invented chat about the christening"]
    )
    teo_ilse = read(
        driver,
        world_id,
        "MATCH (a:Item)-[l:LINK {id: $id}]->(b:Item) RETURN a.id AS a, b.id AS b",
        id=link["id"],
    )[0]
    restate = create_link(teo_ilse["a"], GODPARENT, teo_ilse["b"], "s1")
    apply(driver, world_id, [restate], [observation(second, "s1", 1)], caller=LEARNER)
    row = meaning_row(driver, world_id, GODPARENT)
    assert (row["status"], row["distinct_episodes"]) == ("provisional", 2)

    # The third distinct episode earns it, in the same transaction, with a change log row.
    third = make_source(
        driver, world_id, "notes_files_releases", ["an invented note about the godparents"]
    )
    receipt = apply(
        driver,
        world_id,
        [create_link(teo_ilse["a"], GODPARENT, teo_ilse["b"], "n1")],
        [observation(third, "n1", 1)],
        caller=LEARNER,
    )
    row = meaning_row(driver, world_id, GODPARENT)
    assert (row["status"], row["distinct_episodes"]) == ("canonical", EARNING_BAR)
    assert row["version"] == 2
    promotes = read(driver, world_id, "MATCH (c:OntologyChange {operation: 'promote'}) RETURN c")
    assert len(promotes) == 1 and promotes[0]["c"]["actor"] == "writer"
    assert json.loads(promotes[0]["c"]["inverse_operation"])["status"] == "provisional"
    assert f'o promote meaning "{GODPARENT}" (canonical)' in render_receipt(
        driver, world_id, receipt
    )


# The two recorded judgments --------------------------------------------------------------------


def test_two_judges_who_say_same_make_the_phrase_an_alias_and_mint_nothing(driver, seeded, chat):
    world_id = seeded["world_id"]
    before = meanings_count(driver, world_id)
    ids = judged(driver, world_id, name="is linked up with", verdicts=("same", "same"))
    ops = [
        create_item("person", "Ilse Marrow", "e1", temp_id="$ilse"),
        create_item("organisation", "Quill Rowing Club", "e1", temp_id="$club"),
        propose(
            "e1",
            ids,
            name="is linked up with",
            declared={**PERMANENT_PERSON_TO_PERSON, "to_kinds": {"any": True}},
        ),
        create_link("$ilse", "is linked up with", "$club", "e1", recheck_by=RECHECK),
    ]
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)

    assert meanings_count(driver, world_id) == before
    generic = meaning_row(driver, world_id, "related to")
    alias = read(
        driver,
        world_id,
        "MATCH (a:Alias {normalised_phrase: $p}) RETURN a",
        p=normalise_phrase("is linked up with"),
    )[0]["a"]
    assert (alias["target_id"], alias["status"], alias["origin"]) == (
        generic["id"],
        "canonical",
        "minted",
    )
    link = read(driver, world_id, "MATCH (:Item)-[l:LINK]->(:Item) RETURN l")[0]["l"]
    assert link["meaning"] == generic["id"] and alias["worked_example"]
    assert 'o alias "is linked up with" for meaning "related to"' in render_receipt(
        driver, world_id, receipt
    )


def test_judges_who_disagree_mint_the_word_provisional_rather_than_an_alias(driver, seeded, chat):
    world_id = seeded["world_id"]
    mint_godparent(driver, world_id, chat, verdicts=("same", "different"))
    assert meaning_row(driver, world_id, GODPARENT)["status"] == "provisional"
    assert not read(driver, world_id, "MATCH (a:Alias {phrase: $p}) RETURN a", p=GODPARENT)


@pytest.mark.parametrize(
    "case",
    [
        "none_cited",
        "one_model",
        "same_model_uncorrelated",
        "unrecorded",
        "other_word",
        "wrong_role",
    ],
)
def test_a_proposal_without_two_recorded_judgments_of_this_word_is_refused(
    driver, seeded, chat, case
):
    world_id = seeded["world_id"]
    if case == "none_cited":
        ids: list[str] = []
    elif case == "one_model":
        ids = judged(driver, world_id)[:1]
    elif case == "same_model_uncorrelated":
        ids = judged(driver, world_id, models=(SPARK, SPARK))
    elif case == "unrecorded":
        ids = ["00000000-0000-7000-8000-00000000abcd", *judged(driver, world_id)]
    elif case == "other_word":
        ids = judged(driver, world_id, name="is a neighbour of")
    else:
        call = model_call(driver, world_id, SPARK)
        name_ref = store_subject_value(
            driver, world_id, json.dumps(same_word_value("meaning", GODPARENT))
        )
        generic = meaning_row(driver, world_id, "related to")
        ids = record_judgments(
            driver,
            world_id,
            call,
            [
                JudgmentRow(
                    "first_judge", same_word_subject(name_ref, "meaning", generic["id"]), "same"
                )
            ],
        )
    before = count_rows(driver, world_id)
    refusals = refused(
        driver,
        world_id,
        [*people("e1"), propose("e1", ids), create_link("$ilse", GODPARENT, "$teo", "e1")],
        [observation(chat, "e1", 1)],
    )
    expected = {
        "none_cited": ("second_judge_required", "judgments_required"),
        "one_model": ("second_judge_required", "two_models"),
        "same_model_uncorrelated": ("second_judge_required", "two_models"),
        "unrecorded": ("unrecorded_verdict", "unrecorded_verdict"),
        "other_word": ("unrecorded_verdict", "judgment_subject"),
        "wrong_role": ("unrecorded_verdict", "judgment_role"),
    }[case]
    assert (refusals[0].code, refusals[0].rule) == expected
    assert meaning_row(driver, world_id, GODPARENT) is None
    after = count_rows(driver, world_id)
    assert after.get("Meaning") == before.get("Meaning") and after.get(
        "OntologyChange"
    ) == before.get("OntologyChange")


def test_a_correlated_second_judge_of_the_same_model_counts(driver, seeded, chat):
    world_id = seeded["world_id"]
    mint_godparent(driver, world_id, chat, models=(SPARK, SPARK), correlated=True)
    assert meaning_row(driver, world_id, GODPARENT)["status"] == "provisional"


# Declared fields -------------------------------------------------------------------------------


def test_a_disputed_or_unjudged_field_takes_the_safer_value(driver, seeded, chat):
    world_id = seeded["world_id"]
    declared = {
        **PERMANENT_PERSON_TO_PERSON,
        "carries_consequences": True,
        "downstream_end": "from",
        "temporal_class": "permanent",
    }
    ids = judged(driver, world_id, fields={"temporal_class": ("permanent", "does_not_fit")})
    ops = [
        *people("e1"),
        propose(
            "e1",
            ids,
            declared=declared,
            proposed_capability={"connector_type": "calendar", "capability": "confirms"},
        ),
        create_link("$ilse", GODPARENT, "$teo", "e1", recheck_by=RECHECK),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    row = meaning_row(driver, world_id, GODPARENT)
    assert (row["temporal_class"], row["default_horizon"]) == ("state", SAFER_HORIZON_DAYS)
    assert row["carries_consequences"] is False and "downstream_end" not in row
    assert json.loads(row["disputed_fields"]) == [
        "carries_consequences",
        "proposed_capability",
        "temporal_class",
    ]
    assert not read(
        driver, world_id, "MATCH (c:Capability {registry_id: $id}) RETURN c", id=row["id"]
    )


def test_a_capability_is_stored_only_once_the_second_model_accepts_it(driver, seeded, chat):
    world_id = seeded["world_id"]
    capability = {"connector_type": "calendar", "capability": "confirms"}
    ids = judged(
        driver,
        world_id,
        fields={
            "temporal_class": ("permanent", "fits"),
            "proposed_capability": (capability, "fits"),
        },
    )
    ops = [
        *people("e1"),
        propose("e1", ids, proposed_capability=capability),
        create_link("$ilse", GODPARENT, "$teo", "e1"),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    row = meaning_row(driver, world_id, GODPARENT)
    stored = read(
        driver, world_id, "MATCH (c:Capability {registry_id: $id}) RETURN c", id=row["id"]
    )
    assert [(c["c"]["connector_type"], c["c"]["capability"], c["c"]["origin"]) for c in stored] == [
        ("calendar", "confirms", "minted_with_a_word")
    ]
    assert "disputed_fields" not in row


def test_a_field_fit_verdict_on_another_declared_value_does_not_count(driver, seeded, chat):
    world_id = seeded["world_id"]
    ids = judged(driver, world_id, fields={"temporal_class": ("event", "fits")})
    refusals = refused(
        driver,
        world_id,
        [*people("e1"), propose("e1", ids), create_link("$ilse", GODPARENT, "$teo", "e1")],
        [observation(chat, "e1", 1)],
    )
    assert (refusals[0].code, refusals[0].rule) == ("unrecorded_verdict", "judgment_subject")


@pytest.mark.parametrize(
    "vocabulary, rule",
    [
        ([{"status": "pencilled in", "role": "maybe"}], "unmapped_status"),
        ([{"status": "pencilled in"}], "unmapped_status"),
        ([], "no_vocabulary"),
        (
            [{"status": "open", "role": "open"}, {"status": "Open", "role": "active"}],
            "status_named_twice",
        ),
    ],
)
def test_a_kind_whose_vocabulary_has_an_unmapped_status_is_refused(
    driver, seeded, chat, vocabulary, rule
):
    world_id = seeded["world_id"]
    ids = judged(driver, world_id, name="errand", word_type="kind", neighbour="task")
    op = propose(
        "e1",
        ids,
        word_type="kind",
        name="errand",
        description="A small piece of work outside the home.",
        neighbours=[
            {"word": "task", "distinction": "a task has no place outside the home"},
            {"word": "plan", "distinction": "a plan is larger than one errand"},
        ],
        declared={"lifecycle_class": "work_with_state", "status_vocabulary": vocabulary},
    )
    refusals = refused(driver, world_id, [op], [observation(chat, "e1", 1)])
    assert (refusals[0].code, refusals[0].rule) == ("unmapped_status", rule)


def test_a_new_kind_is_usable_at_once_and_its_statuses_keep_their_roles(driver, seeded, chat):
    world_id = seeded["world_id"]
    registry = Registry.load(WorldQuery(driver, world_id))
    task = next(kind for kind in registry.kinds.values() if kind["name"] == "task")
    name_ref = store_subject_value(driver, world_id, json.dumps(same_word_value("kind", "errand")))
    subject = same_word_subject(name_ref, "kind", task["id"])
    vocabulary = [{"status": "to do", "role": "open"}, {"status": "run", "role": "done"}]
    ids = []
    for index, model in enumerate((SPARK, GLIMMER)):
        rows = [JudgmentRow("same_word", subject, "different")]
        if index:
            ref = store_subject_value(
                driver,
                world_id,
                json.dumps(field_fit_value("kind", "errand", "status_vocabulary", vocabulary)),
            )
            rows.append(JudgmentRow("field_fit", ref, "fits"))
        ids += record_judgments(driver, world_id, model_call(driver, world_id, model), rows)
    op = propose(
        "e1",
        ids,
        word_type="kind",
        name="errand",
        description="A small piece of work outside the home.",
        neighbours=[
            {"word": "task", "distinction": "a task has no place outside the home"},
            {"word": "plan", "distinction": "a plan is larger than one errand"},
        ],
        declared={"lifecycle_class": "work_with_state", "status_vocabulary": vocabulary},
    )
    ops = [
        op,
        create_item(
            "errand", "Collect the pear saplings", "e1", status="to do", recheck_by=RECHECK
        ),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    item = read(driver, world_id, "MATCH (i:Item {title: 'Collect the pear saplings'}) RETURN i")[
        0
    ]["i"]
    assert (item["status"], item["status_role"]) == ("to do", "open")
    kind = read(driver, world_id, "MATCH (k:Kind {name: 'errand'}) RETURN k")[0]["k"]
    assert (kind["status"], kind["uses"], kind["distinct_episodes"]) == ("provisional", 1, 1)


# The valve and the worked example ------------------------------------------------------------


def test_a_batch_mints_at_most_three_words(driver, seeded, chat):
    world_id = seeded["world_id"]
    names = ["is godparent of", "is godchild of", "is a sponsor of", "is a patron of"]
    ops = [*people("e1")]
    for name in names:
        ops.append(propose("e1", judged(driver, world_id, name=name), name=name))
        # No field-fit verdict: each word falls to the safer state class, which needs a re-check.
        ops.append(create_link("$ilse", name, "$teo", "e1", recheck_by=RECHECK))
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])
    assert [(r.op_id, r.rule) for r in refusals] == [
        (ops[8]["op_id"], "mint_valve"),
        (ops[9]["op_id"], "unknown_meaning"),
    ]
    assert meaning_row(driver, world_id, "is godparent of") is None


def test_a_worked_example_must_be_the_evidence_of_a_use_in_the_batch(driver, seeded, chat):
    world_id = seeded["world_id"]
    ids = judged(driver, world_id, fields={"temporal_class": ("permanent", "fits")})
    ops = [*people("e1"), propose("e2", ids), create_link("$ilse", GODPARENT, "$teo", "e1")]
    refusals = refused(
        driver, world_id, ops, [observation(chat, "e1", 1), observation(chat, "e2", 2)]
    )
    assert [(r.op_id, r.rule) for r in refusals] == [(ops[2]["op_id"], "worked_example_unused")]


@pytest.mark.parametrize(
    "neighbours, rule",
    [
        ([], "neighbours_required"),
        ([NEIGHBOURS[0]], "neighbours_required"),
        ([NEIGHBOURS[0], {"word": "related to", "distinction": "again"}], "neighbour_twice"),
        ([NEIGHBOURS[0], {"word": "is unheard of", "distinction": "none"}], "neighbour_unknown"),
        ([NEIGHBOURS[0], {"word": "member of", "distinction": "   "}], "distinction_required"),
        (
            [NEIGHBOURS[0], {"word": "superseded by", "distinction": "a primitive"}],
            "neighbour_unknown",
        ),
    ],
)
def test_a_proposal_names_two_existing_neighbours_each_with_a_distinction(
    driver, seeded, chat, neighbours, rule
):
    world_id = seeded["world_id"]
    ops = [*people("e1"), propose("e1", judged(driver, world_id), neighbours=neighbours)]
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])
    assert (refusals[0].code, refusals[0].rule) == ("invalid_arguments", rule)


# Promotion by an operation -----------------------------------------------------------------------


def test_the_person_promotes_at_once_and_the_consolidation_agent_only_at_the_bar(
    driver, seeded, chat
):
    world_id = seeded["world_id"]
    mint_godparent(driver, world_id, chat)
    promote = {
        "op": "promote_word",
        "op_id": new_op_id(),
        "word_type": "meaning",
        "word": GODPARENT,
        "reason": "the bar",
    }
    refusals = refused(driver, world_id, [promote], [], caller=CONNECTOR)
    assert (refusals[0].code, refusals[0].rule) == ("invalid_arguments", "earning_bar")
    refusals = refused(driver, world_id, [{**promote, "op_id": new_op_id()}], [], caller=LEARNER)
    assert (refusals[0].code, refusals[0].rule) == ("scope_denied", "caller_kind")

    receipt = apply(
        driver,
        world_id,
        [{**promote, "op_id": new_op_id(), "reason": "the person uses this word on purpose"}],
        [],
        caller=PERSON,
    )
    assert meaning_row(driver, world_id, GODPARENT)["status"] == "canonical"
    assert "o promote" in render_receipt(driver, world_id, receipt)
    refusals = refused(driver, world_id, [{**promote, "op_id": new_op_id()}], [], caller=PERSON)
    assert refusals[0].rule == "already_canonical"


# Property names (D8 "How to test it") -----------------------------------------------------------


def property_proposal(name: str, judgment_ids: list[str], neighbours: list[dict]) -> dict:
    return propose(
        "e1",
        judgment_ids,
        word_type="property_name",
        kind="plan",
        name=name,
        description="Who holds the plan.",
        neighbours=neighbours,
        declared={"value_type": "text", "temporal_class": "permanent"},
    )


def plan_item(ev_id: str, title: str = "Replant the orchard", **properties: Any) -> dict:
    return create_item(
        "plan",
        title,
        ev_id,
        status="proposed",
        recheck_by=RECHECK,
        properties=properties,
    )


def plan_kind(driver, world_id: str) -> str:
    registry = Registry.load(WorldQuery(driver, world_id))
    return next(kind["id"] for kind in registry.kinds.values() if kind["name"] == "plan")


def fits(
    driver,
    world_id: str,
    word_type: str,
    name: str,
    field_name: str,
    value: Any,
    kind_id: str | None = None,
) -> list[str]:
    """One field-fit verdict of the second judge; `kind_id` binds a property name's kind."""
    value_text = json.dumps(field_fit_value(word_type, name, field_name, value, kind_id))
    ref = store_subject_value(driver, world_id, value_text)
    call = model_call(driver, world_id, GLIMMER)
    return record_judgments(driver, world_id, call, [JudgmentRow("field_fit", ref, "fits")])


def mint_owner(driver, world_id: str, chat: dict[str, str]) -> dict[str, Any]:
    """The first property name of the plan kind: no neighbour, so only its field is judged."""
    ids = fits(
        driver,
        world_id,
        "property_name",
        "owner",
        "temporal_class",
        "permanent",
        plan_kind(driver, world_id),
    )
    ops = [property_proposal("owner", ids, []), plan_item("e1", owner="the orchard group")]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    return read(driver, world_id, "MATCH (p:PropertyName {name: 'owner'}) RETURN p")[0]["p"]


def test_an_unjudged_field_of_a_first_property_name_falls_to_state_and_fits_no_plan(
    driver, seeded, chat
):
    world_id = seeded["world_id"]
    ops = [property_proposal("owner", [], []), plan_item("e1", owner="the orchard group")]
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])
    assert ("state_property_on_item", "state_property_on_item") in [
        (r.code, r.rule) for r in refusals
    ]


def test_owner_owned_by_and_owner_name_resolve_to_one_property_name(driver, seeded, chat):
    world_id = seeded["world_id"]
    owner = mint_owner(driver, world_id, chat)
    assert (owner["status"], owner["temporal_class"], owner["value_type"]) == (
        "provisional",
        "permanent",
        "text",
    )

    plan_kind_id = plan_kind(driver, world_id)
    titles = {2: "Mend the hedge by the lane", 3: "Paint the barn door"}
    for line, phrase in ((2, "owned_by"), (3, "owner_name")):
        ev = f"e{line}"
        name_ref = store_subject_value(
            driver, world_id, json.dumps(same_word_value("property_name", phrase))
        )
        subject = same_word_subject(name_ref, "property_name", owner["id"])
        ids = []
        for model in (SPARK, GLIMMER):
            call = model_call(driver, world_id, model)
            ids += record_judgments(
                driver, world_id, call, [JudgmentRow("same_word", subject, "same")]
            )
        ids += fits(
            driver, world_id, "property_name", phrase, "temporal_class", "permanent", plan_kind_id
        )
        op = property_proposal(phrase, ids, [{"word": "owner", "distinction": "none found"}])
        op["worked_example"] = {"ev_id": ev}
        item = create_item("plan", titles[line], ev, status="proposed", recheck_by=RECHECK)
        item["properties"] = {phrase: "the hedge group"}
        apply(driver, world_id, [op, item], [observation(chat, ev, line)], caller=LEARNER)

    names = read(driver, world_id, "MATCH (p:PropertyName) RETURN p.name AS name")
    assert names == [{"name": "owner"}]
    for phrase in ("owner", "owned_by", "owner_name", "Owner Name"):
        found = registry_of(driver, world_id).property_name(plan_kind_id, phrase)
        assert found["id"] == owner["id"]
    stored = read(
        driver, world_id, "MATCH (i:Item {title: 'Paint the barn door'}) RETURN i.properties AS p"
    )
    assert json.loads(stored[0]["p"]) == {"owner": "the hedge group"}


def registry_of(driver, world_id: str) -> Registry:
    return Registry.load(WorldQuery(driver, world_id))


def test_a_property_whose_value_names_an_existing_item_is_refused_for_a_link(driver, seeded, chat):
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    for value in ("Ilse Marrow", "ilse marrow"):
        ops = [
            create_item("person", "Ilse Marrow", "e2"),
            plan_item("e2", "Prune the pear trees", owner=value),
        ]
        refusals = refused(driver, world_id, ops, [observation(chat, "e2", 2)])
        assert [(r.code, r.rule) for r in refusals] == [("invalid_arguments", "value_names_item")]
    # A value that names no item stays a value.
    apply(
        driver,
        world_id,
        [plan_item("e3", "Fence the far paddock", owner="the orchard group")],
        [observation(chat, "e3", 3)],
        caller=LEARNER,
    )


def test_a_retired_item_alias_no_longer_counts_at_the_near_duplicate_gate(driver, seeded, chat):
    world_id = seeded["world_id"]
    receipt = apply(
        driver,
        world_id,
        [create_item("place", "The north meadow", "e1")],
        [observation(chat, "e1", 1)],
    )
    meadow = next(line.entity_id for line in receipt.lines if line.symbol == "+")
    for status, gated in (("retired", False), ("canonical", True)):
        alias = plant_node(
            driver,
            world_id,
            "Alias",
            phrase=f"Hollow field {status}",
            normalised_phrase=normalise_phrase(f"Hollow field {status}"),
            target_type="item",
            target_id=meadow,
            reversed=False,
            status=status,
        )
        op = create_item("place", alias["phrase"], "e2")
        if gated:
            refusals = refused(driver, world_id, [op], [observation(chat, "e2", 2)], caller=PERSON)
            assert refusals[0].code == "near_duplicate"
        else:
            apply(driver, world_id, [op], [observation(chat, "e2", 2)], caller=PERSON)


# Regressions of the first R4 verifier (blockers 1 and 2) ----------------------------------------


def test_restating_one_episode_by_ending_and_reattaching_never_earns_a_word(driver, seeded, chat):
    """Blocker 2: a correction that ends a row and attaches another from the same conversation adds
    no distinct episode, because the ended row still shows the episode spoke for the word."""
    world_id = seeded["world_id"]
    mint_godparent(driver, world_id, chat)
    link = godparent_link(driver, world_id)
    for line in (2, 3, 4):
        active = read(
            driver,
            world_id,
            "MATCH (e:Evidence {target_id: $id, active: true}) RETURN e.id AS id",
            id=link["id"],
        )
        ev = f"e{line}"
        ops = [
            {
                "op": "end_evidence",
                "op_id": new_op_id(),
                "evidence": active[0]["id"],
                "end_reason": "correction",
            },
            {
                "op": "attach_evidence",
                "op_id": new_op_id(),
                "target": link["id"],
                "ev_id": ev,
                "polarity": "supports",
            },
        ]
        apply(driver, world_id, ops, [observation(chat, ev, line)], caller=CURATOR)
    row = meaning_row(driver, world_id, GODPARENT)
    assert (row["status"], row["distinct_episodes"]) == ("provisional", 1)
    episodes = read(
        driver,
        world_id,
        "MATCH (e:Evidence {target_id: $id}) RETURN count(DISTINCT e.episode_id) AS c",
        id=link["id"],
    )
    assert episodes[0]["c"] == 1


PREREQUISITE = "is a prerequisite for"
ANY_TO_WORK = {
    **PERMANENT_PERSON_TO_PERSON,
    "from_kinds": {"any": True},
    "to_kinds": {"any": True},
    "temporal_class": "state",
    "default_horizon": 30,
    "carries_consequences": True,
    "downstream_end": "to",
}


def tasks(ev_id: str) -> list[dict[str, Any]]:
    return [
        create_item(
            "task", "Fix the barn roof", ev_id, temp_id="$roof", status="open", recheck_by=RECHECK
        ),
        create_item(
            "task", "Store the hay", ev_id, temp_id="$hay", status="open", recheck_by=RECHECK
        ),
    ]


def test_a_phrase_that_reads_its_neighbour_from_the_other_end_becomes_a_reversed_alias(
    driver, seeded, chat
):
    """Blocker 1: the reading is stated and judged, and the alias carries it, so the link is stored
    the right way round: the roof is needed by the hay, so the hay depends on the roof."""
    world_id = seeded["world_id"]
    depends = meaning_row(driver, world_id, "depends on")
    neighbours = [
        {"word": "depends on", "distinction": "none found"},
        {"word": "blocks", "distinction": "blocks stops work, a prerequisite only comes first"},
    ]
    ids = judged(
        driver,
        world_id,
        name=PREREQUISITE,
        neighbour="depends on",
        verdicts=("same", "same"),
        reversed_=True,
    )
    ops = [
        *tasks("e1"),
        propose(
            "e1", ids, name=PREREQUISITE, neighbours=neighbours, declared=ANY_TO_WORK, reversed=True
        ),
        create_link("$roof", PREREQUISITE, "$hay", "e1", recheck_by=RECHECK),
    ]
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    alias = read(driver, world_id, "MATCH (a:Alias {phrase: $p}) RETURN a", p=PREREQUISITE)[0]["a"]
    assert (alias["target_id"], alias["reversed"]) == (depends["id"], True)
    link = read(
        driver,
        world_id,
        "MATCH (a:Item)-[l:LINK]->(b:Item) RETURN a.title AS a, b.title AS b, l.meaning AS m",
    )
    assert link == [{"a": "Store the hay", "b": "Fix the barn roof", "m": depends["id"]}]
    change = read(driver, world_id, "MATCH (c:OntologyChange) RETURN c")[0]["c"]
    assert change["operation"] == "propose" and sorted(change["judgment_ids"]) == sorted(ids)
    assert json.loads(change["rows_after"])[0]["table"] == "aliases"
    assert change["evidence_ids"] == [alias["worked_example"]]
    assert f'o alias "{PREREQUISITE}" for meaning "depends on"' in render_receipt(
        driver, world_id, receipt
    )


def test_a_verdict_on_one_reading_never_counts_for_the_other(driver, seeded, chat):
    world_id = seeded["world_id"]
    neighbours = [
        {"word": "depends on", "distinction": "none found"},
        {"word": "blocks", "distinction": "blocks stops work, a prerequisite only comes first"},
    ]
    unreversed = judged(
        driver, world_id, name=PREREQUISITE, neighbour="depends on", verdicts=("same", "same")
    )
    ops = [
        *tasks("e1"),
        propose(
            "e1",
            unreversed,
            name=PREREQUISITE,
            neighbours=neighbours,
            declared=ANY_TO_WORK,
            reversed=True,
        ),
        create_link("$roof", PREREQUISITE, "$hay", "e1", recheck_by=RECHECK),
    ]
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])
    assert ("unrecorded_verdict", "judgment_subject") in [(r.code, r.rule) for r in refusals]
    assert not read(driver, world_id, "MATCH (a:Alias {phrase: $p}) RETURN a", p=PREREQUISITE)


def test_an_alias_whose_declared_ends_fit_only_the_other_way_round_is_refused(driver, seeded, chat):
    """Blocker 1, the mechanical guard: "possesses" declared from a person to a plan cannot be an
    unreversed alias of "owned by", whose second end is a person or an organisation."""
    world_id = seeded["world_id"]
    neighbours = [
        {"word": "owned by", "distinction": "none found"},
        {"word": "assigned to", "distinction": "assigned to gives work, not ownership"},
    ]
    declared = {
        **PERMANENT_PERSON_TO_PERSON,
        "from_kinds": {"kinds": ["person"]},
        "to_kinds": {"kinds": ["plan"]},
    }
    ids = judged(
        driver, world_id, name="possesses", neighbour="owned by", verdicts=("same", "same")
    )
    ops = [
        create_item("person", "Ilse Marrow", "e1", temp_id="$ilse"),
        create_item(
            "plan",
            "Replant the orchard",
            "e1",
            temp_id="$plan",
            status="proposed",
            recheck_by=RECHECK,
        ),
        propose("e1", ids, name="possesses", neighbours=neighbours, declared=declared),
        create_link("$ilse", "possesses", "$plan", "e1", recheck_by=RECHECK),
    ]
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])
    assert ("invalid_arguments", "alias_direction") in [(r.code, r.rule) for r in refusals]
    assert not read(driver, world_id, "MATCH (a:Alias {phrase: 'possesses'}) RETURN a")


def test_a_reading_from_the_other_end_is_for_meanings_and_texts_have_a_length(driver, seeded, chat):
    world_id = seeded["world_id"]
    for over, rule in (
        ({"word_type": "kind", "reversed": True}, "type"),
        ({"name": "x" * 121}, "too_long"),
        ({"reason": "y" * 2001}, "too_long"),
    ):
        op = propose("e1", judged(driver, world_id), **over)
        refusals = refused(driver, world_id, [*people("e1"), op], [observation(chat, "e1", 1)])
        assert refusals[0].rule == rule, (over, refusals)


# Regressions of the reviewer's probes on 8dd77d5 ---------------------------------------------


def prop_row(driver, world_id: str, name: str) -> dict[str, Any]:
    return read(driver, world_id, "MATCH (p:PropertyName {name: $n}) RETURN p", n=name)[0]["p"]


def test_a_property_name_earns_only_from_the_writes_that_set_it(driver, seeded, chat):
    """Probe PD1: evidence that confirms the item but writes no property is no use of the property
    name, so two such episodes leave it at its one episode."""
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    plan = read(driver, world_id, "MATCH (i:Item {title: 'Replant the orchard'}) RETURN i.id AS id")
    for n in (2, 3):
        src = make_source(driver, world_id, "whatsapp", [f"invented chat {n}: still proposed"])
        attach = {
            "op": "attach_evidence",
            "op_id": new_op_id(),
            "target": plan[0]["id"],
            "ev_id": f"s{n}",
            "polarity": "supports",
        }
        apply(driver, world_id, [attach], [observation(src, f"s{n}", 1)], caller=LEARNER)
    owner = prop_row(driver, world_id, "owner")
    assert (owner["status"], owner["distinct_episodes"], owner["uses"]) == ("provisional", 1, 1)
    kind = read(
        driver, world_id, "MATCH (k:Kind {id: $id}) RETURN k", id=plan_kind(driver, world_id)
    )
    assert kind[0]["k"]["distinct_episodes"] == 3


def test_an_update_counts_and_checks_only_the_properties_it_writes(driver, seeded, chat):
    """Probe PD2: an update of one property adds no use to another, and a stored value that a
    later item's title matches never blocks an update of another property."""
    world_id = seeded["world_id"]
    kind_id = plan_kind(driver, world_id)
    owner = mint_owner(driver, world_id, chat)
    ids = judged(
        driver,
        world_id,
        name="steward",
        word_type="property_name",
        neighbour=owner["id"],
    )
    ids += fits(
        driver, world_id, "property_name", "steward", "temporal_class", "permanent", kind_id
    )
    op = property_proposal(
        "steward", ids, [{"word": "owner", "distinction": "the owner holds it, a steward runs it"}]
    )
    op["worked_example"] = {"ev_id": "e5"}
    item = plan_item("e5", "Clear the ditch", owner="the ditch crew", steward="the parish")
    apply(driver, world_id, [op, item], [observation(chat, "e5", 5)], caller=LEARNER)
    before = prop_row(driver, world_id, "owner")["uses"]

    def update(ev: str, line: int, value: str) -> None:
        ditch = read(
            driver,
            world_id,
            "MATCH (i:Item {title: 'Clear the ditch'}) RETURN i.id AS id, i.version AS v",
        )[0]
        op = {
            "op": "update_item",
            "op_id": new_op_id(),
            "item": ditch["id"],
            "version": ditch["v"],
            "fields": {"properties": {"steward": value}},
            "evidence_refs": supports(ev),
            "reason": "the steward changed",
        }
        apply(driver, world_id, [op], [observation(chat, ev, line)], caller=LEARNER)

    update("e6", 6, "the vestry")
    assert prop_row(driver, world_id, "owner")["uses"] == before
    assert prop_row(driver, world_id, "steward")["uses"] == 2

    org = create_item("organisation", "The Ditch Crew", "e7", status="active")
    apply(driver, world_id, [org], [observation(chat, "e7", 7)], caller=LEARNER)
    update("e8", 8, "the churchwardens")
    # A value the update sends is still checked.
    with pytest.raises(BatchDeclined) as declined:
        update("e9", 9, "The Ditch Crew")
    assert [r.rule for r in declined.value.refusals] == ["value_names_item"]


@pytest.mark.parametrize("name", ["is superseded by", "are corrected by", "is affected by"])
def test_a_primitive_name_after_a_copula_is_a_primitive_word(driver, seeded, chat, name):
    """Probe PD3."""
    world_id = seeded["world_id"]
    ops = [*people("e1"), propose("e1", judged(driver, world_id, name=name), name=name)]
    refusals = refused(driver, world_id, ops, [observation(chat, "e1", 1)])
    assert (refusals[0].code, refusals[0].rule) == ("invalid_arguments", "primitive_word")


def test_a_field_fit_verdict_of_one_kinds_property_never_counts_for_another_kind(
    driver, seeded, chat
):
    """Probe PD4."""
    world_id = seeded["world_id"]
    registry = Registry.load(WorldQuery(driver, world_id))
    task = next(kind["id"] for kind in registry.kinds.values() if kind["name"] == "task")
    ids = fits(
        driver,
        world_id,
        "property_name",
        "owner",
        "temporal_class",
        "permanent",
        plan_kind(driver, world_id),
    )
    op = property_proposal("owner", ids, [])
    op["kind"] = "task"
    item = create_item(
        "task",
        "Dig the pond",
        "e1",
        status="open",
        recheck_by=RECHECK,
        properties={"owner": "the pond group"},
    )
    refusals = refused(driver, world_id, [op, item], [observation(chat, "e1", 1)])
    assert ("unrecorded_verdict", "judgment_subject") in [(r.code, r.rule) for r in refusals]
    assert task


# Regressions of the reviewer of c19dbd9 ------------------------------------------------------


def _item_ref(driver, world_id: str, title: str) -> dict[str, Any]:
    return read(
        driver, world_id, "MATCH (i:Item {title: $t}) RETURN i.id AS id, i.version AS v", t=title
    )[0]


def _set_properties(driver, world_id, title, src, ev_id, at, properties) -> None:
    item = _item_ref(driver, world_id, title)
    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item["id"],
        "version": item["v"],
        "fields": {"properties": properties},
        "evidence_refs": supports(ev_id),
        "reason": "the steward changed",
    }
    apply(driver, world_id, [op], [observation(src, ev_id, 1, at=at)], caller=LEARNER)


def test_a_late_fact_on_one_property_earns_no_other_property_name(driver, seeded, chat):
    """Blocker 1: a late fact (history only) about `steward` writes `steward` alone. `owner`, which
    the as-of properties also hold, gains no use and no episode, and is never promoted."""
    world_id = seeded["world_id"]
    kind_id = plan_kind(driver, world_id)
    owner = mint_owner(driver, world_id, chat)
    ids = judged(driver, world_id, name="steward", word_type="property_name", neighbour=owner["id"])
    ids += fits(
        driver, world_id, "property_name", "steward", "temporal_class", "permanent", kind_id
    )
    op = property_proposal(
        "steward", ids, [{"word": "owner", "distinction": "the owner holds it, a steward runs it"}]
    )
    op["worked_example"] = {"ev_id": "e5"}
    item = plan_item("e5", "Clear the ditch", owner="the ditch crew", steward="the parish")
    apply(driver, world_id, [op, item], [observation(chat, "e5", 5)], caller=LEARNER)
    before = prop_row(driver, world_id, "owner")
    steward = prop_row(driver, world_id, "steward")

    first = make_source(driver, world_id, "whatsapp", ["invented: the vestry runs it now"])
    _set_properties(
        driver, world_id, "Clear the ditch", first, "s1", at_minute(20), {"steward": "the vestry"}
    )
    for n in (2, 3):
        src = make_source(driver, world_id, "whatsapp", [f"invented late chat {n}"])
        _set_properties(
            driver, world_id, "Clear the ditch", src, f"s{n}", at_minute(10), {"steward": f"v{n}"}
        )
    after = prop_row(driver, world_id, "owner")
    key = ("status", "distinct_episodes", "uses")
    assert [after[name] for name in key] == [before[name] for name in key]
    assert before["status"] == "provisional"
    earned = prop_row(driver, world_id, "steward")
    assert earned["distinct_episodes"] == steward["distinct_episodes"] + 3
    assert earned["status"] == "canonical"


def test_a_word_minted_earlier_in_the_batch_is_no_neighbour(driver, seeded, chat):
    """Blocker 2 in the writer: two first property names of a kind mint in one batch with no
    neighbour each, and a proposal cannot name a word of the same batch as its neighbour."""
    world_id = seeded["world_id"]
    kind_id = plan_kind(driver, world_id)
    owner = fits(driver, world_id, "property_name", "owner", "temporal_class", "permanent", kind_id)
    budget = fits(
        driver, world_id, "property_name", "budget note", "temporal_class", "permanent", kind_id
    )
    ops = [
        property_proposal("owner", owner, []),
        property_proposal("budget note", budget, []),
        plan_item("e1", owner="the orchard group", **{"budget note": "small"}),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    assert prop_row(driver, world_id, "budget note")["status"] == "provisional"

    holder = prop_row(driver, world_id, "owner")
    ids = judged(
        driver, world_id, name="steward", word_type="property_name", neighbour=holder["id"]
    )
    ids += fits(
        driver, world_id, "property_name", "steward", "temporal_class", "permanent", kind_id
    )
    steward = property_proposal(
        "steward",
        ids,
        [
            {"word": "owner", "distinction": "the owner holds it, a steward runs it"},
            {"word": "budget note", "distinction": "a person, not a sum"},
        ],
    )
    deputy = property_proposal(
        "deputy",
        [],
        [
            {"word": "steward", "distinction": "the deputy stands in"},
            {"word": "owner", "distinction": "the deputy holds nothing"},
        ],
    )
    refusals = refused(
        driver,
        world_id,
        [steward, deputy, plan_item("e1", "Clear the ditch", steward="the parish")],
        [observation(chat, "e1", 2)],
    )
    assert ("invalid_arguments", "neighbour_unknown", "steward") in [
        (r.code, r.rule, r.details.get("word")) for r in refusals
    ]


def test_a_state_property_name_on_a_kind_that_is_not_a_record_is_refused_at_the_mint(
    driver, seeded, chat
):
    """Blocker 3 in the writer: declared or settled, a state property name of a plan could sit on
    no plan, so the mint is refused, not the first item that uses it."""
    world_id = seeded["world_id"]
    state = propose(
        "e1",
        [],
        word_type="property_name",
        kind="plan",
        name="mood",
        description="How the plan feels now.",
        neighbours=[],
        declared={"value_type": "text", "temporal_class": "state", "default_horizon": 30},
    )
    refusals = refused(
        driver, world_id, [state, plan_item("e1", mood="calm")], [observation(chat, "e1", 1)]
    )
    assert [(r.code, r.rule) for r in refusals][0] == (
        "state_property_on_item",
        "state_property_on_item",
    )
    assert not read(driver, world_id, "MATCH (p:PropertyName) RETURN p")


def test_an_update_may_restate_a_value_equal_to_its_own_title(driver, seeded, chat):
    """Reviewer minor: the item that holds a value is never the item the value names, so create and
    update read the same."""
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    title = "Fence the far paddock"
    apply(
        driver,
        world_id,
        [plan_item("e2", title, owner=title)],
        [observation(chat, "e2", 2)],
        caller=LEARNER,
    )
    item = _item_ref(driver, world_id, title)
    op = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item["id"],
        "version": item["v"],
        "fields": {"properties": {"owner": title}},
        "evidence_refs": supports("e3"),
        "reason": "restated",
    }
    apply(driver, world_id, [op], [observation(chat, "e3", 3)], caller=LEARNER)


def test_a_capability_minted_with_a_word_has_its_change_entry_and_its_inverse(driver, seeded, chat):
    """Reviewer minor: the capability row is a registry row the proposal wrote, so a rebuild from
    history restores it and the change log row names it."""
    world_id = seeded["world_id"]
    capability = {"connector_type": "calendar", "capability": "confirms"}
    ids = judged(
        driver,
        world_id,
        fields={
            "temporal_class": ("permanent", "fits"),
            "proposed_capability": (capability, "fits"),
        },
    )
    ops = [
        *people("e1"),
        propose("e1", ids, proposed_capability=capability),
        create_link("$ilse", GODPARENT, "$teo", "e1"),
    ]
    receipt = apply(driver, world_id, ops, [observation(chat, "e1", 1)], caller=LEARNER)
    row = meaning_row(driver, world_id, GODPARENT)
    cap = read(driver, world_id, "MATCH (c:Capability {registry_id: $id}) RETURN c", id=row["id"])
    cap_id = cap[0]["c"]["id"]
    text = render_receipt(driver, world_id, receipt)
    assert f'o mint meaning "{GODPARENT}" (provisional)' in text
    assert f'o capability calendar confirms for meaning "{GODPARENT}"' in text
    entries = read(
        driver,
        world_id,
        "MATCH (e:ChangeEntry {entity_id: $id}) RETURN e.entity_type AS t, e.field AS f",
        id=cap_id,
    )
    assert entries == [{"t": "registry_row", "f": "row"}]
    change = read(driver, world_id, "MATCH (c:OntologyChange {operation: 'propose'}) RETURN c")
    after = json.loads(change[0]["c"]["rows_after"])
    assert sorted(entry["table"] for entry in after) == ["capabilities", "meanings"]
    inverse = json.loads(change[0]["c"]["inverse_operation"])
    assert inverse["with"] == [{"table": "capabilities", "id": cap_id}]


# Regressions of the R4 delta on d3ca3c4 (the earning ledger) --------------------------------


def _mint_steward(driver, world_id, src, ev_id, line, title, **properties) -> None:
    kind_id = plan_kind(driver, world_id)
    owner = prop_row(driver, world_id, "owner")
    ids = judged(driver, world_id, name="steward", word_type="property_name", neighbour=owner["id"])
    ids += fits(
        driver, world_id, "property_name", "steward", "temporal_class", "permanent", kind_id
    )
    op = property_proposal(
        "steward", ids, [{"word": "owner", "distinction": "the owner holds it, a steward runs it"}]
    )
    op["worked_example"] = {"ev_id": ev_id}
    item = plan_item(ev_id, title, **properties)
    apply(driver, world_id, [op, item], [observation(src, ev_id, line)], caller=LEARNER)


def test_one_episode_never_earns_a_property_name_by_moving_it_between_items(driver, seeded, chat):
    """One conversation sets steward on a plan, removes it, and sets it on the next plan, twice.
    The word keeps the episode it counted, so it stays at one episode, provisional."""
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    src = make_source(driver, world_id, "whatsapp", [f"invented line {n}" for n in range(12)])
    titles = ["Clear the ditch", "Mend the stile", "Prune the pear trees"]
    _mint_steward(driver, world_id, src, "s1", 1, titles[0], steward="the parish")
    line = 2
    for previous, following in zip(titles, titles[1:], strict=False):
        _set_properties(
            driver, world_id, previous, src, f"s{line}", at_minute(line), {"steward": None}
        )
        line += 1
        apply(
            driver,
            world_id,
            [plan_item(f"s{line}", following, steward="the parish")],
            [observation(src, f"s{line}", line)],
            caller=LEARNER,
        )
        line += 1
    steward = prop_row(driver, world_id, "steward")
    assert (steward["status"], steward["distinct_episodes"]) == ("provisional", 1)
    assert len(json.loads(steward["earning_episodes"])) == 1
    assert not read(driver, world_id, "MATCH (c:OntologyChange {operation: 'promote'}) RETURN c")


def test_each_episode_that_writes_a_property_name_counts_once(driver, seeded, chat):
    """The other direction: an episode that once confirmed a plan, which another episode later gave
    the property, still counts when it writes the property itself."""
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    first = make_source(driver, world_id, "whatsapp", ["invented: the parish runs the ditch"])
    _mint_steward(driver, world_id, first, "s1", 1, "Clear the ditch", steward="the parish")
    second = make_source(driver, world_id, "whatsapp", [f"invented two {n}" for n in range(5)])
    apply(
        driver,
        world_id,
        [plan_item("t1", "Mend the stile")],
        [observation(second, "t1", 1)],
        caller=LEARNER,
    )
    third = make_source(driver, world_id, "whatsapp", ["invented: the vestry runs the stile"])
    _set_properties(
        driver, world_id, "Mend the stile", third, "u1", at_minute(1), {"steward": "vestry"}
    )
    apply(
        driver,
        world_id,
        [plan_item("t2", "Prune the pear trees", steward="the orchard group")],
        [observation(second, "t2", 2)],
        caller=LEARNER,
    )
    steward = prop_row(driver, world_id, "steward")
    assert (steward["distinct_episodes"], steward["status"]) == (3, "canonical")


def test_evidence_attached_in_the_same_batch_is_no_use_of_a_property_name(driver, seeded, chat):
    """Only the operation that wrote a property speaks for its name: two attachments to the item in
    the same batch confirm the item, so they count for its kind and not for the property name."""
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    before = prop_row(driver, world_id, "owner")
    item = _item_ref(driver, world_id, "Replant the orchard")
    sources = {
        n: make_source(driver, world_id, "whatsapp", [f"invented chat {n}: about the orchard"])
        for n in (1, 2, 3)
    }
    update = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item["id"],
        "version": item["v"],
        "fields": {"properties": {"owner": "the vestry"}},
        "evidence_refs": supports("s1"),
        "reason": "the owner changed",
    }
    attaches = [
        {
            "op": "attach_evidence",
            "op_id": new_op_id(),
            "target": item["id"],
            "ev_id": f"s{n}",
            "polarity": "supports",
        }
        for n in (2, 3)
    ]
    apply(
        driver,
        world_id,
        [update, *attaches],
        [observation(sources[n], f"s{n}", 1) for n in (1, 2, 3)],
        caller=LEARNER,
    )
    after = prop_row(driver, world_id, "owner")
    assert (after["status"], after["distinct_episodes"], after["uses"]) == (
        "provisional",
        before["distinct_episodes"] + 1,
        before["uses"] + 1,
    )


def test_a_property_sent_as_null_at_create_is_no_use_of_its_name(driver, seeded, chat):
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    before = prop_row(driver, world_id, "owner")
    src = make_source(driver, world_id, "whatsapp", ["invented: a plan with no owner yet"])
    apply(
        driver,
        world_id,
        [plan_item("s1", "Fence the far paddock", owner=None)],
        [observation(src, "s1", 1)],
        caller=LEARNER,
    )
    after = prop_row(driver, world_id, "owner")
    assert (after["distinct_episodes"], after["uses"]) == (
        before["distinct_episodes"],
        before["uses"],
    )


def test_attachments_before_the_write_in_one_batch_are_no_use_of_its_property(driver, seeded, chat):
    """The other operation order: two chats attach to the item first, then a third chat's update
    writes owner, all in one batch. Only the update's own row speaks for the property name."""
    world_id = seeded["world_id"]
    mint_owner(driver, world_id, chat)
    before = prop_row(driver, world_id, "owner")
    item = _item_ref(driver, world_id, "Replant the orchard")
    sources = {
        n: make_source(driver, world_id, "whatsapp", [f"invented order chat {n}"])
        for n in (1, 2, 3)
    }
    attaches = [
        {
            "op": "attach_evidence",
            "op_id": new_op_id(),
            "target": item["id"],
            "ev_id": f"s{n}",
            "polarity": "supports",
        }
        for n in (2, 3)
    ]
    update = {
        "op": "update_item",
        "op_id": new_op_id(),
        "item": item["id"],
        "version": item["v"],
        "fields": {"properties": {"owner": "the vestry"}},
        "evidence_refs": supports("s1"),
        "reason": "the owner changed",
    }
    apply(
        driver,
        world_id,
        [*attaches, update],
        [observation(sources[n], f"s{n}", 1) for n in (1, 2, 3)],
        caller=LEARNER,
    )
    after = prop_row(driver, world_id, "owner")
    assert (after["status"], after["distinct_episodes"], after["uses"]) == (
        "provisional",
        before["distinct_episodes"] + 1,
        before["uses"] + 1,
    )


def test_a_ledger_that_is_not_a_list_holds_no_episode_and_never_stops_the_batch(
    driver, seeded, chat
):
    world_id = seeded["world_id"]
    owner = mint_owner(driver, world_id, chat)
    # The registry row is changed by hand: no writer path can store such a ledger.
    with driver.session(database=DATABASE) as session:
        session.run(
            "MATCH (p:PropertyName {id: $id, world_id: $w}) SET p.earning_episodes = '5'",
            id=owner["id"],
            w=world_id,
        ).consume()
    src = make_source(driver, world_id, "whatsapp", ["invented: the vestry"])
    _set_properties(
        driver, world_id, "Replant the orchard", src, "s1", at_minute(1), {"owner": "vestry"}
    )
    row = prop_row(driver, world_id, "owner")
    assert row["distinct_episodes"] == owner["distinct_episodes"] + 1
    assert len(json.loads(row["earning_episodes"])) == 1
