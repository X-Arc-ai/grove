"""Run the physical scenario's data contract locally, without any physical store operation."""

from grove.store.history_reads import item_history
from grove.store.query import WorldQuery
from grove.writer.restore import load_snapshot
from tests.writer.forget_drill import seed_forget_drill
from tests.writer.physical_scenario import PhysicalScenario, inventory
from tests.writer.test_restore import purge_world_rows


def test_history_reads_include_the_belief_side_row(driver, make_world):
    world = make_world()
    fixture = seed_forget_drill(driver, world)
    found = item_history(WorldQuery(driver, world), fixture["ids"]["belief"])
    assert {row["entity_type"] for row in found} >= {"item", "belief"}
    assert any(row["entity_type"] == "belief" and row["field"] == "row" for row in found)


def test_physical_scenario_data_contract_uses_logical_restore_only(
    driver, make_world, tmp_path, monkeypatch
):
    scenario = PhysicalScenario(tmp_path)
    scenario.world, scenario.control_world = make_world(), make_world()
    monkeypatch.setenv("GROVE_FORGET_LEDGER_DIR", str(scenario.ledger_dir))
    scenario.seed()
    scenario.forget()
    scenario.assert_clean("live")
    for world in (scenario.world, scenario.control_world):
        purge_world_rows(driver, world)
        load_snapshot(driver, world, scenario.before[world])
    scenario.assert_old_dump_contains_canary()
    scenario.restore_loaded("old_dump")
    scenario.assert_clean("old_dump")
    for world in (scenario.world, scenario.control_world):
        purge_world_rows(driver, world)
        load_snapshot(driver, world, scenario.clean[world])
    scenario.restore_loaded("next_dump")
    actual = inventory(driver, scenario.world, scenario.owner)
    assert actual == scenario.clean_inventory, inventory_delta(scenario.clean_inventory, actual)
    scenario.assert_clean("next_dump")
    assert set(scenario.results["target_types"]) == {
        "item",
        "field_value",
        "named_value",
        "source_span",
    }


def inventory_delta(before, after):
    def rows(inv):
        return {
            (tuple(r["labels"]), r["properties"].get("id", r["properties"].get("item_id", ""))): r[
                "properties"
            ]
            for r in inv["nodes"]
        }

    old, new = rows(before), rows(after)
    return [
        (
            key[0],
            sorted(
                k
                for k in old.get(key, {}).keys() | new.get(key, {}).keys()
                if old.get(key, {}).get(k) != new.get(key, {}).get(k)
            ),
        )
        for key in old.keys() | new.keys()
        if old.get(key) != new.get(key)
    ]
