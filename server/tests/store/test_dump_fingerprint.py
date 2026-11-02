"""The dump fingerprint: any changed byte changes it, and reading it changes nothing."""

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

# The driver's time types, through the store module: only server/grove/store/ imports neo4j.
from grove.store.dump_fingerprint import (
    _CONSTRAINTS,
    _INDEXES,
    _LINKS,
    _NODES,
    MISMATCH,
    Date,
    DateTime,
    Duration,
    Point,
    StoreMoved,
    UnknownValue,
    _read,
    combine,
    dump_fingerprint,
    encode,
    link_digest,
    main,
    node_digest,
    schema_digest,
)
from grove.store.errors import ScopeError
from grove.writer.testing import plant_link, plant_node

# ---- the encoding, with no store --------------------------------------------------------------


def test_the_encoding_keeps_every_type_apart():
    values = [1, 1.0, "1", True, [1], None, b"1", -0.0, 0.0, Date(2026, 11, 2)]
    encoded = [json.dumps(encode(value)) for value in values]
    assert len(set(encoded)) == len(values)


def test_the_encoding_is_exact_for_floats_and_stable_for_nan():
    assert encode(1.0) != encode(1.0000000000000002)
    assert encode(float("nan")) == encode(float("nan"))
    assert encode(float("inf")) != encode(float("-inf"))


