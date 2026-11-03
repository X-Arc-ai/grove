"""The two judges of a proposed word and the plan around them, with every provider faked by
httpx.MockTransport (D8 step 6, D15). Each test ends in the writer, so the recorded judgments are
proven by the gate that reads them, not only by their rows."""

import json
from datetime import timedelta

import httpx
import pytest

from grove import clock
from grove.clock import TestClock
from grove.models.errors import ProviderError
from grove.models.judge import FALLBACK_JOB, FIRST_JOB, SECOND_JOB, judge_word
from grove.models.layer import ModelLayer
from grove.ontology import fit_links, plan_words
from grove.store.query import WorldQuery
from grove.writer import apply_batch
from grove.writer.ontology import check_proposal
from grove.writer.registry import Registry
from tests.models.test_layer import NINE, FakeProvider, chat_doc, reply, settings, spark_doc
from tests.writer.helpers import (
    LEARNER,
    create_item,
    create_link,
    iso,
    new_op_id,
    observation,
    read,
)

GODPARENT = "is godparent of"
RECHECK = iso(NINE + timedelta(days=30))
RESPONSES = "https://api.meta.ai/v1/responses"
GLIMMER_CHAT = "https://glimmer.example.test/v1/chat/completions"


def proposal(name: str = GODPARENT, **over) -> dict:
    op = {
        "op_id": new_op_id(),
        "word_type": "meaning",
        "name": name,
        "description": "The first person stands as a godparent to the second.",
        "reason": "No meaning in the registry says that one person is a godparent of another.",
        "worked_example": {"ev_id": "e1"},
        "neighbours": [
            {"word": "related to", "distinction": "related to records no role"},
            {"word": "member of", "distinction": "member of joins a person to an organisation"},
        ],
        "declared": {
            "from_kinds": {"kinds": ["person"]},
            "to_kinds": {"kinds": ["person"]},
            "direction": "from_to",
            "cardinality": "many",
            "carries_consequences": False,
            "temporal_class": "permanent",
        },
        "example": "Ilse is Teodor's godmother since the christening.",
    }
    op.update(over)
    return op


def first(verdict: str) -> tuple:
    return reply(spark_doc(json.dumps({"same_word": {"verdict": verdict, "confidence": 0.8}})))


def second(verdict: str, fit: str = "fits", fields=("temporal_class",)) -> dict:
    return {
        "same_word": {"verdict": verdict, "confidence": 0.7},
        "fields": {name: {"verdict": fit} for name in fields},
    }


class Router:
    """One scripted reply list per provider URL, so the order across providers does not matter."""

    def __init__(self, spark: list[tuple], glimmer: list[tuple] | None = None) -> None:
        self.spark = FakeProvider(*spark)
        self.glimmer = FakeProvider(*(glimmer or [reply(None, 599)]))
        self.sleeps: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == RESPONSES:
            return self.spark(request)
        assert str(request.url) == GLIMMER_CHAT, request.url
        return self.glimmer(request)


@pytest.fixture
def test_clock():
    fixed = TestClock()
    fixed.set(NINE)
    with clock.using(fixed):
        yield fixed


@pytest.fixture
def make_layer(driver, migrated, test_clock):
    opened = []

    def make(router: Router, **changes) -> ModelLayer:
        layer = ModelLayer(
            driver,
            settings=settings(**changes),
            transport=httpx.MockTransport(router),
            sleep=router.sleeps.append,
        )
        opened.append(layer)
        return layer

    yield make
    for layer in opened:
        layer.close()


def jobs_of(driver, world_id: str) -> list[str]:
    rows = read(driver, world_id, "MATCH (c:ModelCall) RETURN c.job AS job, c.at AS at ORDER BY at")
    return sorted(row["job"] for row in rows)


