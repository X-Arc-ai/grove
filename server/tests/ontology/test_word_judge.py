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
from grove.ontology import plan_words, ride_on_generic
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
    links = [create_link("$ilse", name, "$teo", "e1", recheck_by=RECHECK) for name in names]
    ops = [*plan.ops, *ride_on_generic(registry, links, plan.generic)]
    apply_batch(driver, world_id, LEARNER, batch_for(ops, chat))
    generic = next(row for row in registry.meanings.values() if row.get("generic"))
    kept = read(
        driver,
        world_id,
        "MATCH (:Item)-[l:LINK {meaning: $m}]->(:Item) RETURN l.original_phrase AS phrase",
        m=generic["id"],
    )
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
