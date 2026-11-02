"""D2 bypass: no write reaches the store except through the writer, and only for a caller allowed.

Spec, D2 "How to test it": "Bypass: a CI check that no module except the writer issues write
statements to the store's tables (...), a test of each database role's grants, and a test that a
read-only token is refused by every write operation."

The read path cannot write either: tests/writer/test_read_transaction_refuses_write.py proves
that WorldQuery.read and PlatformQuery.read run in read transactions that Neo4j refuses to write
in. That case is not repeated here.
"""

from typing import Any
from uuid import uuid4

import pytest

from grove.lints import writer_rule
from grove.lints._scan import Finding
from grove.store.errors import Neo4jError
from grove.writer import BatchDeclined, Caller, apply_batch
from grove.writer import ops as op_catalogue
from tests.lints.conftest import Repo
from tests.writer.helpers import (
    READ_ONLY,
    apply,
    at_minute,
    batch,
    clock_at,
    count_rows,
    create_item,
    create_link,
    new_op_id,
    observation,
    read,
    supports,
)

OPS = op_catalogue.OPS
RECHECK = "2027-06-01T00:00:00+00:00"
WRITE_SCOPES = frozenset({"grove.read", "grove.write"})
KIND_OF = {
    "U": "update",
    "C": "cleanup",
    "K": "consolidation",
    "H": "ai_host",
    "P": "person",
    "W": "writer",
}
# B.2's Callers column for the operations of this lane.
B2_CALLERS = {
    "create_item": "UCKP",
    "update_item": "UCKP",
    "change_status": "UCKPW",
    "end_item": "UCKP",
    "reopen_item": "CKP",
    "create_link": "UCKPH",
    "end_link": "UCKPW",
    "attach_evidence": "UCKPW",
    "end_evidence": "CKP",
}
NOT_LISTED = [
    (op, letter)
    for op, letters in B2_CALLERS.items()
    for letter in KIND_OF
    if letter not in letters
]


def caller_of(letter: str) -> Caller:
    kind = KIND_OF[letter]
    return Caller(kind=kind, scopes=WRITE_SCOPES, actor=f"test:{kind}")


def one_id(driver, world_id: str, cypher: str, **params: Any) -> str:
    rows = read(driver, world_id, cypher, **params)
    assert len(rows) == 1, rows
    return rows[0]["id"]


@pytest.fixture
def rows(driver, seeded, chat):
    """A person, a plan, a task assigned to the person, and the ids every payload needs."""
    world_id = seeded["world_id"]
    ops = [
        create_item("person", "Rowan", "e1", temp_id="$rowan"),
        create_item("plan", "Thursday demo rehearsal", "e1", status="proposed", recheck_by=RECHECK),
        create_item(
            "task", "Draft the venue list", "e1", temp_id="$task", status="open", recheck_by=RECHECK
        ),
        create_link("$task", "assigned to", "$rowan", "e1", recheck_by=RECHECK),
    ]
    apply(driver, world_id, ops, [observation(chat, "e1", 1)])

    def item(title: str) -> str:
        return one_id(driver, world_id, "MATCH (i:Item {title: $t}) RETURN i.id AS id", t=title)

    task = item("Draft the venue list")
    return {
        "world_id": world_id,
        "rowan": item("Rowan"),
        "plan": item("Thursday demo rehearsal"),
        "task": task,
        "version": read(
            driver, world_id, "MATCH (i:Item {id: $id}) RETURN i.version AS v", id=task
        )[0]["v"],
        "link": one_id(
            driver,
            world_id,
            "MATCH (:Item {id: $id})-[l:LINK]->(:Item) RETURN l.id AS id",
            id=task,
        ),
        "evidence": one_id(
            driver,
            world_id,
            "MATCH (e:Evidence {target_id: $id}) RETURN e.id AS id",
            id=task,
        ),
        "observations": [observation(chat, "e9", 9)],
    }