def batch_for(ops: list[dict], chat_src: dict) -> dict:
    people = [
        create_item("person", "Ilse Marrow", "e1", temp_id="$ilse"),
        create_item("person", "Teodor Quill", "e1", temp_id="$teo"),
    ]
    return {"evidence": [observation(chat_src, "e1", 1)], "ops": [*people, *ops]}


def meaning(driver, world_id: str, name: str) -> dict | None:
    rows = read(driver, world_id, "MATCH (m:Meaning {name: $n}) RETURN m", n=name)
    return rows[0]["m"] if rows else None


def test_both_judges_record_their_verdicts_and_the_writer_mints_the_word(
    driver, seeded, chat, make_layer
):
    world_id = seeded["world_id"]
    router = Router([first("different")], [reply(chat_doc(json.dumps(second("different"))))])
    plan = plan_words(make_layer(router), driver, world_id, [proposal()])

    assert len(plan.ops) == 1 and plan.generic == () and plan.held == ()
    assert jobs_of(driver, world_id) == sorted([FIRST_JOB, SECOND_JOB])
    rows = read(driver, world_id, "MATCH (j:Judgment) RETURN j")
    assert sorted((r["j"]["role"], r["j"]["verdict"]) for r in rows) == [
        ("field_fit", "fits"),
        ("same_word", "different"),
        ("same_word", "different"),
    ]
    assert all(r["j"]["correlated"] is False for r in rows)
    # The second judge never sees the first one's verdict.
    glimmer_body = json.loads(router.glimmer.requests[0].content)
    assert "different" not in json.dumps(glimmer_body["messages"][1])

    ops = [*plan.ops, create_link("$ilse", GODPARENT, "$teo", "e1")]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    row = meaning(driver, world_id, GODPARENT)
    assert (row["status"], row["temporal_class"]) == ("provisional", "permanent")


def test_two_judges_who_say_same_give_an_alias(driver, seeded, chat, make_layer):
    world_id = seeded["world_id"]
    router = Router([first("same")], [reply(chat_doc(json.dumps(second("same"))))])
    plan = plan_words(make_layer(router), driver, world_id, [proposal("is pals with")])
    ops = [
        *plan.ops,
        create_link("$ilse", "is pals with", "$teo", "e1", recheck_by=RECHECK),
    ]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    assert meaning(driver, world_id, "is pals with") is None
    assert read(
        driver, world_id, "MATCH (a:Alias {phrase: 'is pals with'}) RETURN a.status AS s"
    ) == [{"s": "canonical"}]


@pytest.mark.parametrize("glimmer", ["not_configured", "invalid_output"])
def test_when_glimmer_cannot_answer_a_correlated_spark_judge_stands_in(
    driver, seeded, chat, make_layer, glimmer
):
    world_id = seeded["world_id"]
    fallback = reply(spark_doc(json.dumps(second("different"))))
    if glimmer == "not_configured":
        router = Router([first("different"), fallback])
        layer = make_layer(router, glimmer_base_url=None, glimmer_key=None)
    else:
        router = Router([first("different"), fallback], [reply(chat_doc("not json at all"))])
        layer = make_layer(router)
    plan = plan_words(layer, driver, world_id, [proposal()])

    assert FALLBACK_JOB in jobs_of(driver, world_id)
    fallback_body = json.loads(router.spark.requests[1].content)
    first_body = json.loads(router.spark.requests[0].content)
    assert fallback_body["instructions"] != first_body["instructions"]
    second_rows = read(
        driver, world_id, "MATCH (j:Judgment {correlated: true}) RETURN j.role AS role"
    )
    assert sorted(row["role"] for row in second_rows) == ["field_fit", "same_word"]

    ops = [*plan.ops, create_link("$ilse", GODPARENT, "$teo", "e1")]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    assert meaning(driver, world_id, GODPARENT)["status"] == "provisional"


