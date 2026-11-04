"""D14's deterministic, content-free erasure plan and its in-memory application.

The plan names rows, columns and SHA-256 digests of complete text leaves. It never retains
the person's search value. Matching is exhaustive over the allowed text homes, including
JSON leaves; an index hit is not sufficient for an erasure guarantee.
"""

import copy
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from grove.store.catalogue import TABLES, table
from grove.store.normalise import normalise_phrase
from grove.store.replay import FORGOTTEN_MARKER, ReplayRefused
from grove.writer.errors import InvalidRecord

WORLD_TABLES = {t.name: t for t in TABLES if t.scope == "world"}
TEXT = {
    name: {p for c in t.columns if c.type in {"text_home", "json_home"} for p in c.props}
    for name, t in WORLD_TABLES.items()
}
PLAN_KEYS = {
    "version",
    "hashes",
    "delete",
    "mark_values",
    "history_fields",
    "item_fields",
    "source_ids",
    "end_evidence",
    "clear_examples",
    "locators",
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(text):
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def leaves(value: Any, depth: int = 0):
    if depth > 40:
        raise InvalidRecord("invalid_arguments", "text_depth", "stored text is too deeply nested")
    if isinstance(value, str):
        parsed = _json(value)
        if isinstance(parsed, dict | list):
            yield from leaves(parsed, depth + 1)
        else:
            yield value
    elif isinstance(value, dict):
        for key, part in value.items():
            yield key
            yield from leaves(part, depth + 1)
    elif isinstance(value, list):
        for part in value:
            yield from leaves(part, depth + 1)


def _token(value: str) -> str:
    return "forgotten-" + digest(value)[:24]


def _redact(value: Any, hashes: set[str], depth: int = 0, *, vocabulary: bool = False):
    if depth > 40:
        raise InvalidRecord("invalid_arguments", "text_depth", "stored text is too deeply nested")
    if isinstance(value, str):
        parsed = _json(value)
        if isinstance(parsed, dict | list):
            changed = _redact(parsed, hashes, depth + 1, vocabulary=vocabulary)
            return (
                value
                if changed == parsed
                else json.dumps(changed, sort_keys=True, ensure_ascii=False)
            )
        if digest(value) in hashes:
            return _token(value) if vocabulary else FORGOTTEN_MARKER
        return value
    if isinstance(value, dict):
        return {
            (_token(k) if digest(k) in hashes else k): _redact(
                v, hashes, depth + 1, vocabulary=vocabulary and k == "status"
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, hashes, depth + 1, vocabulary=vocabulary) for v in value]
    return value


def _content(name: str, row: dict, hashes: set[str]) -> dict:
    result = dict(row)
    for field in TEXT.get(name, ()):
        if field not in row:
            continue
        result[field] = _redact(
            row[field], hashes, vocabulary=field in {"status", "status_vocabulary"}
        )
        if field == "name" and result[field] != row[field]:
            result[field] = "forgotten-" + row["id"]
    return result


def selector(name: str, row: dict) -> dict:
    keys = ("id",) if "id" in row else table(name).uniques[0].props
    return {"table": name, "key": {k: str(row[k]) for k in keys if k != "world_id"}}


def _matches(row: dict, key: dict) -> bool:
    return all(str(row.get(k)) == v for k, v in key.items())


def _row_value(value: str):
    parsed = _json(value)
    if (
        isinstance(parsed, dict)
        and isinstance(parsed.get("row"), dict)
        and parsed.get("table") in TEXT
    ):
        return parsed
    return None


def build_plan(inventory: dict[str, list[dict]], target: dict) -> dict:
    from grove.writer.forget_sources import locator_key

    plan = {
        "version": 1,
        "hashes": [],
        "delete": [],
        "mark_values": [],
        "history_fields": [],
        "item_fields": [],
        "source_ids": [],
        "end_evidence": [],
        "clear_examples": [],
        "locators": [],
    }
    needles: set[str] = set()
    gone: set[str] = set()
    marks: set[str] = set()
    kind = target["type"]
    items = {r["id"]: r for r in inventory["items"]}
    values = {r["id"]: r for r in inventory["history_values"]}
    if kind in {"item", "field_value"} and "item_id" in target:
        item = items.get(target["item_id"])
        if item is None:
            raise InvalidRecord("not_found", "forget_item", "no such item")
        if kind == "item":
            gone.add(item["id"])
            for field in ("title", "summary", "body", "properties", "search_terms"):
                needles.update(leaves(item.get(field)))
            for row in inventory["links"]:
                if item["id"] in (row.get("from_item"), row.get("to_item")):
                    gone.add(row["id"])
            for row in inventory["aliases"]:
                if row.get("target_id") == item["id"]:
                    gone.add(row["id"])
                    needles.update(leaves(row.get("phrase")))
            for row in inventory["evidence"]:
                if row.get("target_id") in gone:
                    gone.add(row["id"])
        else:
            field = target["field"]
            if field not in item:
                raise InvalidRecord("not_found", "forget_field", "no such field value")
            needles.update(leaves(item[field]))
            plan["item_fields"].append({"id": item["id"], "field": field})
            for entry in inventory["change_entries"]:
                if entry.get("entity_id") != item["id"]:
                    continue
                if entry.get("field") == "row":
                    plan["history_fields"].append({"id": entry["new_value_id"], "field": field})
                elif entry.get("field") == field:
                    marks.update(entry[k] for k in ("old_value_id", "new_value_id") if entry.get(k))
    elif kind == "field_value":
        value_id = target["value_id"]
        if value_id not in values:
            raise InvalidRecord("not_found", "forget_value", "no such value")
        if _row_value(values[value_id]["value"]):
            raise ReplayRefused(None, "row_value", "a whole row requires a column selector")
        marks.add(value_id)
        needles.update(leaves(values[value_id]["value"]))
    elif kind == "named_value":
        needles.add(target["value"])
    elif kind == "source_span":
        source = next((r for r in inventory["sources"] if r["id"] == target["source_id"]), None)
        if source is None:
            raise InvalidRecord("not_found", "forget_source", "no such source")
        # The source's format may be plain text or a screened message array. Select one
        # message/line when a locator is supplied; never silently widen it to the source.
        locator = target.get("locator")
        if locator:
            messages = _json(source.get("content", ""))
            chosen = (
                [m for m in messages if isinstance(m, dict) and m.get("locator") == locator]
                if isinstance(messages, list)
                else []
            )
            if not chosen and locator.startswith("line:"):
                try:
                    index = int(locator[5:]) - 1
                    lines = source.get("content", "").splitlines()
                    chosen = [lines[index]] if 0 <= index < len(lines) else []
                except ValueError:
                    pass
            if not chosen:
                raise InvalidRecord("unresolvable_locator", "forget_span", "no such source span")
            for message in chosen:
                needles.update(leaves(message))
        else:
            plan["source_ids"].append(source["id"])
            needles.update(leaves(source.get("content")))
    for name, rows in inventory.items():
        for row in rows:
            if row.get("id") in gone or (
                name in {"beliefs", "map_positions"} and row.get("item_id") in gone
            ):
                plan["delete"].append(selector(name, row))
                for col in table(name).columns:
                    if col.type == "value_id":
                        marks.update(row[p] for p in col.props if row.get(p))
    for entry in inventory["change_entries"]:
        if entry.get("entity_id") in gone:
            marks.update(
                entry[k] for k in ("old_value_id", "new_value_id", "reason") if entry.get(k)
            )
    # A forgotten row retains its history entry, but no row value or old prose survives.
    plan["delete"] = [s for s in plan["delete"] if s["table"] != "change_entries"]
    normalized = {
        normalise_phrase(n) for n in needles if isinstance(n, str) and normalise_phrase(n)
    }
    hashes: set[str] = set()
    source_ids = set(plan["source_ids"])
    for name, rows in inventory.items():
        for row in rows:
            for field in TEXT.get(name, ()):
                value = row.get(field)
                rv = _row_value(value) if name == "history_values" else None
                candidates = (
                    (
                        leaf
                        for prop in TEXT.get(rv["table"], ())
                        for leaf in leaves(rv["row"].get(prop))
                    )
                    if rv
                    else leaves(value)
                )
                matched = {
                    digest(leaf)
                    for leaf in candidates
                    if any(n in normalise_phrase(leaf) for n in normalized)
                }
                hashes.update(matched)
                if matched and name == "sources":
                    source_ids.add(row["id"])
    # Source content is fully discarded when a match is found. This is explicit in the
    # receipt; blocking its stable locator prevents a repeat import from resurrecting it.
    plan["source_ids"] = sorted(source_ids)
    for row in inventory["sources"]:
        if row["id"] in source_ids:
            plan["locators"].append(locator_key(row["external_locator"]))
    ended = {
        r["id"]
        for r in inventory["evidence"]
        if r.get("source_id") in source_ids and r["id"] not in gone
    }
    plan["end_evidence"] = sorted(ended)
    affected_episodes = {
        r["id"] for r in inventory["episodes"] if set(r.get("source_ids", ())) & source_ids
    }
    for name in ("holds", "unresolved_mentions"):
        plan["delete"].extend(
            selector(name, r) for r in inventory[name] if r.get("episode_id") in affected_episodes
        )
    plan["clear_examples"] = sorted(ended | gone)
    plan["hashes"] = sorted(hashes)
    plan["mark_values"] = sorted(marks)
    return plan


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS or plan.get("version") != 1:
        raise ValueError("invalid forget plan")
    for key in PLAN_KEYS - {"version"}:
        if not isinstance(plan[key], list):
            raise ValueError("invalid forget plan list")
    for value in plan["hashes"]:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("invalid text digest")
    for selected in plan["delete"]:
        if (
            set(selected) != {"table", "key"}
            or selected["table"] not in WORLD_TABLES
            or selected["table"] in {"change_entries", "forget_ledger", "history_values"}
        ):
            raise ValueError("invalid deletion selector")
        key = selected["key"]
        allowed = [set(c.props) - {"world_id"} for c in table(selected["table"]).uniques]
        if (
            not isinstance(key, dict)
            or set(key) not in allowed
            or not all(isinstance(v, str) for v in key.values())
        ):
            raise ValueError("invalid deletion key")
    for name in ("mark_values", "source_ids", "end_evidence", "clear_examples", "locators"):
        if not all(isinstance(v, str) and v for v in plan[name]):
            raise ValueError("invalid forget identifier")
    for name in ("history_fields", "item_fields"):
        for selected in plan[name]:
            if (
                set(selected) != {"id", "field"}
                or not isinstance(selected["id"], str)
                or selected["field"] not in TEXT["items"]
            ):
                raise ValueError("invalid field selector")
    return plan


def transform(
    inventory: dict[str, list[dict]], plan: dict, at: datetime
) -> tuple[dict, dict[str, int]]:
    """Apply one validated plan without rewriting any ChangeEntry."""
    validate_plan(plan)
    result = copy.deepcopy(inventory)
    hashes = set(plan["hashes"])
    marks = set(plan["mark_values"])
    fields = {s["id"]: s["field"] for s in plan["history_fields"]}
    item_fields = {s["id"]: s["field"] for s in plan["item_fields"]}
    ended = set(plan["end_evidence"])
    deleted = defaultdict(list)
    for selected in plan["delete"]:
        deleted[selected["table"]].append(selected["key"])
    counts: dict[str, int] = defaultdict(int)
    for name, rows in result.items():
        kept = []
        for original in rows:
            if any(_matches(original, key) for key in deleted[name]):
                counts[name] += 1
                continue
            if name in {"change_entries", "forget_ledger", "forget_approvals"}:
                kept.append(original)
                continue
            row = _content(name, original, hashes)
            if name == "history_values":
                value_id = row["id"]
                rv = _row_value(original.get("value"))
                if value_id in marks:
                    row["value"] = FORGOTTEN_MARKER
                elif rv:
                    held = _content(rv["table"], rv["row"], hashes)
                    if value_id in fields:
                        prop = fields[value_id]
                        if prop == "properties":
                            held[prop] = "{}"
                            held.pop("property_values", None)
                        elif prop == "search_terms":
                            held[prop] = []
                        elif prop == "status":
                            held.pop(prop, None)
                        else:
                            held[prop] = FORGOTTEN_MARKER
                    if rv["table"] == "evidence" and held.get("id") in ended:
                        held.update(active=False, end_reason="forget", ended_at=at.isoformat())
                    if held.get("worked_example") in plan["clear_examples"]:
                        held.pop("worked_example", None)
                    row["value"] = json.dumps(
                        {**rv, "row": held}, sort_keys=True, ensure_ascii=False
                    )
                if row["value"] != original["value"]:
                    row.update(
                        forgotten_at=at, version=int(original.get("version", 1)) + 1, touched_at=at
                    )
            if name == "items" and row["id"] in item_fields:
                prop = item_fields[row["id"]]
                if prop == "properties":
                    row[prop] = "{}"
                    row.pop("property_values", None)
                elif prop == "search_terms":
                    row.pop(prop, None)
                elif prop == "status":
                    row.pop(prop, None)
                else:
                    row[prop] = FORGOTTEN_MARKER
            if name == "sources" and row["id"] in plan["source_ids"]:
                from grove.writer.forget_sources import locator_key

                row.update(
                    content="[]",
                    forget_state="forgotten",
                    external_locator=locator_key(original["external_locator"]),
                )
                row.pop("content_pointer", None)
            if name == "evidence" and row["id"] in ended:
                row.update(active=False, end_reason="forget", ended_at=at)
            if row.get("worked_example") in plan["clear_examples"]:
                row.pop("worked_example", None)
            if row != original:
                counts[name] += 1
            kept.append(row)
        result[name] = kept
    return result, dict(counts)