def payload(op: str, ids: dict[str, Any]) -> dict[str, Any]:
    """A minimal operation of each kind that an allowed caller could send. An op this table does
    not know yet gets its bare name, which a scope check must still refuse."""
    reason = "the message states it"
    stop = clock_at(at_minute(9))
    shapes = {
        "create_item": lambda: create_item("person", "Okonkwo", "e9"),
        "update_item": lambda: {
            "item": ids["task"],
            "version": ids["version"],
            "fields": {"title": "Book the boathouse for the rehearsal"},
            "evidence_refs": supports("e9"),
            "reason": reason,
        },
        "change_status": lambda: {
            "item": ids["task"],
            "status": "in progress",
            "evidence_refs": supports("e9"),
            "reason": reason,
            "recheck_by": RECHECK,
        },
        "end_item": lambda: {
            "item": ids["task"],
            "end_class": "ended_in_the_world",
            "stopped_being_true": stop,
            "evidence_refs": supports("e9"),
            "reason": reason,
        },
        "reopen_item": lambda: {"item": ids["task"], "reason": "the list is needed again"},
        "create_link": lambda: create_link(ids["task"], "part of", ids["plan"], "e9"),
        "end_link": lambda: {
            "link": ids["link"],
            "end_class": "ended_in_the_world",
            "stopped_being_true": stop,
            "reason": reason,
        },
        "attach_evidence": lambda: {"target": ids["task"], "ev_id": "e9", "polarity": "supports"},
        "end_evidence": lambda: {"evidence": ids["evidence"], "end_reason": "correction"},
    }
    body = shapes[op]() if op in shapes else {}
    return {"op": op, "op_id": body.pop("op_id", None) or new_op_id(), **body}


def refusals_of(driver, world_id: str, ops: list[dict], evidence: list[dict], caller) -> list:
    with pytest.raises(BatchDeclined) as declined:
        apply(driver, world_id, ops, evidence, caller=caller)
    return declined.value.refusals


def placeholder_ids() -> dict[str, Any]:
    world = f"t_writer-a1_{uuid4()}"
    src = {"episode_id": str(uuid4()), "source_id": str(uuid4())}
    ids = {key: str(uuid4()) for key in ("rowan", "plan", "task", "link", "evidence")}
    return {"world_id": world, "version": 1, "observations": [observation(src, "e9", 9)], **ids}