def test_a_disputed_field_from_the_second_judge_reaches_the_writer_as_the_safer_value(
    driver, seeded, chat, make_layer
):
    world_id = seeded["world_id"]
    router = Router(
        [first("different")], [reply(chat_doc(json.dumps(second("different", "does_not_fit"))))]
    )
    plan = plan_words(make_layer(router), driver, world_id, [proposal()])
    ops = [
        *plan.ops,
        create_link("$ilse", GODPARENT, "$teo", "e1", recheck_by=RECHECK),
    ]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    row = meaning(driver, world_id, GODPARENT)
    assert row["temporal_class"] == "state" and json.loads(row["disputed_fields"]) == [
        "temporal_class"
    ]


def test_a_known_phrase_and_a_refused_proposal_cost_no_judge_call(driver, seeded, make_layer):
    world_id = seeded["world_id"]
    router = Router([], [])
    bad_kind = proposal(
        "errand",
        word_type="kind",
        neighbours=[
            {"word": "task", "distinction": "a task has no place"},
            {"word": "plan", "distinction": "a plan is larger"},
        ],
        declared={
            "lifecycle_class": "work_with_state",
            "status_vocabulary": [{"status": "x", "role": "maybe"}],
        },
    )
    plan = plan_words(
        make_layer(router),
        driver,
        world_id,
        [proposal("connected with"), proposal("is close to", neighbours=[]), bad_kind],
    )
    assert plan.ops == () and router.spark.requests == [] and router.glimmer.requests == []
    assert plan.reused == ("connected with",)
    assert plan.generic == ("is close to",)
    assert plan.held == (("kind", "errand", "unmapped_status"),)


def test_past_the_valve_a_phrase_rides_on_the_generic_meaning_with_its_phrase_kept(
    driver, seeded, chat, make_layer
):
    world_id = seeded["world_id"]
    names = ["is godparent of", "is godchild of", "is a sponsor of", "is a patron of"]
    router = Router([first("different")], [reply(chat_doc(json.dumps(second("different"))))])
    plan = plan_words(make_layer(router), driver, world_id, [proposal(name) for name in names])
    assert [op["name"] for op in plan.ops] == names[:3]
    assert plan.generic == (names[3],)

    registry = Registry.load(WorldQuery(driver, world_id))
    # Reviewer blocker 4: each phrase was proposed permanent, so its link carries no re-check by;
    # the generic meaning is a state, and fit_links gives the ridden link its horizon.
    links = [create_link("$ilse", name, "$teo", "e1") for name in names]
    ops = [*plan.ops, *fit_links(plan, links, now=clock.now(world_id))]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    generic = next(row for row in registry.meanings.values() if row.get("generic"))
    kept = read(
        driver,
        world_id,
        "MATCH (:Item)-[l:LINK {meaning: $m}]->(:Item) "
        "RETURN l.original_phrase AS phrase, l.recheck_by AS recheck_by",
        m=generic["id"],
    )
    assert kept[0]["recheck_by"] == NINE + timedelta(days=generic["default_horizon"])
    texts = read(
        driver,
        world_id,
        "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS v",
        id=kept[0]["phrase"],
    )
    assert texts == [{"v": names[3]}]
    assert all(meaning(driver, world_id, name)["status"] == "provisional" for name in names[:3])


def test_the_judge_takes_a_checked_proposal_and_records_nothing_without_a_call(
    driver, seeded, make_layer
):
    world_id = seeded["world_id"]
    registry = Registry.load(WorldQuery(driver, world_id))
    checked = check_proposal(registry, {k: v for k, v in proposal().items() if k != "example"})
    router = Router([reply(None, 500)], [])
    with pytest.raises(ProviderError):
        judge_word(make_layer(router), driver, world_id, checked, example="a line")
    assert read(driver, world_id, "MATCH (j:Judgment) RETURN j") == []


