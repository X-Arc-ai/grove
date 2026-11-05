"""What the real import chain proved about one Learner pass, with no model call.

The first live pass (64,000-token cap) spent four of its twelve calls on finding the brain
command forms, four more on recall in an empty world, and its one propose on a plan and a task
with no re-check date. The repair then met the token cap. So the packet says which statuses need
a re-check date, how many items the world holds and where search terms go, and the Learner's
file gives the command forms.
"""

from pathlib import Path

from grove.runner.brain import FIELD_GUIDE, pass_input
from grove.runner.passes import PassSettings, caller_for, run_record, start_pass
from grove.writer import apply_request
from tests.runner.helpers import new_auth, queue_store

REPO = Path(__file__).resolve().parents[3]
WORK_STATUSES = {
    "assumption": ["holds"],
    "plan": ["proposed", "active", "waiting"],
    "problem": ["open", "being worked on"],
    "task": ["open", "in progress", "blocked"],
}


# Far enough apart that the writer sees no near duplicate among them.
TITLES = (
    "Amber lantern",
    "Birch canoe",
    "Copper kettle",
    "Dune ferry",
    "Ember quilt",
    "Fennel market",
    "Glacier atlas",
    "Harbor violin",
    "Indigo orchard",
    "Juniper bridge",
    "Kestrel mural",
    "Lotus tram",
    "Marble sundial",
    "Nettle choir",
    "Onyx windmill",
    "Plum observatory",
    "Quartz bakery",
)


def packet_of(driver, world_id):
    request_id = queue_store(driver, world_id)
    grant = start_pass(driver, new_auth()[0], world_id, agent="learner", settings=PassSettings())
    packet = pass_input(driver, world_id, run_record(driver, world_id, grant.run_id))
    return request_id, grant, packet


def item(op_id, kind, status, packet):
    form = packet["batch_form"]["batch"]
    return {
        "op": "create_item",
        "op_id": op_id,
        "kind": kind,
        "title": TITLES[int(op_id[1:])],
        "status": status,
        "became_true": "unknown",
        "evidence_refs": form["ops"][0]["evidence_refs"],
        "reason": "the source says it",
    }


def test_packet_lists_each_status_that_needs_a_recheck_date(driver, seeded):
    packet = packet_of(driver, seeded)[2]
    kinds = {row["name"]: row for row in packet["kinds"]}
    assert {
        name: row["recheck_statuses"] for name, row in kinds.items() if row["recheck_statuses"]
    } == WORK_STATUSES
    assert kinds["organisation"]["recheck_statuses"] == []


def test_the_listed_statuses_are_the_ones_the_writer_declines(driver, seeded):
    request_id, grant, packet = packet_of(driver, seeded)
    form = packet["batch_form"]["batch"]
    kinds = [row for row in packet["kinds"] if row["class"] == "work_with_state"]
    ops, listed = [], set()
    for row in kinds:
        for status in row["statuses"]:
            op_id = f"o{len(ops) + 1}"
            ops.append(item(op_id, row["name"], status, packet))
            if status in row["recheck_statuses"]:
                listed.add(op_id)
    state = apply_request(
        driver,
        seeded,
        caller_for("learner"),
        request_id,
        {"evidence": form["evidence"], "ops": ops},
        run_id=grant.run_id,
    )
    assert state.status == "declined"
    assert {row["op_id"] for row in state.declines if row["code"] == "missing_recheck"} == listed


def test_a_live_plan_with_its_recheck_date_and_search_terms_is_saved(driver, seeded):
    request_id, grant, packet = packet_of(driver, seeded)
    form = packet["batch_form"]["batch"]
    plan = item("o1", "plan", "active", packet) | {
        "recheck_by": "2026-12-01T09:00:00+00:00",
        "search_terms": ["seed library"],
    }
    state = apply_request(
        driver,
        seeded,
        caller_for("learner"),
        request_id,
        {"evidence": form["evidence"], "ops": [plan]},
        run_id=grant.run_id,
    )
    assert state.status == "saved" and state.receipt is not None


def test_the_field_guide_names_search_terms_and_the_recheck_list():
    guide = FIELD_GUIDE["create_item"]
    assert "search_terms (a list of short strings" in guide
    assert "recheck_statuses" in guide


def test_packet_counts_the_items_of_the_world(driver, seeded):
    packet = packet_of(driver, seeded)[2]
    assert packet["world_items"] == 0


def test_learner_file_gives_the_command_forms_and_the_empty_world_rule():
    body = (REPO / "runner" / "agents" / "learner.md").read_text(encoding="utf-8")
    for form in (
        "brain recall WORDS",
        "brain get ITEM_ID",
        "brain propose --request REQUEST_ID <<'EOF'\nTHE BATCH AS JSON\nEOF\n",
        "brain finish REQUEST_ID",
    ):
        assert form in body
    assert "Never run a help option" in body
    assert "If world_items is 0" in body
    assert "recheck_statuses" in body
    assert "search_terms" in body
