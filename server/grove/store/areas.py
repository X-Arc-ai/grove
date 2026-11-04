"""Pure D11 area membership over one complete D7 capture.

The caller supplies D3 eligibility and D14 authorization independently. The inventory contains
scoped structural identities; an identity can exist without being eligible at the selected time.
Only their intersection contributes a path or a count. This module does not select historical
Area definitions: the record-axis adapter must supply the actual replayed definitions.

No claim text, database handle, model, clock provider, or mutable cache enters these functions.
The cleanup writer owns area discovery, partition selection, and the audit token budget.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

from grove.store.primitives import PRIMITIVE_MEANING_IDS, PRIMITIVE_WORLD
from grove.store.scope import check_world_id
from grove.strict_json import loads_strict

MAX_RULE_BYTES = 1_048_576
HUB_DEGREE = 12
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class AreaError(ValueError):
    """A structural error, with a safe code and no row values in its text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise AreaError("invalid_area_id")
    return value


def _bool(value: object) -> None:
    if type(value) is not bool:
        raise AreaError("invalid_area_graph")


def _ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise AreaError("invalid_partition_rule")
    result = tuple(_id(value) for value in values)
    if tuple(sorted(set(result))) != result:
        raise AreaError("invalid_partition_rule")
    return result


@dataclass(frozen=True)
class PartitionRule:
    by: str
    meaning_ids: tuple[str, ...] = ()
    second_level_hub_ids: tuple[str, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise AreaError("invalid_partition_rule")
        if self.by not in ("all", "meaning", "second_level_hub"):
            raise AreaError("invalid_partition_rule")
        if self.by == "all":
            if self.meaning_ids or self.second_level_hub_ids:
                raise AreaError("invalid_partition_rule")
        else:
            object.__setattr__(self, "meaning_ids", _ids(self.meaning_ids))
        if self.by == "second_level_hub":
            object.__setattr__(self, "second_level_hub_ids", _ids(self.second_level_hub_ids))
        elif self.second_level_hub_ids:
            raise AreaError("invalid_partition_rule")
        # Even empty collections cannot retain a caller-owned list.
        object.__setattr__(self, "meaning_ids", tuple(self.meaning_ids))
        object.__setattr__(self, "second_level_hub_ids", tuple(self.second_level_hub_ids))


def parse_partition_rule(raw: str) -> PartitionRule:
    """Parse only the version-1 stored format; never guess an older variant's meaning."""
    if not isinstance(raw, str):
        raise AreaError("invalid_partition_rule")

    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise AreaError("invalid_partition_rule")
            result[key] = value
        return result

    try:
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_RULE_BYTES:
            raise AreaError("invalid_partition_rule")
        loads_strict(encoded)
        data = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise AreaError("invalid_partition_rule")
        by = data.get("by")
        keys = {
            "all": {"version", "by"},
            "meaning": {"version", "by", "meaning_ids"},
            "second_level_hub": {"version", "by", "meaning_ids", "second_level_hub_ids"},
        }
        if not isinstance(by, str) or by not in keys or set(data) != keys[by]:
            raise AreaError("invalid_partition_rule")
        return PartitionRule(
            by=by,
            meaning_ids=_ids(data["meaning_ids"]) if "meaning_ids" in data else (),
            second_level_hub_ids=_ids(data["second_level_hub_ids"])
            if "second_level_hub_ids" in data
            else (),
            version=data["version"],
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise AreaError("invalid_partition_rule") from error


@dataclass(frozen=True)
class AreaDefinition:
    id: str
    world_id: str
    hub_item: str
    partition_rule: PartitionRule
    version: int = 1
    status: str = "active"
    merged_into: str | None = None

    def __post_init__(self) -> None:
        _id(self.id)
        _id(self.hub_item)
        check_world_id(self.world_id)
        if not isinstance(self.partition_rule, PartitionRule):
            raise AreaError("invalid_partition_rule")
        if type(self.version) is not int or self.version < 1:
            raise AreaError("invalid_area_definition")
        if self.status not in ("active", "merged", "retired"):
            raise AreaError("invalid_area_definition")
        if self.status == "merged":
            _id(self.merged_into)
            if self.merged_into == self.id:
                raise AreaError("area_merge_cycle")
        elif self.merged_into is not None:
            raise AreaError("invalid_area_definition")


@dataclass(frozen=True)
class AreaItem:
    id: str
    world_id: str


@dataclass(frozen=True)
class AreaLink:
    id: str
    world_id: str
    from_item: str
    to_item: str
    meaning: str


@dataclass(frozen=True)
class AreaMeaning:
    id: str
    world_id: str
    primitive: bool
    carries_consequences: bool
    decomposition: bool


def _rows(rows: tuple, expected_type: type) -> Mapping:
    indexed = {}
    for row in rows:
        if not isinstance(row, expected_type):
            raise AreaError("invalid_area_graph")
        _id(row.id)
        if row.id in indexed:
            raise AreaError("duplicate_area_graph_id")
        indexed[row.id] = row
    return MappingProxyType(indexed)


@dataclass(frozen=True)
class AreaGraph:
    """Complete scoped identities plus explicit time and disclosure selection.

    `area_ids` contains every same-capture Area identity, including ended definitions. Items
    absent from the selected temporal view remain in the inventory but not in `eligible_item_ids`.
    This permits a historical branch anchor to contribute no path without accepting foreign IDs.
    Completeness is a capture-adapter assertion; these pure functions cannot prove a DB snapshot.
    """

    world_id: str
    commit: int
    captured_at: datetime
    axis: str
    effective_at: datetime
    items: tuple[AreaItem, ...]
    links: tuple[AreaLink, ...]
    meanings: tuple[AreaMeaning, ...]
    area_ids: frozenset[str]
    eligible_item_ids: frozenset[str]
    eligible_link_ids: frozenset[str]
    authorized_item_ids: frozenset[str]
    authorized_link_ids: frozenset[str]
    authorized_area_ids: frozenset[str]
    complete: bool
    _items: Mapping[str, AreaItem] = field(init=False, repr=False, compare=False)
    _links: Mapping[str, AreaLink] = field(init=False, repr=False, compare=False)
    _meanings: Mapping[str, AreaMeaning] = field(init=False, repr=False, compare=False)
    _adjacent: Mapping[str, tuple[AreaLink, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        check_world_id(self.world_id)
        if type(self.commit) is not int or self.commit < 0:
            raise AreaError("invalid_area_graph")
        if self.axis not in ("current", "world", "record"):
            raise AreaError("invalid_area_graph")
        for instant in (self.captured_at, self.effective_at):
            if not isinstance(instant, datetime) or instant.utcoffset() is None:
                raise AreaError("invalid_area_graph")
        if self.complete is not True:
            raise AreaError("incomplete_area_graph")
        for name in ("items", "links", "meanings"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        items = _rows(self.items, AreaItem)
        links = _rows(self.links, AreaLink)
        meanings = _rows(self.meanings, AreaMeaning)
        for row in (*self.items, *self.links):
            if row.world_id != self.world_id:
                raise AreaError("area_scope_mismatch")
        for row in self.meanings:
            primitive_id = row.id in PRIMITIVE_MEANING_IDS
            if row.world_id != self.world_id and not (
                primitive_id and row.world_id == PRIMITIVE_WORLD
            ):
                raise AreaError("area_scope_mismatch")
            for flag in (row.primitive, row.carries_consequences, row.decomposition):
                _bool(flag)
        for name, inventory in (
            ("eligible_item_ids", items),
            ("authorized_item_ids", items),
            ("eligible_link_ids", links),
            ("authorized_link_ids", links),
            ("area_ids", None),
            ("authorized_area_ids", None),
        ):
            supplied = getattr(self, name)
            if isinstance(supplied, str):
                raise AreaError("invalid_area_graph")
            value = frozenset(_id(part) for part in supplied)
            if inventory is not None and not value <= inventory.keys():
                raise AreaError("invalid_area_reference")
            object.__setattr__(self, name, value)
        if not self.authorized_area_ids <= self.area_ids:
            raise AreaError("invalid_area_reference")
        visible = self.eligible_item_ids & self.authorized_item_ids
        adjacency: dict[str, list[AreaLink]] = {}
        for link in self.links:
            if (
                link.from_item not in items
                or link.to_item not in items
                or link.meaning not in meanings
            ):
                raise AreaError("invalid_area_reference")
            meaning = meanings[link.meaning]
            if (
                link.id not in self.eligible_link_ids
                or link.id not in self.authorized_link_ids
                or link.from_item not in visible
                or link.to_item not in visible
                or link.from_item == link.to_item
                or meaning.primitive
                or meaning.id in PRIMITIVE_MEANING_IDS
            ):
                continue
            adjacency.setdefault(link.from_item, []).append(link)
            adjacency.setdefault(link.to_item, []).append(link)
        object.__setattr__(self, "_items", items)
        object.__setattr__(self, "_links", links)
        object.__setattr__(self, "_meanings", meanings)
        object.__setattr__(
            self,
            "_adjacent",
            MappingProxyType(
                {
                    key: tuple(sorted(value, key=lambda row: row.id))
                    for key, value in adjacency.items()
                }
            ),
        )


@dataclass(frozen=True)
class AreaMembership:
    area_id: str
    area_version: int
    hub_item: str
    commit: int
    captured_at: datetime
    axis: str
    effective_at: datetime
    first_hop_ids: tuple[str, ...]
    second_hop_ids: tuple[str, ...]
    member_ids: tuple[str, ...]
    first_hop_link_ids: tuple[str, ...]
    path_link_ids: tuple[str, ...]
    status: str
    merged_into: str | None

    @property
    def member_count(self) -> int:
        return len(self.member_ids)


@dataclass(frozen=True)
class AreaIndexEntry:
    membership: AreaMembership
    successor_area_id: str | None


@dataclass(frozen=True)
class PartitionCoverage:
    hub_item: str
    first_hop_link_ids: tuple[str, ...]
    covered_link_ids: tuple[str, ...]
    uncovered_link_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.uncovered_link_ids


def _visible_item(graph: AreaGraph, item_id: str) -> bool:
    return item_id in graph.eligible_item_ids and item_id in graph.authorized_item_ids


def hub_degree(graph: AreaGraph, hub_item: str) -> int:
    """Count eligible disclosed non-primitive links; never disclose a hidden hub's degree."""
    if not _visible_item(graph, hub_item):
        raise AreaError("area_unavailable")
    return len(graph._adjacent.get(hub_item, ()))


def is_hub(graph: AreaGraph, item_id: str) -> bool:
    return hub_degree(graph, item_id) >= HUB_DEGREE


def _validate_definition(area: AreaDefinition, graph: AreaGraph) -> None:
    if area.world_id != graph.world_id:
        raise AreaError("area_scope_mismatch")
    if area.id not in graph.area_ids or (
        area.merged_into is not None and area.merged_into not in graph.area_ids
    ):
        raise AreaError("invalid_area_reference")
    if not _visible_item(graph, area.hub_item):
        raise AreaError("area_unavailable")
    if area.id not in graph.authorized_area_ids:
        raise AreaError("area_unavailable")
    for meaning_id in area.partition_rule.meaning_ids:
        row = graph._meanings.get(meaning_id)
        if row is None or row.primitive or row.id in PRIMITIVE_MEANING_IDS:
            raise AreaError("invalid_area_reference")
    if any(anchor not in graph._items for anchor in area.partition_rule.second_level_hub_ids):
        raise AreaError("invalid_area_reference")


def _other(link: AreaLink, item: str) -> str:
    return link.to_item if link.from_item == item else link.from_item


def area_membership(area: AreaDefinition, graph: AreaGraph) -> AreaMembership:
    """Compute the complete structural neighbourhood before any ranking or page limit."""
    _validate_definition(area, graph)
    rule = area.partition_rule
    meanings = frozenset(rule.meaning_ids)
    anchors = frozenset(rule.second_level_hub_ids)
    first: set[str] = set()
    first_links: set[str] = set()
    for link in graph._adjacent.get(area.hub_item, ()):
        other = _other(link, area.hub_item)
        if rule.by != "all" and link.meaning not in meanings:
            continue
        if rule.by == "second_level_hub" and other not in anchors:
            continue
        first.add(other)
        first_links.add(link.id)
    second: set[str] = set()
    path_links = set(first_links)
    for anchor in first:
        for link in graph._adjacent.get(anchor, ()):
            meaning = graph._meanings[link.meaning]
            if not (meaning.carries_consequences or meaning.decomposition):
                continue
            other = _other(link, anchor)
            if other == area.hub_item:
                continue
            path_links.add(link.id)
            if other not in first:
                second.add(other)
    return AreaMembership(
        area.id,
        area.version,
        area.hub_item,
        graph.commit,
        graph.captured_at,
        graph.axis,
        graph.effective_at,
        tuple(sorted(first)),
        tuple(sorted(second)),
        tuple(sorted({area.hub_item} | first | second)),
        tuple(sorted(first_links)),
        tuple(sorted(path_links)),
        area.status,
        area.merged_into if area.merged_into in graph.authorized_area_ids else None,
    )


def area_index(
    areas: tuple[AreaDefinition, ...],
    graph: AreaGraph,
    *,
    include_ended: bool = False,
) -> tuple[AreaIndexEntry, ...]:
    """Read the supplied registry; a record-axis caller supplies its replayed registry."""
    _bool(include_ended)
    rows = _rows(tuple(areas), AreaDefinition)
    if rows.keys() != graph.area_ids:
        raise AreaError("incomplete_area_registry")
    for area in rows.values():
        if area.world_id != graph.world_id:
            raise AreaError("area_scope_mismatch")

    def successor(area: AreaDefinition) -> str | None:
        seen = {area.id}
        current = area
        while current.merged_into is not None:
            target = rows.get(current.merged_into)
            if target is None:
                raise AreaError("invalid_area_reference")
            if target.id in seen:
                raise AreaError("area_merge_cycle")
            seen.add(target.id)
            if (
                not _visible_item(graph, target.hub_item)
                or target.id not in graph.authorized_area_ids
            ):
                return None
            current = target
        if current.id == area.id:
            return None
        return current.id

    result = []
    for area in sorted(rows.values(), key=lambda row: row.id):
        target = successor(area)
        if area.status != "active" and not include_ended:
            continue
        if not _visible_item(graph, area.hub_item) or area.id not in graph.authorized_area_ids:
            continue
        membership = area_membership(area, graph)
        # A raw immediate successor could bypass the visibility check on the resolved successor.
        if membership.merged_into is not None and target is None:
            membership = replace(membership, merged_into=None)
        result.append(AreaIndexEntry(membership, target))
    return tuple(result)


def partition_coverage(
    hub_item: str,
    areas: tuple[AreaDefinition, ...],
    graph: AreaGraph,
) -> PartitionCoverage:
    """Report uncovered first-hop paths; never repair a stale partition registry on a read."""
    hub_degree(graph, hub_item)
    expected = {link.id for link in graph._adjacent.get(hub_item, ())}
    covered: set[str] = set()
    seen: set[str] = set()
    for area in areas:
        if area.id in seen:
            raise AreaError("duplicate_area_graph_id")
        seen.add(area.id)
        if area.hub_item != hub_item:
            raise AreaError("area_partition_hub_mismatch")
        if area.status == "active":
            covered.update(area_membership(area, graph).first_hop_link_ids)
    return PartitionCoverage(
        hub_item,
        tuple(sorted(expected)),
        tuple(sorted(covered)),
        tuple(sorted(expected - covered)),
    )