def test_one_word_proposed_twice_in_a_plan_is_judged_once(driver, seeded, chat, make_layer):
    """Probe PJ1: the second proposal of a name writes with the first, so the batch commits."""
    world_id = seeded["world_id"]
    router = Router([first("different")], [reply(chat_doc(json.dumps(second("different"))))])
    plan = plan_words(make_layer(router), driver, world_id, [proposal(), proposal()])
    assert len(plan.ops) == 1 and plan.reused == (GODPARENT,)
    assert len(router.spark.requests) == 1
    ops = [*plan.ops, create_link("$ilse", GODPARENT, "$teo", "e1")]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    assert meaning(driver, world_id, GODPARENT)["status"] == "provisional"


def test_a_proposal_the_judges_cannot_take_is_held_and_the_plan_goes_on(driver, seeded, make_layer):
    """Probe PJ2: a field value too long for a subject is the proposal's fault, not the plan's."""
    world_id = seeded["world_id"]
    vocabulary = [{"status": f"stage {n} " + "x" * 60, "role": "open"} for n in range(80)]
    errand = proposal(
        "errand",
        word_type="kind",
        neighbours=[
            {"word": "task", "distinction": "a task has no place"},
            {"word": "plan", "distinction": "a plan is larger"},
        ],
        declared={"lifecycle_class": "work_with_state", "status_vocabulary": vocabulary},
    )
    router = Router([first("different")], [reply(chat_doc(json.dumps(second("different"))))])
    plan = plan_words(make_layer(router), driver, world_id, [errand, proposal()])
    assert plan.held == (("kind", "errand", "judge_failed"),)
    assert [op["name"] for op in plan.ops] == [GODPARENT]


# Regressions of the reviewer of c19dbd9 (blockers 2 to 4) ----------------------------------------


def property_proposal(name: str, **over) -> dict:
    # A starter kind with no property name of its own yet (the starter set names scheduled times
    # for plan, task, trip, period and event, S50), so its first property needs no neighbour.
    op = {
        "op_id": new_op_id(),
        "word_type": "property_name",
        "kind": "problem",
        "name": name,
        "description": f"The plan's {name}.",
        "reason": f"No property says the plan's {name}.",
        "worked_example": {"ev_id": "e1"},
        "neighbours": [],
        "declared": {"value_type": "text", "temporal_class": "permanent"},
        "example": "The orchard group holds the plan; the budget is small.",
    }
    op.update(over)
    return op


def fields_only(verdict: str) -> tuple:
    return reply(chat_doc(json.dumps({"fields": {"temporal_class": {"verdict": verdict}}})))


def orchard_plan(**properties) -> dict:
    return create_item(
        "problem",
        "Replant the orchard",
        "e1",
        status="open",
        recheck_by=RECHECK,
        properties=properties,
    )


def test_two_new_property_names_of_one_kind_plan_and_commit_in_one_batch(
    driver, seeded, chat, make_layer
):
    """Blocker 2: a word the batch mints is no neighbour of a later proposal, in the plan and in
    the writer alike, so the second property name of a new kind needs no neighbour either."""
    world_id = seeded["world_id"]
    router = Router([], [fields_only("fits")])
    proposals = [property_proposal("owner"), property_proposal("budget note")]
    plan = plan_words(make_layer(router), driver, world_id, proposals)
    assert [op["name"] for op in plan.ops] == ["owner", "budget note"] and plan.held == ()
    item = orchard_plan(owner="the orchard group", **{"budget note": "small"})
    apply_batch(
        driver,
        world_id,
        LEARNER,
        {"evidence": [observation(chat, "e1", 1)], "ops": [*plan.ops, item]},
    )
    rows = read(
        driver,
        world_id,
        "MATCH (p:PropertyName) WHERE p.origin <> 'starter' "
        "RETURN p.name AS name, p.status AS status ORDER BY name",
    )
    assert rows == [
        {"name": "budget note", "status": "provisional"},
        {"name": "owner", "status": "provisional"},
    ]


