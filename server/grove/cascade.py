"""The pure D6 P0 walk: R1 endings, R2 marks and R4 proposals.

The caller supplies a world-scoped snapshot and typed triggers after validation. This module
never reads a clock, generates an id, calls a model, changes its input, or accesses storage.
The writer applies its result in the trigger's transaction; readers use the same result for
simulation. R3 completion and R5 date conflicts belong to P1.

Snapshot keys: items and meanings keyed by id; links as stored rows; now; latest_change keyed
by item id; prior_status keyed by item id with status and role; and r1_endings, each containing
link_id and trigger_id from a prior R1 ending. Inactive rows needed for reopen must remain in
links. Changes contain item_id, change_set_id, world_time, source_type, terminal,
person_correction and reopened. An unknown world_time remains None; it permits proposals only.
An optional end_clock is preserved on R1 operations. The caller selects changes to a role,
a propagating field,
a belief's weakening state, or an end. A reported_done role is a proposed terminal trigger.

Plans use ids and codes, not prose. Their dictionaries are newly allocated. A stored mark's
identity is (trigger_change_set, affected_item), including when a batch changes several roots.
"""

from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grove.store.current import TERMINAL_ROLES

MAX_DEPTH = 3
MAX_MARKS = 100
RULES = frozenset({"R1", "R2", "R4"})

Row = dict[str, Any]


@dataclass(frozen=True)
class CascadePlan:
    endings: tuple[Row, ...] = ()
    marks: tuple[Row, ...] = ()
    proposals: tuple[Row, ...] = ()


@dataclass(frozen=True)
class _Edge:
    row: Mapping[str, Any]
    upstream: str
    downstream: str
    provisional: bool

    @property
    def id(self) -> str:
        return self.row["id"]