def test_the_encoding_keeps_a_time_s_offset_and_its_zone():
    plus_one = DateTime(2026, 11, 2, 9, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    utc = DateTime(2026, 11, 2, 8, 0, 0, tzinfo=UTC)
    assert encode(plus_one) != encode(utc)
    assert encode(Duration(days=1)) != encode(Duration(seconds=86400))


def test_an_unknown_type_is_refused_by_name_only():
    with pytest.raises(UnknownValue) as refused:
        encode(object())
    assert "object" in str(refused.value)


def test_a_map_digest_ignores_key_order_and_label_order():
    assert node_digest(["Item", "Extra"], {"a": 1, "b": 2}) == node_digest(
        ["Extra", "Item"], {"b": 2, "a": 1}
    )


def test_a_node_digest_changes_with_any_label_key_or_value():
    base = node_digest(["Item"], {"title": "Osprey", "n": 1})
    assert node_digest(["Item", "Extra"], {"title": "Osprey", "n": 1}) != base
    assert node_digest(["Item"], {"title": "Osprey ", "n": 1}) != base
    assert node_digest(["Item"], {"title": "Osprey", "n": 2}) != base
    assert node_digest(["Item"], {"title": "Osprey", "m": 1}) != base
    assert node_digest(["Item"], {"title": "Osprey"}) != base


def test_a_link_digest_changes_with_its_ends_type_and_values():
    a, b = node_digest(["Item"], {"id": "a"}), node_digest(["Item"], {"id": "b"})
    base = link_digest("LINK", {"w": 1}, a, b)
    assert link_digest("LINK", {"w": 1}, b, a) != base
    assert link_digest("OTHER", {"w": 1}, a, b) != base
    assert link_digest("LINK", {"w": 2}, a, b) != base


def test_the_combined_digest_is_a_multiset_with_no_order():
    counts = {"nodes": 2}
    one, two = "1" * 64, "2" * 64
    assert combine([one, two], [], None, counts) == combine([two, one], [], None, counts)
    assert combine([one, one], [], None, counts) != combine([one], [], None, counts)
    assert combine([one], [two], None, counts) != combine([one, two], [], None, counts)
    assert combine([one], [], ["s"], counts) != combine([one], [], None, counts)


def test_the_schema_digest_sees_an_index_option():
    plain = [{"name": "i", "options": {"indexConfig": {"fulltext.analyzer": "standard"}}}]
    folded = [{"name": "i", "options": {"indexConfig": {"fulltext.analyzer": "folding"}}}]
    assert schema_digest(plain, "indexes") != schema_digest(folded, "indexes")


# ---- one world on the live store -------------------------------------------------------------


def _plant_world(driver, world):
    first = plant_node(
        driver, world, "Item", title="Osprey Pro", price=55.0, seen=[1, 2], known=True
    )
    second = plant_node(
        driver, world, "Item", title="Tern", when=datetime(2026, 10, 13, tzinfo=UTC)
    )
    plant_link(driver, world, first["id"], second["id"], meaning="depends_on")
    return first, second


def test_reading_a_world_twice_gives_one_fingerprint_and_changes_nothing(driver, world):
    _plant_world(driver, world)
    first = dump_fingerprint(driver, world_id=world)
    second = dump_fingerprint(driver, world_id=world)
    assert first == second
    assert first["scope"] == "world"
    assert first["nodes"] == 2 and first["relationships"] == 1
    assert first["labels"] == {"Item": 2} and first["types"] == {"LINK": 1}
    assert first["constraints"] == first["indexes"] == 0


def test_a_new_node_or_link_in_the_world_changes_its_fingerprint(driver, world):
    first, second = _plant_world(driver, world)
    before = dump_fingerprint(driver, world_id=world)
    plant_link(driver, world, second["id"], first["id"], meaning="depends_on")
    after_link = dump_fingerprint(driver, world_id=world)
    plant_node(driver, world, "Item", title="Heron")
    after_node = dump_fingerprint(driver, world_id=world)
    assert len({before["fingerprint"], after_link["fingerprint"], after_node["fingerprint"]}) == 3


def test_equal_content_in_two_worlds_still_differs_by_its_world_ids(driver, make_world):
    one, two = make_world(), make_world()
    plant_node(driver, one, "Item", title="Osprey Pro")
    plant_node(driver, two, "Item", title="Osprey Pro")
    assert (
        dump_fingerprint(driver, world_id=one)["fingerprint"]
        != dump_fingerprint(driver, world_id=two)["fingerprint"]
    )


def test_another_world_never_moves_this_world_s_fingerprint(driver, make_world):
    mine, other = make_world(), make_world()
    _plant_world(driver, mine)
    before = dump_fingerprint(driver, world_id=mine)
    _plant_world(driver, other)
    assert dump_fingerprint(driver, world_id=mine) == before


def test_the_command_exits_3_on_another_fingerprint(driver, world, capsys):
    _plant_world(driver, world)
    expected = dump_fingerprint(driver, world_id=world)["fingerprint"]
    assert main(["--world", world, "--expect", expected]) == 0
    assert json.loads(capsys.readouterr().out)["match"] is True
    assert main(["--world", world, "--expect", "0" * 64]) == MISMATCH
    shown = json.loads(capsys.readouterr().out)
    assert shown["match"] is False
    assert "Osprey" not in json.dumps(shown)


def test_the_command_prints_digests_and_counts_and_no_stored_text(driver, world, capsys):
    first, _ = _plant_world(driver, world)
    assert main(["--world", world]) == 0
    printed = capsys.readouterr().out
    assert "Osprey" not in printed and first["id"] not in printed and world not in printed


# ---- the whole store, only where no other lane writes ----------------------------------------


def test_the_whole_store_reads_the_same_twice_and_holds_the_schema(driver, migrated):
    if os.environ.get("GROVE_STORE_EXCLUSIVE") != "1":
        pytest.skip("other lanes write the shared store between two reads")
    first = dump_fingerprint(driver)
    assert dump_fingerprint(driver) == first
    assert first["scope"] == "store"
    assert first["constraints"] > 0 and first["indexes"] > 0
    assert first["labels"]["Migration"] >= 1


def test_a_new_row_anywhere_moves_the_whole_store_fingerprint(driver, world):
    if os.environ.get("GROVE_STORE_EXCLUSIVE") != "1":
        pytest.skip("other lanes write the shared store between two reads")
    before = dump_fingerprint(driver)
    plant_node(driver, world, "Item", title="Heron")
    after = dump_fingerprint(driver)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["nodes"] == before["nodes"] + 1


# ---- review round one -------------------------------------------------------------------------


class _MovingTx:
    """A transaction whose second read sees a link to a node the first read did not return."""

    def run(self, query, **params):
        if query == _NODES:
            return [{"element": "e1", "labels": ["Item"], "props": {"id": "a"}}]
        return [{"start": "e1", "end": "e-new", "type": "LINK", "props": {}}]


def test_a_link_to_a_node_the_first_read_missed_is_refused():
    with pytest.raises(StoreMoved):
        _read(_MovingTx(), "t_deploy-a_00000000-0000-0000-0000-000000000000", with_schema=False)


def test_a_link_into_another_world_stays_out_of_this_world_s_fingerprint(driver, make_world):
    mine, other = make_world(), make_world()
    first, _ = _plant_world(driver, mine)
    before = dump_fingerprint(driver, world_id=mine)
    far = plant_node(driver, other, "Item", title="Far")
    plant_link(driver, mine, first["id"], far["id"], to_world=other, meaning="depends_on")
    after = dump_fingerprint(driver, world_id=mine)
    assert after == before
    assert after["relationships"] == 1


def test_the_live_index_rows_carry_their_options(driver, migrated):
    with driver.session(database="neo4j") as session:
        rows = [dict(row) for row in session.run(_INDEXES)]
    fulltext = [row for row in rows if row["type"] == "FULLTEXT"]
    assert fulltext
    for row in fulltext:
        assert row["options"]["indexConfig"]["fulltext.analyzer"]


# ---- review round five ------------------------------------------------------------------------


def test_the_encoding_keeps_a_zone_name_nanoseconds_a_srid_and_bytes_apart():
    fixed = DateTime(2026, 7, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    named = DateTime(2026, 7, 1, 9, 0, 0, tzinfo=ZoneInfo("Europe/Paris"))
    assert encode(fixed) != encode(named)
    assert encode(Duration(seconds=1)) != encode(Duration(seconds=1, nanoseconds=1))

    class Wgs84(Point):
        srid = 4326

    class Cartesian(Point):
        srid = 7203

    assert encode(Wgs84((1.0, 2.0))) != encode(Cartesian((1.0, 2.0)))
    assert encode(b"ab") != encode(b"ab".hex())


def test_the_combined_digest_counts_equal_links_and_ignores_their_order():
    counts = {"relationships": 2}
    one, two = "1" * 64, "2" * 64
    assert combine([], [one, two], None, counts) == combine([], [two, one], None, counts)
    assert combine([], [one, one], None, counts) != combine([], [one], None, counts)


def test_the_schema_digest_ignores_the_order_of_its_rows():
    first, second = {"name": "a"}, {"name": "b"}
    assert schema_digest([first, second], "indexes") == schema_digest([second, first], "indexes")
    assert schema_digest([first], "indexes") != schema_digest([first], "constraints")


def test_a_bad_world_id_is_refused(driver):
    with pytest.raises(ScopeError):
        dump_fingerprint(driver, world_id="not a world")


def test_the_live_schema_rows_keep_every_column_the_digest_needs(driver, migrated):
    with driver.session(database="neo4j") as session:
        constraints = [dict(row) for row in session.run(_CONSTRAINTS)]
        indexes = [dict(row) for row in session.run(_INDEXES)]
    assert constraints and indexes
    assert {"name", "type", "entityType", "labelsOrTypes", "properties", "ownedIndex"} == set(
        constraints[0]
    )
    assert {
        "name",
        "type",
        "entityType",
        "labelsOrTypes",
        "properties",
        "indexProvider",
        "options",
        "owningConstraint",
    } == set(indexes[0])


def test_the_whole_store_reads_see_this_world_s_rows(driver, world):
    """The null branch of each filter, which the backup uses. Safe on a shared store: other lanes
    only add rows."""
    first, second = _plant_world(driver, world)
    with driver.session(database="neo4j") as session:
        nodes = [dict(row) for row in session.run(_NODES, world_id=None)]
        links = [dict(row) for row in session.run(_LINKS, world_id=None)]
    assert {first["id"], second["id"]} <= {row["props"].get("id") for row in nodes}
    mine = [row for row in links if row["props"].get("world_id") == world]
    assert len(mine) == 1
    assert mine[0]["type"] == "LINK"


def test_a_new_link_moves_the_whole_store_fingerprint(driver, world):
    if os.environ.get("GROVE_STORE_EXCLUSIVE") != "1":
        pytest.skip("other lanes write the shared store between two reads")
    first, second = _plant_world(driver, world)
    before = dump_fingerprint(driver)
    assert before["types"]["LINK"] >= 1
    plant_link(driver, world, second["id"], first["id"], meaning="depends_on")
    after = dump_fingerprint(driver)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["relationships"] == before["relationships"] + 1
    assert after["types"]["LINK"] == before["types"]["LINK"] + 1