def test_a_disputed_property_of_a_kind_that_is_not_a_record_is_held_not_planned(
    driver, seeded, make_layer
):
    """Blocker 3a: the safer value of a disputed permanent property is state, and a state property
    sits only on a record, so the word would fit no plan: the plan holds it."""
    world_id = seeded["world_id"]
    router = Router([], [fields_only("does_not_fit")])
    plan = plan_words(make_layer(router), driver, world_id, [property_proposal("owner")])
    assert plan.ops == () and plan.words == ()
    assert plan.held == (("property_name", "owner", "state_property_on_item"),)


def test_a_disputed_meaning_settles_as_state_and_its_link_gets_a_recheck(
    driver, seeded, chat, make_layer
):
    """Blocker 3b: the plan says how the word settles, and fit_links gives a link of a word that
    settled as a state the safer horizon, so the batch commits with no hand-made re-check."""
    world_id = seeded["world_id"]
    router = Router(
        [first("different")], [reply(chat_doc(json.dumps(second("different", "does_not_fit"))))]
    )
    plan = plan_words(make_layer(router), driver, world_id, [proposal()])
    assert [(w.outcome, w.temporal_class, w.default_horizon, w.disputed) for w in plan.words] == [
        ("mint", "state", 30, ("temporal_class",))
    ]
    links = fit_links(
        plan, [create_link("$ilse", GODPARENT, "$teo", "e1")], now=clock.now(world_id)
    )
    apply_batch(driver, world_id, LEARNER, batch_for([*plan.ops, *links], chat))
    row = meaning(driver, world_id, GODPARENT)
    assert row["temporal_class"] == "state"
    link = read(driver, world_id, "MATCH ()-[l:LINK]->() RETURN l.recheck_by AS r")
    assert link == [{"r": NINE + timedelta(days=30)}]


def test_an_alias_of_a_state_meaning_reads_its_horizon_and_a_given_recheck_is_kept(
    driver, seeded, chat, make_layer
):
    world_id = seeded["world_id"]
    router = Router([first("same")], [reply(chat_doc(json.dumps(second("same"))))])
    plan = plan_words(make_layer(router), driver, world_id, [proposal("is pals with")])
    assert [(w.outcome, w.temporal_class, w.default_horizon) for w in plan.words] == [
        ("alias", "state", 180)
    ]
    sent = [
        create_link("$ilse", "is pals with", "$teo", "e1"),
        create_link("$teo", "is pals with", "$ilse", "e1", recheck_by=RECHECK),
    ]
    links = fit_links(plan, sent, now=clock.now(world_id))
    assert [link["recheck_by"] for link in links] == [
        (NINE + timedelta(days=180)).isoformat(),
        RECHECK,
    ]
    apply_batch(driver, world_id, LEARNER, batch_for([*plan.ops, *links], chat))
    assert meaning(driver, world_id, "is pals with") is None


# Regressions of the R4 delta on d3ca3c4 (the plan's own registry) ---------------------------


def test_an_alias_whose_ends_fit_only_reversed_is_not_planned_and_rides_on_generic(
    driver, seeded, chat, make_layer
):
    """Both judges say "possesses" is "owned by", but its declared ends fit that word only the
    other way round: the plan settles it as the writer will, sends the phrase to the generic
    meaning, and the batch commits."""
    world_id = seeded["world_id"]
    router = Router([first("same")], [reply(chat_doc(json.dumps(second("same"))))])
    possesses = proposal(
        "possesses",
        neighbours=[
            {"word": "owned by", "distinction": "none found"},
            {"word": "assigned to", "distinction": "assigned to gives work, not ownership"},
        ],
        declared={
            "from_kinds": {"kinds": ["person"]},
            "to_kinds": {"kinds": ["plan"]},
            "direction": "from_to",
            "cardinality": "many",
            "carries_consequences": False,
            "temporal_class": "permanent",
        },
    )
    plan = plan_words(make_layer(router), driver, world_id, [possesses])
    assert plan.ops == () and plan.generic == ("possesses",)
    links = fit_links(
        plan, [create_link("$ilse", "possesses", "$plan", "e1")], now=clock.now(world_id)
    )
    assert links[0]["original_phrase"] == "possesses" and links[0]["recheck_by"]
    apply_batch(
        driver,
        world_id,
        LEARNER,
        batch_for([orchard_plan() | {"temp_id": "$plan"}, *links], chat),
    )