def _instant(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("cascade time must be an aware datetime")
    return value


def _edges(snapshot: Mapping[str, Any]) -> list[_Edge]:
    meanings = snapshot["meanings"]
    # Validate the registry before evaluating any rule. A malformed consequence direction must
    # never silently become a rule that cannot match.
    for meaning in meanings.values():
        if meaning.get("carries_consequences") and meaning.get("downstream_end") not in {
            "from",
            "to",
        }:
            raise ValueError("a consequence meaning needs a declared downstream end")
    result = []
    for link in sorted(snapshot["links"], key=lambda row: row["id"]):
        meaning = meanings.get(link["meaning"])
        if meaning is None:
            raise ValueError("a link names an absent meaning")
        if not link.get("active_key") or not meaning.get("carries_consequences"):
            continue
        if meaning.get("decomposition"):
            # Decomposition survives the end of work. Its only cascade is P1 completion.
            continue
        source, target = link["from_item"], link["to_item"]
        if source not in snapshot["items"] or target not in snapshot["items"]:
            raise ValueError("a consequence link endpoint is absent from the snapshot")
        downstream, upstream = (
            (source, target) if meaning["downstream_end"] == "from" else (target, source)
        )
        result.append(_Edge(link, upstream, downstream, meaning.get("status") == "provisional"))
    return result


def _walk(root: str, outgoing: Mapping[str, list[_Edge]]) -> dict[str, tuple[list[str], bool]]:
    # Keep one visited set for each trust class. A short provisional path must not suppress a
    # longer canonical path, or make the canonical path's descendants provisional.
    queue = deque([(root, [], False)])
    visited = {(root, False), (root, True)}
    found: dict[str, tuple[list[str], bool]] = {}
    while queue:
        item, path, provisional = queue.popleft()
        if len(path) >= MAX_DEPTH:
            continue
        for edge in outgoing.get(item, ()):
            carried = provisional or edge.provisional
            key = (edge.downstream, carried)
            if key in visited:
                continue
            visited.add(key)
            next_path = [*path, edge.id]
            old = found.get(edge.downstream)
            if old is None or (old[1] and not carried):
                found[edge.downstream] = (next_path, carried)
            queue.append((edge.downstream, next_path, carried))
    return found


def _proposal(change: Mapping[str, Any], rule: str, target: str, action: str, **extra: Any) -> Row:
    return {
        "rule": rule,
        "proposal_type": "cascade",
        "target_item": target,
        "trigger_id": change["item_id"],
        "trigger_change_set": change["change_set_id"],
        "source_type": change["source_type"],
        "world_time": change["world_time"],
        "action": action,
        "link_ids": [],
        "mark_targets": [],
        "detail": {},
        **extra,
    }


def _terminal(item: Mapping[str, Any]) -> bool:
    return bool(item.get("end_class")) or item.get("status_role") in TERMINAL_ROLES


def _candidate_end(change: Mapping[str, Any], items: Mapping[str, Row]) -> bool:
    return bool(change.get("terminal")) or items[change["item_id"]].get("status_role") == (
        "reported_done"
    )


def _automatic(change: Mapping[str, Any], now: datetime) -> bool:
    return (
        bool(change.get("terminal"))
        and change["world_time"] is not None
        and change["world_time"] <= now
        and (change["source_type"] == "confirmed" or change.get("person_correction") is True)
    )


def _reopen(snapshot: Mapping[str, Any], change: Mapping[str, Any]) -> Row | None:
    links = {link["id"]: link for link in snapshot["links"]}
    restore = set()
    for ending in snapshot.get("r1_endings", ()):
        if ending["trigger_id"] != change["item_id"]:
            continue
        link = links.get(ending["link_id"])
        if link is None or link.get("active_key"):
            continue
        if change["item_id"] not in (link["from_item"], link["to_item"]):
            continue
        meaning = snapshot["meanings"][link["meaning"]]
        if meaning.get("carries_consequences") and not meaning.get("decomposition"):
            restore.add(link["id"])
    if not restore:
        return None
    return _proposal(
        change,
        "R4",
        change["item_id"],
        "reopen_links",
        proposal_type="reopen",
        link_ids=sorted(restore),
    )


def plan(snapshot: Mapping[str, Any], changes: Iterable[Mapping[str, Any]]) -> CascadePlan:
    """Return the deterministic plan for validated triggers, without changing the snapshot.

    The input contains all active links on the bounded paths and all active upstream links of
    their targets. Retain the original active snapshot while evaluating R2, including links R1
    ends. `prior_status` is the last status before blocked, read by the caller from history.
    A missing previous status yields a review, never an invented resume status.
    """
    now = _instant(snapshot["now"])
    items = snapshot["items"]
    ordered = sorted(
        changes,
        key=lambda change: (
            change["world_time"] is None,
            _instant(change["world_time"]) if change["world_time"] is not None else None,
            change["change_set_id"],
            change["item_id"],
        ),
    )
    for change in ordered:
        if change["item_id"] not in items:
            raise ValueError("a cascade trigger is absent from the snapshot")
        if change["source_type"] not in {"planned", "said", "reported_done", "confirmed"}:
            raise ValueError("a cascade trigger needs a source type")
    edges = _edges(snapshot)
    outgoing: dict[str, list[_Edge]] = defaultdict(list)
    incoming: dict[str, list[_Edge]] = defaultdict(list)
    touching: dict[str, list[_Edge]] = defaultdict(list)
    for edge in edges:
        outgoing[edge.upstream].append(edge)
        incoming[edge.downstream].append(edge)
        touching[edge.upstream].append(edge)
        if edge.downstream != edge.upstream:
            touching[edge.downstream].append(edge)

    endings: dict[str, Row] = {}
    proposals: list[Row] = []
    if "R1" in RULES:
        for change in ordered:
            if not _candidate_end(change, items) or change.get("reopened"):
                continue
            for edge in touching.get(change["item_id"], ()):
                if _automatic(change, now) and not edge.provisional:
                    endings.setdefault(
                        edge.id,
                        {
                            "link_id": edge.id,
                            "trigger_id": change["item_id"],
                            "change_set_id": change["change_set_id"],
                            "world_time": change["world_time"],
                            **(
                                {"end_clock": deepcopy(change["end_clock"])}
                                if "end_clock" in change
                                else {}
                            ),
                        },
                    )
                else:
                    proposals.append(
                        _proposal(
                            change,
                            "R1",
                            change["item_id"],
                            "end_link",
                            link_ids=[edge.id],
                            detail={"provisional": edge.provisional},
                            **(
                                {"end_clock": deepcopy(change["end_clock"])}
                                if "end_clock" in change
                                else {}
                            ),
                            mark_targets=[edge.downstream]
                            if edge.downstream != change["item_id"]
                            else [],
                        )
                    )

    marks: dict[tuple[str, str], Row] = {}
    for change in ordered:
        root = change["item_id"]
        reached = _walk(root, outgoing) if "R2" in RULES else {}
        wide = len(reached) > MAX_MARKS
        kept = {item: path for item, path in reached.items() if not wide or len(path[0]) == 1}
        if wide:
            counts = Counter(items[item]["kind"] for item in reached)
            proposals.append(
                _proposal(
                    change,
                    "R2",
                    root,
                    "wide_impact",
                    proposal_type="wide_impact",
                    detail={
                        "reached": len(reached),
                        "kept": len(kept),
                        "omitted": len(reached) - len(kept),
                        "counts_by_kind": dict(sorted(counts.items())),
                    },
                    mark_targets=sorted(
                        item for item, (_, provisional) in kept.items() if not provisional
                    ),
                )
            )
        for item, (path, provisional) in sorted(kept.items()):
            if provisional:
                proposals.append(
                    _proposal(
                        change,
                        "R2",
                        item,
                        "mark",
                        link_ids=list(path),
                        detail={"depth": len(path), "provisional": True},
                    )
                )
                continue
            latest = snapshot.get("latest_change", {}).get(item)
            state = (
                "born_resolved"
                if (
                    latest is not None
                    and change["world_time"] is not None
                    and latest > change["world_time"]
                )
                else "open"
            )
            key = (change["change_set_id"], item)
            mark = {
                "trigger_id": root,
                "trigger_change_set": change["change_set_id"],
                "affected_item": item,
                "rule": "R2",
                "depth": len(path),
                "path": list(path),
                "state": state,
                "source_type": change["source_type"],
                "world_time": change["world_time"],
            }
            # The schema has one mark per change set and affected item. Choose one stable path.
            if key not in marks:
                marks[key] = mark

        if "R4" not in RULES:
            continue
        if change.get("reopened"):
            proposal = _reopen(snapshot, change)
            if proposal is not None:
                proposals.append(proposal)
            continue
        if "R1" not in RULES or not _candidate_end(change, items):
            continue
        losses: dict[str, list[_Edge]] = defaultdict(list)
        for edge in outgoing.get(root, ()):
            if not _terminal(items[edge.downstream]):
                losses[edge.downstream].append(edge)
        for target, lost in sorted(losses.items()):
            lost_ids = {edge.id for edge in lost}
            # For an unconfirmed trigger, describe its hypothetical follow-up without applying
            # it. Other unconfirmed triggers do not remove real upstream links for this test.
            still_held = any(
                edge.id not in endings and edge.id not in lost_ids for edge in incoming[target]
            )
            previous = snapshot.get("prior_status", {}).get(target, {})
            resume = (
                items[target].get("status_role") == "blocked"
                and not still_held
                and previous.get("role") not in TERMINAL_ROLES | {"blocked", "reported_done"}
                and previous.get("status") is not None
                and previous.get("role") is not None
            )
            proposal = _proposal(
                change,
                "R4",
                target,
                "resume" if resume else "review",
                link_ids=sorted(lost_ids),
                mark_targets=[target] if target in kept and not kept[target][1] else [],
                detail={"provisional": any(edge.provisional for edge in lost)},
            )
            if resume:
                proposal["suggested_status"] = previous["status"]
            proposals.append(proposal)

    # Repeated trigger inputs are harmless, as are their repeated proposal computations.
    unique: dict[tuple[Any, ...], Row] = {}
    for proposal in proposals:
        key = (
            proposal["trigger_change_set"],
            proposal["trigger_id"],
            proposal["rule"],
            proposal["action"],
            proposal["target_item"],
            tuple(proposal["link_ids"]),
        )
        unique.setdefault(key, proposal)
    return CascadePlan(
        endings=tuple(endings[key] for key in sorted(endings)),
        marks=tuple(marks[key] for key in sorted(marks)),
        proposals=tuple(unique[key] for key in sorted(unique)),
    )
