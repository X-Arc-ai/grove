"""D12 dependency work, derived from retained changes and the single D6 planner.

Candidate discovery also follows declared dependency direction when its consequence flag
is false. That is review work, never an alternate calculation of automatic effects.
"""

import hashlib
import json
from datetime import datetime

from grove import cascade
from grove.store.cascade_reads import snapshot
from grove.store.current import read_rows
from grove.store.primitives import MEANING_DESCRIBES, PRIMITIVE_MEANINGS
from grove.writer.registry import Registry

MAX_REVIEWS = 400
FIELDS = frozenset(
    {"title", "body", "summary", "properties", "status_role", "end_class", "state", "reopened"}
)


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def review_key(row):
    return (row["trigger"], row["target"], row["cause_change_id"])


def pending_proposals(source):
    rows = []
    for found in read_rows(
        source, "MATCH (p:CascadeProposal {state: 'open'}) RETURN p ORDER BY p.id"
    ):
        proposal = found["p"]
        question = _decode(proposal.get("detail") or "{}").get("question_id")
        if question and read_rows(
            source, "MATCH (q:Question {id: $id, state: 'open'}) RETURN q.id AS id", id=question
        ):
            continue
        rows.append(proposal)
    return rows


def pending_reviews(source, *, since, now, registry=None):
    """Read-only work list. The writer calls this again under its world lock."""
    registry = registry or Registry.load(source)
    entries = read_rows(
        source,
        "MATCH (c:ChangeEntry) WHERE c.recorded_at > $since "
        "AND c.entity_type IN ['item', 'belief'] AND c.history_only = false "
        "RETURN c ORDER BY c.sequence",
        since=since,
    )
    ids = [row["c"].get("new_value_id") for row in entries if row["c"].get("new_value_id")]
    values = {
        row["id"]: row["value"]
        for row in read_rows(
            source,
            "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
            ids=ids,
        )
    }
    triggers, covered, reviewed = [], set(), set()
    for found in entries:
        entry = found["c"]
        if entry.get("field") not in {"cascade_trigger", "cascade_review"}:
            continue
        try:
            value = _decode(values.get(entry.get("new_value_id")))
        except (TypeError, ValueError):
            continue  # D14 may have physically removed the retained value.
        if not isinstance(value, dict):
            continue
        if entry.get("field") == "cascade_review":
            reviewed.add(review_key(value))
            continue
        value = dict(value)
        if isinstance(value.get("world_time"), str):
            value["world_time"] = datetime.fromisoformat(value["world_time"])
        value["cause_change_id"] = entry["sequence"]
        triggers.append(value)
        covered.add((entry["entity_id"], entry["change_set_id"]))
    # Older published history has no trigger marker. It gets conservative review authority;
    # never borrow a current item's stronger rank or treat a historical actor as the person.
    legacy = {}
    for found in entries:
        entry = found["c"]
        key = (entry["entity_id"], entry.get("change_set_id"))
        if entry.get("field") in FIELDS and key not in covered:
            legacy[key] = entry
    for entry in legacy.values():
        triggers.append(
            {
                "item_id": entry["entity_id"],
                "change_set_id": entry.get("change_set_id"),
                "world_time": entry.get("world_time"),
                "source_type": "said",
                "terminal": False,
                "reopened": False,
                "person_correction": False,
                "cause_change_id": entry["sequence"],
            }
        )
    meanings = {**registry.meanings, **PRIMITIVE_MEANINGS}
    dependencies = [
        key
        for key, row in meanings.items()
        if row.get("downstream_end") in {"from", "to"} and not row.get("decomposition")
    ]
    reviews = []
    for change in triggers:
        trigger = change["item_id"]
        held = snapshot(source, [trigger], now=now, registry=registry)
        if trigger not in held["items"]:
            continue
        planned = cascade.plan(held, [change])
        planned_marks = {row["affected_item"]: row for row in planned.marks}
        present_marks = {
            row["id"]
            for row in read_rows(
                source,
                "MATCH (m:Impact {trigger_change_set: $id}) RETURN m.affected_item AS id",
                id=change["change_set_id"],
            )
        }
        planned_targets = {row["affected_item"] for row in planned.marks}
        planned_targets.update(row["target_item"] for row in planned.proposals)
        frontier, paths = {trigger}, {trigger: []}
        path_versions, gaps = {trigger: []}, {trigger: False}
        for _ in range(3):
            if not frontier:
                break
            following = set()
            rows = read_rows(
                source,
                "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.meaning IN $meanings "
                "AND (l.from_item IN $ids OR l.to_item IN $ids) "
                "AND (l.active_key IS NOT NULL OR l.id IN $ended) RETURN l ORDER BY l.id",
                meanings=dependencies + [MEANING_DESCRIBES],
                ids=sorted(frontier),
                ended=[row["link_id"] for row in held["r1_endings"]],
            )
            edges = []
            for found in rows:
                link = found["l"]
                meaning = meanings[link["meaning"]]
                downstream = (
                    "from_item"
                    if link["meaning"] == MEANING_DESCRIBES
                    or meaning.get("downstream_end") == "from"
                    else "to_item"
                )
                upstream = "to_item" if downstream == "from_item" else "from_item"
                edges.append(
                    (
                        link[upstream],
                        link[downstream],
                        link["id"],
                        link.get("version", 1),
                        link["meaning"] != MEANING_DESCRIBES
                        and not meaning.get("carries_consequences"),
                    )
                )
            for found in read_rows(
                source,
                "MATCH (b:Belief) WHERE b.state <> 'superseded' "
                "AND any(id IN $ids WHERE id IN coalesce(b.support_items, [])) RETURN b",
                ids=sorted(frontier),
            ):
                belief = found["b"]
                edges.extend(
                    (root, belief["item_id"], None, None, False)
                    for root in frontier
                    if root in belief.get("support_items", [])
                )
            for upstream, target, link_id, link_version, wrong_flag in edges:
                if upstream not in frontier or target in paths:
                    continue
                paths[target] = paths[upstream] + ([link_id] if link_id else [])
                path_versions[target] = path_versions[upstream] + (
                    [link_version] if link_id else []
                )
                gaps[target] = gaps[upstream] or wrong_flag
                following.add(target)
                versions = read_rows(
                    source,
                    "MATCH (i:Item {id: $id}) OPTIONAL MATCH (b:Belief {item_id: $id}) "
                    "RETURN i.version AS item_version, b.version AS belief_version",
                    id=target,
                )
                if not versions:
                    continue
                row = {
                    "trigger": trigger,
                    "target": target,
                    "cause_change_id": change["cause_change_id"],
                    "path": paths[target],
                    "path_versions": path_versions[target],
                    **versions[0],
                    "flag_gap": gaps[target],
                    "planned": target in planned_targets,
                    "missing_mark": target in planned_marks and target not in present_marks,
                }
                if row["missing_mark"]:
                    mark = planned_marks[target]
                    row["mark"] = {
                        **mark,
                        "world_time": mark["world_time"].isoformat()
                        if mark["world_time"] is not None
                        else None,
                    }
                if review_key(row) not in reviewed:
                    row["review_token"] = hashlib.sha256(
                        json.dumps(row, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    reviews.append(row)
                    if len(reviews) > MAX_REVIEWS:
                        from grove.consolidation.packet import PacketTooLarge

                        raise PacketTooLarge("the complete dependency review exceeds its bound")
            frontier = following
    return reviews