def test_one_word_in_two_surface_forms_is_planned_once(driver, seeded, chat, make_layer):
    """The resolver takes "is godparent of" to "godparent of": the plan's registry knows the first
    once it is planned, so the second is reused, and both phrases write with one word."""
    world_id = seeded["world_id"]
    router = Router([first("different")], [reply(chat_doc(json.dumps(second("different"))))])
    plan = plan_words(
        make_layer(router),
        driver,
        world_id,
        [proposal("godparent of"), proposal("is godparent of")],
    )
    assert [op["name"] for op in plan.ops] == ["godparent of"]
    assert plan.reused == ("is godparent of",)
    links = [
        create_link("$ilse", "godparent of", "$teo", "e1"),
        create_link("$teo", "is godparent of", "$ilse", "e1"),
    ]
    apply_batch(driver, world_id, LEARNER, batch_for([*plan.ops, *links], chat))
    assert read(driver, world_id, "MATCH (m:Meaning {status: 'provisional'}) RETURN m.name AS n")


def test_one_property_name_with_its_kind_spelled_twice_is_planned_once(
    driver, seeded, chat, make_layer
):
    world_id = seeded["world_id"]
    router = Router([], [fields_only("fits")])
    plan = plan_words(
        make_layer(router),
        driver,
        world_id,
        [property_proposal("owner"), property_proposal("owner", kind="Problem")],
    )
    assert [op["name"] for op in plan.ops] == ["owner"] and plan.reused == ("owner",)
    apply_batch(
        driver,
        world_id,
        LEARNER,
        {
            "evidence": [observation(chat, "e1", 1)],
            "ops": [*plan.ops, orchard_plan(owner="the orchard group")],
        },
    )


def test_fit_links_reads_a_copula_form_of_a_planned_state_word(driver, seeded, chat, make_layer):
    world_id = seeded["world_id"]
    router = Router(
        [first("different")], [reply(chat_doc(json.dumps(second("different", "does_not_fit"))))]
    )
    plan = plan_words(make_layer(router), driver, world_id, [proposal("godparent of")])
    links = fit_links(
        plan, [create_link("$ilse", "is godparent of", "$teo", "e1")], now=clock.now(world_id)
    )
    assert links[0]["recheck_by"] == (NINE + timedelta(days=30)).isoformat()
    apply_batch(driver, world_id, LEARNER, batch_for([*plan.ops, *links], chat))


def test_a_proposal_of_an_existing_word_with_a_stray_field_is_reused_and_its_links_stay(
    driver, seeded, make_layer
):
    """Reuse first runs before every other check, so a malformed proposal of "depends on" never
    sends the phrase to the generic meaning, and its links keep their meaning."""
    world_id = seeded["world_id"]
    router = Router([], [])
    plan = plan_words(make_layer(router), driver, world_id, [proposal("depends on", kind="task")])
    assert (plan.ops, plan.generic, plan.reused) == ((), (), ("depends on",))
    link = create_link("$roof", "depends on", "$hay", "e1")
    assert fit_links(plan, [link], now=clock.now(world_id)) == [link]


def test_a_second_proposal_of_a_held_word_is_held_again_not_reused(driver, seeded, make_layer):
    world_id = seeded["world_id"]
    state = {"value_type": "text", "temporal_class": "state", "default_horizon": 30}
    plan = plan_words(
        make_layer(Router([], [])),
        driver,
        world_id,
        [property_proposal("mood", declared=state), property_proposal("mood", declared=state)],
    )
    assert plan.reused == () and plan.held == (
        ("property_name", "mood", "state_property_on_item"),
        ("property_name", "mood", "state_property_on_item"),
    )


