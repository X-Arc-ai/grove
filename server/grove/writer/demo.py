"""Dev tool: a synthetic world to look at, seeded from web/checks/fixture.synthetic.json.

It writes t_ worlds only, through grove.writer.testing, which refuses any other world id. It is
no part of the product: it fills a world for the browser test of the views and for a local look.

From server/, with NEO4J_AUTH set (`set -a; . ~/.grove/neo4j-dev.env; set +a`) and the store
migrated (`uv run python -m grove.writer.migrate`):

    uv run python -m grove.writer.demo seed --fixture ../web/checks/fixture.synthetic.json \
        --token-file <path>
    uv run python -m grove.writer.demo add --world <id> --count N
    uv run python -m grove.writer.demo purge --world <id>

seed writes the session token to the token file with mode 600 and prints only the world id and
the counts; the token never reaches the output. The session lasts one hour, as plant_session's.
The fixture holds the simulation's graph shape: `elements.nodes[].data`, `elements.edges[].data`
and `person_id`, the person's own node.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from neo4j import Driver

from grove import clock
from grove.ids import new_id
from grove.store.connection import open_driver
from grove.store.query import WorldQuery
from grove.writer.testing import (
    _refuse_real_world,
    plant_link,
    plant_node,
    plant_session,
    purge_test_world,
    seed_test_world,
)

# The simulation's visual types that are no starter kind, and the starter kind each becomes.
# person, task, decision, event and observation are starter kinds of the same name.
KIND_OF_TYPE = {
    "company": "organisation",
    "client": "organisation",
    "project": "plan",
    "pattern": "assumption",
}
# A meaning that the starter set lacks is planted as a world row with this cardinality.
PLANTED_CARDINALITY = "many"
DAY = "day"
INSTANT = "instant"
WORLD_PREFIX = "t_demo_"

# What `add` plants: invented titles, the kinds they take turns in, and the meaning that links
# each one to an item already there.
ADD_KINDS = ("task", "observation", "event", "decision")
ADD_VERBS = ("Sketch", "Review", "Confirm", "Plan", "Draft", "Book")
ADD_THINGS = (
    "the tasting menu",
    "the spring catalogue",
    "the studio lease",
    "the reading list",
    "the harbour walk",
    "the print proofs",
    "the garden plan",
)
ADD_MEANING = "related to"
ADD_SUMMARY = "An invented item that the demo seeder added"

_KINDS = "MATCH (k:Kind) RETURN k.id AS id, k.name AS name"
_MEANINGS = "MATCH (m:Meaning) RETURN m.id AS id, m.name AS name"
_ITEMS = "MATCH (i:Item) RETURN i.id AS id ORDER BY i.id"


@dataclass(frozen=True)
class Seeded:
    """What seed_world planted. The token has no repr, so no report or log can show it."""

    world_id: str
    token: str = field(repr=False)
    items: dict[str, str]  # fixture node id -> item id
    links: int
    ended_links: int
    links_skipped: int
    meanings_added: list[str]
    self_item_id: str | None

    def counts(self) -> dict[str, Any]:
        return {
            "world_id": self.world_id,
            "items": len(self.items),
            "links": self.links,
            "ended_links": self.ended_links,
            "links_skipped": self.links_skipped,
            "meanings_added": len(self.meanings_added),
            "self_item": self.self_item_id is not None,
        }


def instant_of(text: object) -> datetime | None:
    """A fixture time ('2026-10-03T09:00:00Z') as an aware UTC datetime; None when absent."""
    if not isinstance(text, str) or not text:
        return None
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a fixture time must carry a timezone")
    return value.astimezone(UTC)


def active_key(world_id: str, from_item: str, meaning_id: str, to_item: str) -> str:
    """S7: world, from, meaning and to joined by a bar, present only while the link is active."""
    return "|".join((world_id, from_item, meaning_id, to_item))


def kind_name_of(sim_type: str) -> str:
    return KIND_OF_TYPE.get(sim_type, sim_type)


def _present(row: dict[str, Any]) -> dict[str, Any]:
    """Null is no property value: leave an absent field out of the row."""
    return {key: value for key, value in row.items() if value is not None and value != ""}


def _names(driver: Driver, world_id: str, cypher: str) -> dict[str, str]:
    return {row["name"]: row["id"] for row in WorldQuery(driver, world_id).read(cypher)}


def _item_row(node: dict[str, Any], kind_id: str) -> dict[str, Any]:
    stopped = instant_of(node.get("invalid_at"))
    version = node.get("version")
    return _present(
        {
            "kind": kind_id,
            "title": node.get("label"),
            "summary": node.get("summary"),
            "status": node.get("status"),
            "status_since": instant_of(node.get("status_since")),
            "recorded": instant_of(node.get("created_at")),
            "became_true_earliest": instant_of(node.get("valid_at")),
            "became_true_precision": DAY if node.get("valid_at") else None,
            "stopped_being_true_latest": stopped,
            "stopped_being_true_precision": DAY if stopped else None,
            "touched_at": instant_of(node.get("updated_at")),
            "last_confirmed": instant_of(node.get("verified_at")),
            "version": version if isinstance(version, int) and version > 0 else 1,
            "properties": json.dumps(node.get("properties") or {}, sort_keys=True),
        }
    )


def seed_world(
    driver: Driver, world_id: str, fixture: dict[str, Any], *, owner_user_id: str | None = None
) -> Seeded:
    """Create the world with its starter set, then plant the fixture's items, links and session.

    A link whose fixture row has invalid_at has ended: it gets its stopped bound and no
    active_key (S7). A second active link with the same world, from, meaning and to is skipped,
    because active_key is unique.
    """
    _refuse_real_world(world_id)
    nodes = [wrapper["data"] for wrapper in fixture["elements"]["nodes"]]
    edges = [wrapper["data"] for wrapper in fixture["elements"]["edges"]]
    owner = owner_user_id or new_id()
    seed_test_world(driver, world_id, owner_user_id=owner)
    kinds = _names(driver, world_id, _KINDS)
    meanings = _names(driver, world_id, _MEANINGS)

    items: dict[str, str] = {}
    for node in nodes:
        kind_id = kinds.get(kind_name_of(node["type"]))
        if kind_id is None:
            raise ValueError(f"no starter kind for the simulation type {node['type']!r}")
        items[node["id"]] = plant_node(driver, world_id, "Item", **_item_row(node, kind_id))["id"]

    added: list[str] = []
    keys: set[str] = set()
    links = ended = skipped = 0
    for edge in edges:
        verb = edge.get("verb") or edge.get("label")
        if verb not in meanings:
            meanings[verb] = plant_node(
                driver,
                world_id,
                "Meaning",
                name=verb,
                cardinality=PLANTED_CARDINALITY,
                primitive=False,
                version=1,
            )["id"]
            added.append(verb)
        from_item, to_item = items[edge["source"]], items[edge["target"]]
        meaning_id = meanings[verb]
        started = instant_of(edge.get("valid_at"))
        stopped = instant_of(edge.get("invalid_at"))
        row = _present(
            {
                "meaning": meaning_id,
                "recorded": started,
                "became_true_earliest": started,
                "became_true_precision": DAY if started else None,
                "stopped_being_true_latest": stopped,
                "stopped_being_true_precision": DAY if stopped else None,
                "version": 1,
            }
        )
        if stopped is None:
            key = active_key(world_id, from_item, meaning_id, to_item)
            if key in keys:
                skipped += 1
                continue
            keys.add(key)
            row["active_key"] = key
        else:
            ended += 1
        plant_link(driver, world_id, from_item, to_item, **row)
        links += 1

    own = fixture.get("person_id")
    self_item_id = items.get(own) if isinstance(own, str) else None
    token = plant_session(driver, world_id, user_id=owner, self_item_id=self_item_id)
    return Seeded(
        world_id=world_id,
        token=token,
        items=items,
        links=links,
        ended_links=ended,
        links_skipped=skipped,
        meanings_added=added,
        self_item_id=self_item_id,
    )


def add_items(driver: Driver, world_id: str, count: int) -> list[str]:
    """Plant `count` invented items, each with an active link to an item already in the world.

    Returns the new item ids in the order they were planted.
    """
    _refuse_real_world(world_id)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer")
    existing = [row["id"] for row in WorldQuery(driver, world_id).read(_ITEMS)]
    if not existing:
        raise ValueError("the world has no item to link a new item to")
    kinds = _names(driver, world_id, _KINDS)
    meaning_id = _names(driver, world_id, _MEANINGS).get(ADD_MEANING)
    if meaning_id is None:
        raise ValueError("the world has no starter meaning to link a new item with")
    at = clock.now(world_id)
    added = []
    for index in range(count):
        number = len(existing) + index + 1
        title = (
            f"{ADD_VERBS[index % len(ADD_VERBS)]} "
            f"{ADD_THINGS[(index // len(ADD_VERBS)) % len(ADD_THINGS)]} {number}"
        )
        item = plant_node(
            driver,
            world_id,
            "Item",
            kind=kinds[ADD_KINDS[index % len(ADD_KINDS)]],
            title=title,
            summary=ADD_SUMMARY,
            recorded=at,
            became_true_earliest=at,
            became_true_precision=INSTANT,
            touched_at=at,
            version=1,
            properties="{}",
        )["id"]
        # Spread the new items over the world: a step that is prime to most sizes.
        target = existing[(index * 37 + len(existing) // 3) % len(existing)]
        plant_link(
            driver,
            world_id,
            item,
            target,
            meaning=meaning_id,
            recorded=at,
            became_true_earliest=at,
            became_true_precision=INSTANT,
            version=1,
            active_key=active_key(world_id, item, meaning_id, target),
        )
        added.append(item)
    return added


def purge(driver: Driver, world_id: str) -> None:
    purge_test_world(driver, world_id)


def write_token(path: Path, token: str) -> None:
    """Mode 600 from the first byte, also when the file was there before."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m grove.writer.demo", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="create a t_ world from a synthetic fixture")
    seed.add_argument("--fixture", required=True, type=Path)
    seed.add_argument("--token-file", required=True, type=Path)
    seed.add_argument("--world", help="a t_ world id; default: a new t_demo_ id")
    add = commands.add_parser("add", help="plant N invented items, each linked to an item")
    add.add_argument("--world", required=True)
    add.add_argument("--count", required=True, type=int)
    gone = commands.add_parser("purge", help="remove a t_ world and every row planted for it")
    gone.add_argument("--world", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    world_id = args.world if args.world is not None else f"{WORLD_PREFIX}{uuid4()}"
    try:
        _refuse_real_world(world_id)
    except ValueError as refused:
        print(f"grove.writer.demo: {refused}", file=sys.stderr)
        return 2
    with open_driver() as driver:
        if args.command == "seed":
            fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
            seeded = seed_world(driver, world_id, fixture)
            write_token(args.token_file, seeded.token)
            print(json.dumps(seeded.counts()))
        elif args.command == "add":
            added = add_items(driver, world_id, args.count)
            print(json.dumps({"world_id": world_id, "added": len(added)}))
        else:
            purge(driver, world_id)
            print(json.dumps({"world_id": world_id, "purged": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
