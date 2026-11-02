"""The demo seeder: a synthetic world from the web checks' fixture, t_ worlds only."""

import ast
import json
import stat
from collections import Counter
from pathlib import Path

import pytest

from grove.store.connection import DATABASE
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer import demo, testing
from grove.writer.testing import session_token_hash
from grove.writer.worlds import load_starter_set

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "web" / "checks" / "fixture.synthetic.json"


@pytest.fixture(scope="module")
def fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def small(fixture):
    """The person's own node, two nodes of each type and the links among them: a quick seed."""
    chosen, per_type = {fixture["person_id"]}, Counter()
    for wrapper in fixture["elements"]["nodes"]:
        node = wrapper["data"]
        if per_type[node["type"]] < 2:
            per_type[node["type"]] += 1
            chosen.add(node["id"])
    return {
        "person_id": fixture["person_id"],
        "elements": {
            "nodes": [w for w in fixture["elements"]["nodes"] if w["data"]["id"] in chosen],
            "edges": [
                w
                for w in fixture["elements"]["edges"]
                if w["data"]["source"] in chosen and w["data"]["target"] in chosen
            ],
        },
    }


def expected_links(fixture):
    """(active, ended, skipped): an ended link has no active_key, and a repeat is skipped."""
    active, ended, seen = 0, 0, set()
    for wrapper in fixture["elements"]["edges"]:
        edge = wrapper["data"]
        if edge["invalid_at"]:
            ended += 1
        else:
            seen.add((edge["source"], edge["verb"], edge["target"]))
            active += 1
    return len(seen), ended, active - len(seen)


def rows_of(driver, world_id):
    with driver.session(database=DATABASE) as session:
        return session.run(
            "MATCH (n) WHERE n.world_id = $w OR n.planted_for = $w OR n.id = $w "
            "RETURN count(n) AS c",
            w=world_id,
        ).single()["c"]


def test_seed_plants_every_item_link_meaning_and_the_session(driver, world, fixture):
    seeded = demo.seed_world(driver, world, fixture)
    nodes = [w["data"] for w in fixture["elements"]["nodes"]]
    active, ended, skipped = expected_links(fixture)
    assert seeded.counts() == {
        "world_id": world,
        "items": len(nodes),
        "links": active + ended,
        "ended_links": ended,
        "links_skipped": skipped,
        "meanings_added": len(seeded.meanings_added),
        "self_item": True,
    }
    assert seeded.self_item_id == seeded.items[fixture["person_id"]]

    query = WorldQuery(driver, world)
    kinds = {
        r["id"]: r["name"] for r in query.read("MATCH (k:Kind) RETURN k.id AS id, k.name AS name")
    }
    stored = query.read("MATCH (i:Item) RETURN i.kind AS kind, i.title AS title")
    assert len(stored) == len(nodes)
    assert Counter(kinds[r["kind"]] for r in stored) == Counter(
        demo.kind_name_of(n["type"]) for n in nodes
    )
    assert {r["title"] for r in stored} == {n["label"] for n in nodes}

    links = query.read(
        "MATCH (a:Item)-[l:LINK]->(b:Item) "
        "RETURN l.active_key AS key, l.from_item AS a, l.meaning AS m, l.to_item AS b"
    )
    keyed = [r for r in links if r["key"] is not None]
    assert len(keyed) == active and len(links) == active + ended
    assert all(r["key"] == "|".join((world, r["a"], r["m"], r["b"])) for r in keyed)
    verbs = {w["data"]["verb"] for w in fixture["elements"]["edges"]}
    meanings = {r["name"] for r in query.read("MATCH (m:Meaning) RETURN m.name AS name")}
    assert verbs <= meanings
    starter = {m["name"] for m in load_starter_set()["meanings"]}
    assert set(seeded.meanings_added) == verbs - starter

    own = PlatformQuery(driver).read(
        "MATCH (w:World {id: $w}) RETURN w.self_item_id AS own, w.owner_user_id AS owner", w=world
    )
    assert own[0]["own"] == seeded.self_item_id
    sessions = PlatformQuery(driver).read(
        "MATCH (s:Session {token_hash: $h}) RETURN s.user_id AS user",
        h=session_token_hash(seeded.token),
    )
    assert [s["user"] for s in sessions] == [own[0]["owner"]]

    # An ended item keeps the fixture's fields, its stopped bound among them.
    node = next(w["data"] for w in fixture["elements"]["nodes"] if w["data"]["invalid_at"])
    found = query.read("MATCH (i:Item {id: $id}) RETURN i", id=seeded.items[node["id"]])[0]["i"]
    assert found["title"] == node["label"]
    assert found["recorded"] == demo.instant_of(node["created_at"])
    assert found["became_true_earliest"] == demo.instant_of(node["valid_at"])
    assert found["became_true_precision"] == "day"
    assert found["stopped_being_true_latest"] == demo.instant_of(node["invalid_at"])
    assert found["touched_at"] == demo.instant_of(node["updated_at"])
    assert found["version"] == node["version"]
    assert json.loads(found["properties"]) == node["properties"]