class NoStore:
    """A driver that fails the test the moment anything asks it for the store."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the store was reached before the scope check: {name}")


def test_the_catalogue_holds_every_operation_of_this_lane():
    assert set(B2_CALLERS) <= set(OPS)


@pytest.mark.parametrize("op_name", sorted(OPS))
def test_a_read_only_token_is_refused_by_every_operation(driver, rows, op_name):
    world_id = rows["world_id"]
    op = payload(op_name, rows)
    before = count_rows(driver, world_id)

    refusals = refusals_of(driver, world_id, [op], rows["observations"], READ_ONLY)

    assert [(r.op_id, r.code) for r in refusals] == [(op["op_id"], "scope_denied")]
    assert count_rows(driver, world_id) == before


def test_a_read_only_token_is_refused_before_the_store_is_read():
    ids = placeholder_ids()
    ops = [payload(op_name, ids) for op_name in sorted(OPS)]

    with pytest.raises(BatchDeclined) as declined:
        apply_batch(NoStore(), ids["world_id"], READ_ONLY, batch(ops, ids["observations"]))

    named = sorted((r.op_id, r.code) for r in declined.value.refusals)
    assert named == sorted((op["op_id"], "scope_denied") for op in ops)


@pytest.mark.parametrize(("op_name", "letter"), NOT_LISTED, ids=[f"{o}-{k}" for o, k in NOT_LISTED])
def test_an_operation_refuses_a_caller_kind_it_does_not_list(driver, rows, op_name, letter):
    world_id = rows["world_id"]
    op = payload(op_name, rows)
    before = count_rows(driver, world_id)

    refusals = refusals_of(driver, world_id, [op], rows["observations"], caller_of(letter))

    assert [(r.op_id, r.code) for r in refusals] == [(op["op_id"], "scope_denied")]
    assert count_rows(driver, world_id) == before


@pytest.mark.parametrize("op_name", sorted(B2_CALLERS))
def test_an_operation_never_refuses_a_caller_kind_it_lists_for_its_kind(driver, rows, op_name):
    # The control for the two tests above: the same payloads are not refused for scope when the
    # caller's kind is listed. Another refusal (an item already ended, say) is fine here.
    for letter in B2_CALLERS[op_name]:
        op = payload(op_name, rows)
        try:
            apply(driver, rows["world_id"], [op], rows["observations"], caller=caller_of(letter))
        except BatchDeclined as declined:
            codes = {r.code for r in declined.refusals}
            assert "scope_denied" not in codes, (letter, declined.refusals)


def test_a_host_sending_create_item_is_refused_and_the_person_is_not(driver, rows):
    world_id = rows["world_id"]
    host = Caller(kind="ai_host", scopes=WRITE_SCOPES, actor="host:t")
    op = payload("create_item", rows)

    refusals = refusals_of(driver, world_id, [op], rows["observations"], host)

    assert [(r.op_id, r.code) for r in refusals] == [(op["op_id"], "scope_denied")]
    saved = apply(driver, world_id, [payload("create_item", rows)], rows["observations"])
    assert saved.state == "saved"
    titles = read(driver, world_id, "MATCH (i:Item {title: 'Okonkwo'}) RETURN i.id AS id")
    assert len(titles) == 1


# Neo4j Community has no roles, so "a test of each database role's grants" becomes: the server
# refuses every role command, and the writer lint below is what holds the rule.
UNSUPPORTED = "Neo.ClientError.Statement.UnsupportedAdministrationCommand"
PROBE_ROLE = "t_writer_a1_bypass_probe"


@pytest.mark.parametrize(
    "statement",
    [
        "SHOW ROLES",
        "SHOW PRIVILEGES",
        f"CREATE ROLE {PROBE_ROLE}",
        f"GRANT WRITE ON GRAPH * TO {PROBE_ROLE}",
    ],
)
def test_the_store_has_no_roles_to_grant(driver, statement):
    with driver.session(database="system") as session:
        edition = session.run(
            "CALL dbms.components() YIELD name, edition WHERE name = 'Neo4j Kernel' RETURN edition"
        ).single()["edition"]
        # On any other edition the role command would succeed, so stop before it runs.
        assert edition == "community"
        with pytest.raises(Neo4jError) as refused:
            session.run(statement).consume()
    assert refused.value.code == UNSUPPORTED


BYPASS = "server/grove/models/bypass.py"

ENTRY_POINT_MODULE = '''"""A model module that saves its own answer."""

from grove.store.connection import DATABASE


def save(driver, answer):
    with driver.session(database=DATABASE) as session:
        session.execute_write(lambda tx: tx.run("RETURN $a", a=answer))
'''

WORLD_QUERY_MODULE = '''"""A model module that saves its own answer through the read side."""

from grove.store.query import WorldQuery


def save(driver, world_id, answer):
    return WorldQuery(driver, world_id).read(
        "CREATE (n:Item {title: $a}) RETURN n.id AS id", a=answer
    )
'''

READ_ONLY_MODULE = WORLD_QUERY_MODULE.replace("CREATE (n:Item", "MATCH (n:Item")


def line_with(text: str, marker: str) -> int:
    return next(n for n, line in enumerate(text.splitlines(), 1) if marker in line)


def findings_for(tmp_path, text: str) -> list[Finding]:
    repo = Repo(tmp_path)
    repo.plant(BYPASS, text)
    return [found for found in writer_rule.check(repo.path) if found.path == BYPASS]


def test_the_writer_lint_flags_a_new_models_module_that_opens_a_write(tmp_path):
    found = findings_for(tmp_path, ENTRY_POINT_MODULE)
    line = line_with(ENTRY_POINT_MODULE, "execute_write")
    assert Finding(BYPASS, line, "writer/entry-point") in found


def test_the_writer_lint_flags_a_create_sent_through_a_world_query(tmp_path):
    found = findings_for(tmp_path, WORLD_QUERY_MODULE)
    line = line_with(WORLD_QUERY_MODULE, "CREATE (n:Item")
    assert Finding(BYPASS, line, "writer/cypher") in found


def test_the_same_models_module_with_a_read_stays_clean(tmp_path):
    assert findings_for(tmp_path, READ_ONLY_MODULE) == []