def test_a_property_name_of_a_kind_the_plan_mints_waits_for_a_later_batch(
    driver, seeded, make_layer
):
    """Its field-fit subject would bind the planned kind, which the writer gives another id."""
    world_id = seeded["world_id"]
    errand = proposal(
        "errand",
        word_type="kind",
        neighbours=[
            {"word": "task", "distinction": "a task has no place"},
            {"word": "plan", "distinction": "a plan is larger"},
        ],
        declared={
            "lifecycle_class": "work_with_state",
            "status_vocabulary": [
                {"status": "to do", "role": "open"},
                {"status": "finished", "role": "done"},
            ],
        },
    )
    router = Router(
        [first("different")],
        [reply(chat_doc(json.dumps(second("different", fields=("status_vocabulary",)))))],
    )
    plan = plan_words(
        make_layer(router),
        driver,
        world_id,
        [errand, property_proposal("courier", kind="errand")],
    )
    assert [op["name"] for op in plan.ops] == ["errand"]
    assert plan.held == (("property_name", "courier", "kind_in_this_plan"),)


def test_a_word_the_judges_set_aside_is_not_judged_again_in_the_same_plan(
    driver, seeded, make_layer
):
    """The second judge disputes "owner" on a plan, so it would be a state: held. A second proposal
    of it in the same plan is held for the same rule without another verdict."""
    world_id = seeded["world_id"]
    router = Router([], [fields_only("does_not_fit"), fields_only("fits")])
    plan = plan_words(
        make_layer(router),
        driver,
        world_id,
        [property_proposal("owner"), property_proposal("owner")],
    )
    assert plan.ops == () and len(router.glimmer.requests) == 1
    assert plan.held == (
        ("property_name", "owner", "state_property_on_item"),
        ("property_name", "owner", "state_property_on_item"),
    )


def test_a_meaning_proposal_with_a_kind_key_is_refused_before_any_judge(driver, seeded, make_layer):
    """The operation's shape check refuses a kind key on a meaning, null too, so the plan sends the
    phrase to the generic meaning without a judge call."""
    world_id = seeded["world_id"]
    router = Router([], [])
    plan = plan_words(make_layer(router), driver, world_id, [proposal(kind=None)])
    assert (plan.ops, plan.generic) == ((), (GODPARENT,))
    assert router.spark.requests == [] and router.glimmer.requests == []


def test_fit_links_never_reads_a_planned_row_id_as_a_phrase(driver, seeded, make_layer):
    world_id = seeded["world_id"]
    router = Router([first("different")], [reply(chat_doc(json.dumps(second("different"))))])
    plan = plan_words(
        make_layer(router), driver, world_id, [proposal(), proposal("is close to", neighbours=[])]
    )
    assert plan.generic == ("is close to",)
    links = [
        create_link("$ilse", "planned:0", "$teo", "e1"),
        create_link("$ilse", "generic:0", "$teo", "e1"),
    ]
    assert fit_links(plan, links, now=clock.now(world_id)) == links


def test_the_plan_reads_the_registry_once(driver, seeded, make_layer, monkeypatch):
    """The plan's copy starts equal to the registry it records as the base: one read, so a write
    between two reads can never make them differ."""
    from grove.ontology import plan as plan_module

    world_id = seeded["world_id"]
    loads = []
    real = plan_module.Registry.load

    def counted(source):
        loads.append(source)
        return real(source)

    monkeypatch.setattr(plan_module.Registry, "load", staticmethod(counted))
    plan_words(make_layer(Router([], [])), driver, world_id, [proposal("connected with")])
    assert len(loads) == 1