@pytest.mark.parametrize(
    "bad", ["", "real-world", "t_short", "x_demo_0123456789abcdef0123456789abcdef"]
)
def test_every_entry_refuses_a_world_that_is_not_a_test_world(driver, small, bad):
    with pytest.raises(ValueError):
        demo.seed_world(driver, bad, small)
    with pytest.raises(ValueError):
        demo.add_items(driver, bad, 1)
    with pytest.raises(ValueError):
        demo.purge(driver, bad)
    assert demo.main(["purge", "--world", bad]) == 2
    if bad:
        assert rows_of(driver, bad) == 0


def test_add_links_each_new_item_to_an_item_already_there(driver, world, small):
    seeded = demo.seed_world(driver, world, small)
    added = demo.add_items(driver, world, 5)
    assert len(added) == len(set(added)) == 5
    old = set(seeded.items.values())
    query = WorldQuery(driver, world)
    for item_id in added:
        links = query.read(
            "MATCH (a:Item {id: $id})-[l:LINK]->(b:Item) RETURN b.id AS to, l.active_key AS key",
            id=item_id,
        )
        assert len(links) == 1 and links[0]["to"] in old and links[0]["key"] is not None
    with pytest.raises(ValueError):
        demo.add_items(driver, world, 0)


def test_purge_removes_the_world_and_what_was_planted_for_it(driver, make_world, small):
    world = make_world()
    demo.seed_world(driver, world, small)
    assert rows_of(driver, world) > len(small["elements"]["nodes"])
    demo.purge(driver, world)
    assert rows_of(driver, world) == 0


def test_the_cli_writes_the_token_file_600_and_prints_no_token(
    driver, world, small, tmp_path, capsys
):
    fixture_file = tmp_path / "fixture.json"
    fixture_file.write_text(json.dumps(small), encoding="utf-8")
    token_file = tmp_path / "token"
    token_file.write_text("old", encoding="utf-8")
    token_file.chmod(0o644)
    code = demo.main(
        ["seed", "--fixture", str(fixture_file), "--token-file", str(token_file), "--world", world]
    )
    assert code == 0
    out = capsys.readouterr().out
    printed = json.loads(out)
    assert printed["world_id"] == world
    assert printed["items"] == len(small["elements"]["nodes"])
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    token = token_file.read_text(encoding="utf-8")
    leaked = token in out
    assert leaked is False
    known = PlatformQuery(driver).read(
        "MATCH (s:Session {token_hash: $h}) RETURN count(s) AS n", h=session_token_hash(token)
    )
    assert known[0]["n"] == 1

    assert demo.main(["add", "--world", world, "--count", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"world_id": world, "added": 3}
    assert len(WorldQuery(driver, world).read("MATCH (i:Item) RETURN i.id AS id")) == (
        len(small["elements"]["nodes"]) + 3
    )


def test_the_demo_imports_only_public_names_of_the_test_helpers():
    """demo.py is a module of its own, so it takes no private name of grove.writer.testing."""
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    taken = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "grove.writer.testing"
        for alias in node.names
    ]
    assert "refuse_real_world" in taken
    assert not [name for name in taken if name.startswith("_")], taken
    assert testing.refuse_real_world("t_demo_" + "0" * 32) is None
    with pytest.raises(ValueError, match="t_ worlds only"):
        testing.refuse_real_world("a-real-world")
