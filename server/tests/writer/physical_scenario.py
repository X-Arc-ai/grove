"""The real D14 scenario used by the isolated physical backup command.

The broad fixture also runs in local logical tests. This adapter retains its old and
sanitized snapshots across service restarts, while each method closes its own driver.
Only ids, hashes, counts and check names leave the private scenario directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from grove import clock
from grove.api.app import create_app
from grove.api.config import ApiSettings
from grove.ids import new_id
from grove.store.asof import world_as_of
from grove.store.catalogue import FULLTEXT_INDEXES, PLATFORM_LABELS
from grove.store.connection import open_driver
from grove.store.history_reads import item_history
from grove.store.query import WorldQuery, plain
from grove.store.replay import known_as_of
from grove.views.graph import Window, node_payload
from grove.views.session import SESSION_COOKIE
from grove.writer.migrate import run_migrations
from grove.writer.resolver import lexical_candidates
from grove.writer.restore import _tag, _untag, take_snapshot
from grove.writer.testing import plant_session, refuse_real_world
from grove.writer.worlds import seed_primitives
from tests.writer.physical_backup import (
    AUX_ENTRY_LIMIT,
    AUX_LIMIT,
    BUFFER_SIZE,
    MANIFEST_LIMIT,
    Refused,
    Usage,
    measure_tree,
    require,
)


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def private_manifest(root: Path, name: str, value: Any) -> None:
    """Keep the restore metadata only in the caller's private drill directory."""
    require(name in {"baseline.json", "sanitized.json"}, "invalid_manifest_name")
    path = root / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = 0
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, default=str)
        with os.fdopen(descriptor, "wb") as handle:
            for piece in encoder.iterencode(value):
                # A scalar may be one encoder chunk. Reject its character lower
                # bound before any UTF-8 allocation, then encode bounded slices.
                require(written + len(piece) <= MANIFEST_LIMIT, "manifest_size_limit")
                for offset in range(0, len(piece), BUFFER_SIZE // 4):
                    data = piece[offset : offset + BUFFER_SIZE // 4].encode("utf-8")
                    require(written + len(data) <= MANIFEST_LIMIT, "manifest_size_limit")
                    handle.write(data)
                    written += len(data)
            handle.flush()
            os.fsync(handle.fileno())
        # These are the scenario-owned auxiliary files, including both atomic
        # ledger replacement paths. The orchestrator also counts every root file.
        usage = Usage()
        for name in ("baseline.json", "sanitized.json", "ledger"):
            held = root / name
            if held.exists():
                usage += measure_tree(root, held, AUX_LIMIT, entry_limit=AUX_ENTRY_LIMIT)
        require(usage.bytes <= AUX_LIMIT, "auxiliary_size_limit")
        require(usage.entries <= AUX_ENTRY_LIMIT, "auxiliary_entry_limit")
    except BaseException:
        path.unlink()
        raise


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def inventory(driver, world: str, owner: str) -> dict[str, Any]:
    """Every world row and relationship, plus its complete account/session records."""
    refuse_real_world(world)
    with driver.session(database="neo4j") as session:
        nodes = session.run(
            "CYPHER 25 MATCH (n) WHERE n.world_id = $world OR n.planted_for = $world "
            "OR (n:World AND n.id = $world) OR (n:User AND n.id = $owner) "
            "OR n.user_id = $owner "
            "OR (n:OAuthClient AND EXISTS { MATCH (g:OAuthGrant {world_id: $world}) "
            "WHERE g.client_id = n.client_id }) "
            "OR (n:EmailCode AND EXISTS { MATCH (u:User {id: $owner}) "
            "WHERE u.email_hash = n.email_hash }) "
            "RETURN labels(n) AS labels, properties(n) AS properties",
            world=world,
            owner=owner,
        ).data()
        links = session.run(
            "CYPHER 25 MATCH (a)-[r]->(b) WHERE r.world_id = $world "
            "OR a.world_id = $world OR b.world_id = $world "
            "RETURN type(r) AS type, properties(r) AS properties, a.id AS start, b.id AS end",
            world=world,
        ).data()
    nodes, links = plain(nodes), plain(links)
    for row in nodes:
        row["labels"] = sorted(row["labels"])
    return {"nodes": sorted(nodes, key=encoded), "relationships": sorted(links, key=encoded)}


def primitive_ids(driver) -> list[dict[str, Any]]:
    with driver.session(database="neo4j") as session:
        rows = session.run(
            "CYPHER 25 MATCH (n) WHERE n.world_id = '' RETURN labels(n) AS labels, n.id AS id"
        ).data()
    for row in rows:
        row["labels"] = sorted(row["labels"])
    require(bool(rows) and all(row["id"] for row in rows), "primitive_identities_absent")
    return sorted(rows, key=encoded)


def platform_records(rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "labels": row["labels"],
            "properties": {key: _tag(value) for key, value in row["properties"].items()},
        }
        for row in rows["nodes"]
        if set(row["labels"]) <= PLATFORM_LABELS
    ]


def restore_platform_records(
    driver, records: list[dict[str, Any]], worlds: set[str], owners: set[str]
) -> None:
    """Only the synthetic account rows exported above enter the new private store."""
    for world in worlds:
        refuse_real_world(world)
    clients = {
        row["properties"].get("client_id")
        for row in records
        if "OAuthGrant" in row["labels"] and row["properties"].get("world_id") in worlds
    } - {None}
    emails = {
        row["properties"].get("email_hash")
        for row in records
        if "User" in row["labels"] and row["properties"].get("id") in owners
    } - {None}
    with driver.session(database="neo4j") as session:
        for row in records:
            labels = set(row["labels"])
            require(bool(labels) and labels <= PLATFORM_LABELS, "invalid_platform_label")
            props = {key: _untag(value) for key, value in row["properties"].items()}
            belongs = (
                props.get("world_id") in worlds
                or props.get("planted_for") in worlds
                or props.get("user_id") in owners
                or ("World" in labels and props.get("id") in worlds)
                or ("User" in labels and props.get("id") in owners)
                or ("OAuthClient" in labels and props.get("client_id") in clients)
                or ("EmailCode" in labels and props.get("email_hash") in emails)
            )
            require(belongs, "invalid_platform_owner")
            session.run(
                "CYPHER 25 CREATE (n:" + ":".join(sorted(labels)) + ") SET n = $properties",
                properties=props,
            ).consume()


class PhysicalScenario:
    def __init__(self, private_root: Path) -> None:
        self.private_root = private_root
        self.ledger_dir = private_root / "ledger"
        self.ledger_dir.mkdir(mode=0o700)
        self.world = "t_ci-forget_" + str(uuid4())
        self.control_world = "t_ci-forget_" + str(uuid4())
        self.owner = new_id()
        self.control_owner = new_id()
        self.before: dict[str, dict[str, Any]] = {}
        self.clean: dict[str, dict[str, Any]] = {}
        self.platform: list[dict[str, Any]] = []
        self.results: dict[str, Any] = {"checks": {}, "target_types": []}
        self.ledger: list[dict[str, Any]] = []
        self.fixture: dict[str, Any] = {}
        self.control_inventory: dict[str, Any] = {}
        self.clean_inventory: dict[str, Any] = {}
        self.history: list[dict[str, Any]] = []
        self.primitives: list[dict[str, Any]] = []
        self.token = ""

    @staticmethod
    def _driver():
        return open_driver(
            connection_timeout=3, connection_acquisition_timeout=3, max_transaction_retry_time=0
        )

    def seed(self) -> None:
        from tests.writer.forget_drill import seed_forget_drill

        with self._driver() as driver:
            self.fixture = seed_forget_drill(driver, self.world)
            self.primitives = primitive_ids(driver)
            # A complete second world proves that reconstruction retains unrelated data.
            seed_forget_drill(driver, self.control_world)
            self.token = plant_session(driver, self.world, user_id=self.owner)
            plant_session(driver, self.control_world, user_id=self.control_owner)
            for world in (self.world, self.control_world):
                self.before[world] = take_snapshot(driver, world)
            self.control_inventory = inventory(driver, self.control_world, self.control_owner)
            self.history = [
                row
                for row in inventory(driver, self.world, self.owner)["nodes"]
                if "ChangeEntry" in row["labels"]
            ]
            query = WorldQuery(driver, self.world)
            item_id = self.fixture["ids"]["item"]
            item_canary = self.fixture["canaries"][1]
            require(
                any(
                    row["node"]["id"] == item_id
                    for row in query.search("grove_items_lexical", item_canary, limit=100)
                ),
                "baseline_fulltext_control_absent",
            )
            require(
                any(
                    row.item_id == item_id
                    for row in lexical_candidates(query, self.fixture["ids"]["kind"], item_canary)
                ),
                "baseline_trigram_control_absent",
            )
        private_manifest(
            self.private_root,
            "baseline.json",
            {
                "snapshots": self.before,
                "control_inventory": self.control_inventory,
                "primitive_ids": self.primitives,
            },
        )
        require(len(self.fixture.get("canaries", [])) >= 1, "fixture_has_no_canary")
        self.results["baseline_fingerprints"] = {
            world: snapshot["fingerprint"]["fingerprint"] for world, snapshot in self.before.items()
        }
        self.results["control_inventory_sha256"] = digest(self.control_inventory)
        self.results["history_sha256"] = digest(self.history)
        self.results["history_entries"] = len(self.history)
        self.results["primitive_identity_sha256"] = digest(self.primitives)

    def forget(self) -> None:
        from grove.writer.forget_ledger import read_ledger

        # This environment belongs only to the short-lived CI drill process.
        os.environ["GROVE_FORGET_LEDGER_DIR"] = str(self.ledger_dir)
        with self._driver() as driver:
            app = create_app(ApiSettings(public_base_url="http://127.0.0.1:8000"), driver=driver)
            with TestClient(app, base_url="http://127.0.0.1:8000") as client:
                client.cookies.set(SESSION_COOKIE, self.token)
                for target in self.fixture["targets"]:
                    approval = client.post(
                        "/v1/forget/approve",
                        json={"target": target, "confirm": True},
                        headers={"Origin": "http://127.0.0.1:8000"},
                    )
                    require(approval.status_code == 200, "approval_api_failed")
                    token = approval.json()["approval_token"]
                    response = client.post(
                        "/v1/forget",
                        json={"target": target, "approval_token": token},
                        headers={"Origin": "http://127.0.0.1:8000"},
                    )
                    require(response.status_code == 200, "forget_api_failed")
                    self._assert_no_canary(response.json())
                    self.results["target_types"].append(target["type"])
        self.ledger = read_ledger(self.ledger_dir, self.world)
        require(bool(self.ledger), "latest_ledger_empty")
        self._assert_no_canary(self.ledger)
        self.results["ledger_sha256"] = digest(self.ledger)

    def _assert_no_canary(self, value: Any) -> None:
        text = encoded(value).casefold()
        if any(str(canary).casefold() in text for canary in self.fixture["canaries"]):
            raise AssertionError("forbidden_canary")

    def assert_old_dump_contains_canary(self) -> None:
        from tests.writer.forget_drill import assert_forget_drill

        with self._driver() as driver:
            rows = inventory(driver, self.world, self.owner)
            try:
                self._assert_no_canary(rows)
            except AssertionError:
                pass
            else:
                raise Refused("old_dump_control_has_no_canary")
            try:
                assert_forget_drill(driver, self.fixture)
            except AssertionError:
                self.results["old_dump_control"] = "expected_failure"
            else:
                raise Refused("omitted_ledger_control_survived")

    def restore_loaded(self, stage: str) -> None:
        from grove.writer.forget_ledger import read_ledger
        from grove.writer.restore import restore_loaded

        latest = read_ledger(self.ledger_dir, self.world)
        require(digest(latest) == self.results["ledger_sha256"], "latest_ledger_changed")
        snapshots = self.before if stage == "old_dump" else self.clean
        with self._driver() as driver:
            for world in (self.world, self.control_world):
                result = restore_loaded(
                    driver,
                    world,
                    snapshots[world],
                    latest if world == self.world else [],
                    ledger_dir=self.ledger_dir,
                )
                self.results.setdefault("restored_fingerprints", {})[stage + ":" + world] = result[
                    "fingerprint"
                ]

    def _assert_read_surfaces(self, driver) -> dict[str, Any]:
        query = WorldQuery(driver, self.world)
        for index in FULLTEXT_INDEXES:
            for canary in self.fixture["canaries"]:
                require(not query.search(index.name, canary, limit=100), "fulltext_value_survived")
        for canary in self.fixture["canaries"]:
            candidates = lexical_candidates(query, self.fixture["ids"]["kind"], canary)
            require(
                all(row.item_id != self.fixture["ids"]["item"] for row in candidates),
                "trigram_item_survived",
            )
        retained = lexical_candidates(
            query, self.fixture["ids"]["kind"], self.fixture["retained_word"]
        )
        require(
            any(row.item_id == self.fixture["ids"]["control"] for row in retained),
            "retained_trigram_control_absent",
        )
        for name in ("item", "field", "control", "belief"):
            self._assert_no_canary(item_history(query, self.fixture["ids"][name]))
        from tests.writer.forget_drill import AT

        for at in (AT, clock.now(self.world)):
            for axis in (world_as_of(query, at), known_as_of(query, at)):
                self._assert_no_canary([held.row for held in axis.items.values()])
                self._assert_no_canary([held.row for held in axis.links.values()])
                require(self.fixture["ids"]["item"] not in axis.items, "forgotten_asof_item")
        for name in ("item", "field", "control", "belief"):
            detail = node_payload(
                query,
                WorldQuery.primitives(driver),
                self.fixture["ids"][name],
                window=Window(),
                self_item_id=None,
            )
            self._assert_no_canary(detail)
            if name == "item":
                require(detail is None, "forgotten_item_page")
        self._assert_no_canary(take_snapshot(driver, self.world))
        current = [
            row
            for row in inventory(driver, self.world, self.owner)["nodes"]
            if "ChangeEntry" in row["labels"]
        ]
        require(current == self.history, "history_structure_changed")
        return {
            "fulltext_indexes": [index.name for index in FULLTEXT_INDEXES],
            "trigram": "PASS",
            "item_history": "PASS",
            "history_structure": "PASS",
            "asof_axes": "PASS",
            "item_page": "PASS",
            "logical_export": "PASS",
        }

    def assert_clean(self, stage: str) -> None:
        from tests.writer.forget_drill import assert_forget_drill, reimport_forget_drill

        with self._driver() as driver:
            counts = assert_forget_drill(driver, self.fixture)
            require(primitive_ids(driver) == self.primitives, "primitive_identities_changed")
            read_surfaces = self._assert_read_surfaces(driver)
            self._assert_no_canary(inventory(driver, self.world, self.owner))
            reimport_forget_drill(driver, self.fixture)
            assert_forget_drill(driver, self.fixture)
            self._assert_no_canary(inventory(driver, self.world, self.owner))
            control = inventory(driver, self.control_world, self.control_owner)
            require(control == self.control_inventory, "control_world_changed")
            if stage == "old_dump":
                self.platform = []
                for world, owner in (
                    (self.world, self.owner),
                    (self.control_world, self.control_owner),
                ):
                    self.clean[world] = take_snapshot(driver, world)
                    rows = inventory(driver, world, owner)
                    self.platform.extend(
                        row for row in platform_records(rows) if row not in self.platform
                    )
                    if world == self.world:
                        self.clean_inventory = rows
                self._assert_no_canary(self.clean[self.world])
                private_manifest(
                    self.private_root,
                    "sanitized.json",
                    {
                        "snapshots": self.clean,
                        "platform": self.platform,
                        "inventory": self.clean_inventory,
                        "primitive_ids": self.primitives,
                    },
                )
            elif stage in ("clean_reconstruction", "next_dump"):
                require(
                    inventory(driver, self.world, self.owner) == self.clean_inventory,
                    "sanitized_world_changed",
                )
            self.results["checks"][stage] = {
                "columns": "PASS",
                "counts": counts,
                **read_surfaces,
                "repeat_import": "PASS",
                "control_world_all_rows": "PASS",
            }

    def reconstruct_clean(self) -> None:
        from grove.writer.restore import restore

        require(bool(self.clean) and bool(self.platform), "sanitized_export_absent")
        with self._driver() as driver:
            run_migrations(driver)
            seed_primitives(driver)
            restore_platform_records(
                driver,
                self.platform,
                {self.world, self.control_world},
                {self.owner, self.control_owner},
            )
            for world in (self.world, self.control_world):
                restore(
                    driver,
                    world,
                    self.clean[world],
                    self.ledger if world == self.world else [],
                    ledger_dir=self.ledger_dir,
                )

    def summary(self) -> dict[str, Any]:
        require(
            set(self.results["target_types"])
            == {"item", "field_value", "named_value", "source_span"},
            "incomplete_target_types",
        )
        require(
            set(self.results["checks"])
            == {"live", "old_dump", "clean_reconstruction", "next_dump"},
            "incomplete_checks",
        )
        require(
            self.results.get("old_dump_control") == "expected_failure",
            "omitted_ledger_control_absent",
        )
        return self.results


def make_scenario(private_root: Path) -> PhysicalScenario:
    return PhysicalScenario(private_root)
