"""Deterministic D14 erasure plans with no retained search text.

Plans contain row identities, complete-leaf hashes and opaque source selectors. Source
messages are addressed by their original keys, never their current array positions.
"""

import copy
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from grove.store.catalogue import TABLES, table
from grove.store.normalise import normalise_phrase
from grove.store.replay import FORGOTTEN_MARKER, ReplayRefused
from grove.writer.errors import InvalidRecord
from grove.writer.forget_sources import locator_key, message_locator_key
from grove.writer.source_types import RANK, claim_fields

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
    "source_spans",
    "evidence_values",
    "end_evidence",
    "clear_examples",
    "locators",
    "queue_beliefs",
}
_HASH = re.compile(r"[0-9a-f]{64}")
_LOCATOR = re.compile(r"sha256:[0-9a-f]{64}")
_RANGE = re.compile(r"(line|char):(\d+)(?:-(\d+))?")
_PROTECTED = {"change_entries", "forget_ledger", "forget_approvals"}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _json(text):
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def leaves(value: Any, depth: int = 0, *, include_keys=False):
    """Only values are search needles: dictionary keys describe the storage format."""
    if depth > 40:
        raise InvalidRecord("invalid_arguments", "text_depth", "stored text is too deeply nested")
    if isinstance(value, str):
        parsed = _json(value)
        if isinstance(parsed, dict | list | str):
            yield from leaves(parsed, depth + 1, include_keys=include_keys)
        else:
            yield value
    elif isinstance(value, dict):
        for key, part in value.items():
            if include_keys:
                yield key
            yield from leaves(part, depth + 1, include_keys=include_keys)
    elif isinstance(value, list):
        for part in value:
            yield from leaves(part, depth + 1, include_keys=include_keys)


def _messages(content):
    parsed = _json(content)
    if isinstance(parsed, list) and all(
        isinstance(m, dict) and isinstance(m.get("key"), str) and isinstance(m.get("text"), str)
        for m in parsed
    ):
        return parsed
    return None


def _row_value(value):
    parsed = _json(value)
    if (
        isinstance(parsed, dict)
        and isinstance(parsed.get("row"), dict)
        and parsed.get("table") in WORLD_TABLES
    ):
        return parsed
    return None


def _text_leaves(name, row, *, include_keys=False):
    if name == "sources":
        messages = _messages(row.get("content"))
        if messages is not None:
            for message in messages:
                for field in ("sender", "text"):
                    if isinstance(message.get(field), str):
                        yield message[field]
        else:
            yield from leaves(row.get("content"))
        return
    if name == "history_values" and (nested := _row_value(row.get("value"))):
        yield from _text_leaves(nested["table"], nested["row"], include_keys=include_keys)
        return
    for field in TEXT[name]:
        if field == "status_vocabulary":
            for part in _json(row.get(field)) or []:
                if isinstance(part, dict) and isinstance(part.get("status"), str):
                    yield part["status"]
        else:
            yield from leaves(row.get(field), include_keys=include_keys)


def _token(value):
    return "forgotten-" + digest(value)


def _redact(value, hashes, depth=0, *, vocabulary=False, preserve_roles=False):
    if depth > 40:
        raise InvalidRecord("invalid_arguments", "text_depth", "stored text is too deeply nested")
    if isinstance(value, str):
        parsed = _json(value)
        if isinstance(parsed, dict | list | str):
            changed = _redact(
                parsed, hashes, depth + 1, vocabulary=vocabulary, preserve_roles=preserve_roles
            )
            return (
                value
                if changed == parsed
                else json.dumps(changed, sort_keys=True, ensure_ascii=True)
            )
        if digest(value) in hashes:
            return _token(value) if vocabulary else FORGOTTEN_MARKER
    elif isinstance(value, dict):
        return {
            (_token(key) if not preserve_roles and digest(key) in hashes else key): (
                part
                if preserve_roles and key == "role"
                else _redact(
                    part, hashes, depth + 1, vocabulary=vocabulary, preserve_roles=preserve_roles
                )
            )
            for key, part in value.items()
        }
    elif isinstance(value, list):
        return [
            _redact(part, hashes, depth + 1, vocabulary=vocabulary, preserve_roles=preserve_roles)
            for part in value
        ]
    return value


def _content(name, row, hashes):
    result = dict(row)
    for field in TEXT[name]:
        if field in row and not (name == "sources" and field == "content"):
            result[field] = _redact(
                row[field],
                hashes,
                vocabulary=field in {"status", "status_vocabulary", "name"},
                preserve_roles=field == "status_vocabulary",
            )
    # Existing metadata contracts permit strings. A matching copy in a locator or
    # configuration must not survive merely because its declared type is not prose.
    for column in WORLD_TABLES[name].columns:
        if column.type in {"locator", "code", "json_ids", "ids"}:
            for field in column.props:
                if field in row:
                    result[field] = _redact(row[field], hashes, vocabulary=True)
    if name == "sources" and result.get("external_locator") != row.get("external_locator"):
        result["external_locator"] = _source_key(row)
        result["forget_state"] = "span_forgotten"
    return result


def selector(name, row):
    keys = ("id",) if "id" in row else table(name).uniques[0].props
    return {"table": name, "key": {k: str(row[k]) for k in keys if k != "world_id"}}


def _matches(row, key):
    return all(str(row.get(k)) == value for k, value in key.items())


def _source_key(source):
    # The stored hash is the original locator after an earlier whole-source erasure.
    value = source.get("external_locator", "")
    return (
        value
        if source.get("forget_state") in {"forgotten", "span_forgotten"}
        and _LOCATOR.fullmatch(value)
        else locator_key(value)
    )


def _span(source, mode, **extra):
    return {"source_id": source["id"], "source_key": _source_key(source), "mode": mode, **extra}


def _resolve_span(source, locator, evidence):
    if locator in {None, "whole"}:
        return _span(source, "whole")
    messages = _messages(source.get("content"))
    if messages is not None:
        keys = {
            m["key"]
            for m in messages
            if locator in {m["key"], "key:" + m["key"], "message:" + m["key"]}
        }
        keys.update(
            e["message_key"]
            for e in evidence
            if e.get("source_id") == source["id"]
            and e.get("locator") == locator
            and e.get("message_key") in {m["key"] for m in messages}
        )
        found = _RANGE.fullmatch(locator)
        if not keys and found and found[1] == "line":
            first, last = int(found[2]), int(found[3] or found[2])
            if 1 <= first <= last <= len(messages):
                keys = {m["key"] for m in messages[first - 1 : last]}
        if keys:
            return _span(source, "messages", message_keys=sorted(digest(k) for k in keys))
    else:
        content = source.get("content") or ""
        found = _RANGE.fullmatch(locator)
        if found:
            mode, first, last = found[1], int(found[2]), int(found[3] or found[2])
            if (mode == "line" and 1 <= first <= last <= len(content.splitlines())) or (
                mode == "char" and found[3] and 0 <= first < last <= len(content)
            ):
                return _span(source, mode, start=first, end=last)
    raise InvalidRecord("unresolvable_locator", "forget_span", "no such source span")


def _span_text(source, span):
    if span["mode"] == "whole":
        yield from _text_leaves("sources", source)
    elif span["mode"] == "messages":
        for message in _messages(source.get("content")) or []:
            if digest(message["key"]) in span["message_keys"]:
                for field in ("text", "sender"):
                    if isinstance(message.get(field), str):
                        yield message[field]
    elif span["mode"] == "line":
        yield from (source.get("content") or "").splitlines()[span["start"] - 1 : span["end"]]
    else:
        yield (source.get("content") or "")[span["start"] : span["end"]]


def _overlap(locator, span, source, *, message_key=None):
    if span["mode"] == "whole" or locator == "whole":
        return True
    if span["mode"] == "messages":
        if message_key and digest(message_key) in span["message_keys"]:
            return True
        try:
            other = _resolve_span(source, locator, [])
        except (InvalidRecord, TypeError):
            return False
        return bool(set(other.get("message_keys", ())) & set(span["message_keys"]))
    content = source.get("content") or ""
    found = _RANGE.fullmatch(locator or "")

    def bounds(mode, first, last):
        if mode == "char":
            return first, last
        lines = content.splitlines(keepends=True)
        return sum(map(len, lines[: first - 1])), sum(map(len, lines[:last]))

    start, end = bounds(span["mode"], span["start"], span["end"])
    if found:
        other_start, other_end = bounds(found[1], int(found[2]), int(found[3] or found[2]))
        return max(start, other_start) < min(end, other_end)
    if isinstance(locator, str) and locator.startswith("key:") and locator[4:]:
        token = locator[4:]
        position = content.find(token)
        while position >= 0:
            if max(start, position) < min(end, position + len(token)):
                return True
            position = content.find(token, position + 1)
    return False


def build_plan(inventory: dict[str, list[dict]], target: dict) -> dict:
    plan = {key: [] for key in PLAN_KEYS}
    plan["version"] = 1
    rows = {name: inventory.get(name, []) for name in WORLD_TABLES}
    needles, gone, marks = set(), set(), set()
    items = {r["id"]: r for r in rows["items"]}
    values = {r["id"]: r for r in rows["history_values"]}
    sources = {r["id"]: r for r in rows["sources"]}
    kind = target.get("type")
    if kind in {"item", "field_value"} and "item_id" in target:
        item = items.get(target["item_id"])
        if item is None:
            raise InvalidRecord("not_found", "forget_item", "no such item")
        if kind == "item":
            gone.add(item["id"])
            needles.update(_text_leaves("items", item))
            gone.update(
                r["id"]
                for r in rows["links"]
                if item["id"] in (r.get("from_item"), r.get("to_item"))
            )
            for row in rows["aliases"]:
                if row.get("target_id") == item["id"]:
                    gone.add(row["id"])
                    needles.update(_text_leaves("aliases", row))
        else:
            field = target.get("field")
            if field not in TEXT["items"]:
                raise InvalidRecord("invalid_arguments", "forget_field", "field is not a text home")
            if field not in item:
                raise InvalidRecord("not_found", "forget_field", "no such field value")
            needles.update(leaves(item[field]))
            plan["item_fields"].append({"id": item["id"], "field": field})
            for entry in rows["change_entries"]:
                if entry.get("entity_id") != item["id"]:
                    continue
                if entry.get("field") == "row":
                    for prop in ("old_value_id", "new_value_id"):
                        if value := values.get(entry.get(prop)):
                            nested = _row_value(value["value"])
                            if nested:
                                needles.update(leaves(nested["row"].get(field)))
                                plan["history_fields"].append({"id": value["id"], "field": field})
                elif entry.get("field") == field:
                    marks.update(entry[p] for p in ("old_value_id", "new_value_id") if entry.get(p))
    elif kind == "field_value":
        value = values.get(target.get("value_id"))
        if value is None:
            raise InvalidRecord("not_found", "forget_value", "no such value")
        if _row_value(value["value"]):
            raise ReplayRefused(None, "row_value", "a whole row requires a column selector")
        marks.add(value["id"])
    elif kind == "named_value":
        if not isinstance(target.get("value"), str) or not normalise_phrase(target["value"]):
            raise InvalidRecord("invalid_arguments", "forget_value", "a nonblank value is required")
        needles.add(target["value"])
    elif kind == "source_span":
        source = sources.get(target.get("source_id"))
        if source is None:
            raise InvalidRecord("not_found", "forget_source", "no such source")
        span = _resolve_span(source, target.get("locator"), rows["evidence"])
        plan["source_spans"].append(span)
        needles.update(_span_text(source, span))
    else:
        raise InvalidRecord("invalid_arguments", "forget_target", "unknown forget target")

    gone.update(r["id"] for r in rows["evidence"] if r.get("target_id") in gone)
    # Reach old row values and the value-id properties of deleted rows to a fixed point.
    for name, current in rows.items():
        if name in _PROTECTED or name == "history_values":
            continue
        for row in current:
            if row.get("id") in gone or (
                name in {"beliefs", "map_positions"} and row.get("item_id") in gone
            ):
                plan["delete"].append(selector(name, row))
                for column in WORLD_TABLES[name].columns:
                    if column.type == "value_id":
                        marks.update(row[p] for p in column.props if row.get(p))
    for entry in rows["change_entries"]:
        if entry.get("entity_id") in gone:
            marks.update(
                entry[p] for p in ("old_value_id", "new_value_id", "reason") if entry.get(p)
            )
    pending = list(marks)
    visited = set()
    while pending:
        value_id = pending.pop()
        if value_id in visited or value_id not in values:
            continue
        visited.add(value_id)
        value = values[value_id]
        needles.update(_text_leaves("history_values", value))
        nested = _row_value(value["value"])
        if nested:
            for column in WORLD_TABLES[nested["table"]].columns:
                if column.type == "value_id":
                    for prop in column.props:
                        ref = nested["row"].get(prop)
                        if ref and ref not in marks:
                            marks.add(ref)
                            pending.append(ref)
    normalized = {
        normalise_phrase(n) for n in needles if n != FORGOTTEN_MARKER and normalise_phrase(n)
    }

    def matches(text):
        return any(needle in normalise_phrase(text) for needle in normalized)

    # Immutable identities cannot be rewritten without changing the append-only
    # history. Fail before issuing a plan if legacy data put prose in such a key.
    def check_identity(name, row):
        keys = WORLD_TABLES[name].uniques[0].props
        for prop in keys:
            value = row.get(prop)
            if prop == "world_id" or not isinstance(value, str):
                continue
            try:
                UUID(value)
                opaque = True
            except ValueError:
                opaque = False
            if not opaque and matches(value):
                raise InvalidRecord(
                    "unavailable",
                    "forget_identity",
                    "a stored identity requires repair before erasure",
                )

    for name, current in rows.items():
        for row in current:
            check_identity(name, row)
            if name == "history_values" and (nested := _row_value(row.get("value"))):
                check_identity(nested["table"], nested["row"])
    hashes = set()
    for name, current in rows.items():
        if name in _PROTECTED:
            continue
        for row in current:
            hashes.update(
                digest(text) for text in _text_leaves(name, row, include_keys=True) if matches(text)
            )
            # Scan metadata copies too, but never let them introduce new search needles.
            metadata_name, metadata_row = name, row
            if name == "history_values" and (nested := _row_value(row.get("value"))):
                metadata_name, metadata_row = nested["table"], nested["row"]
            for column in WORLD_TABLES[metadata_name].columns:
                if column.type in {"locator", "code", "json_ids", "ids"}:
                    for prop in column.props:
                        hashes.update(
                            digest(text)
                            for text in leaves(metadata_row.get(prop), include_keys=True)
                            if matches(text)
                        )
    for source in sources.values():
        if kind == "source_span" and source["id"] == target["source_id"]:
            continue
        messages = _messages(source.get("content"))
        if messages is not None:
            selected = [
                digest(m["key"])
                for m in messages
                if any(matches(m[f]) for f in ("text", "sender") if isinstance(m.get(f), str))
            ]
            if selected:
                plan["source_spans"].append(
                    _span(source, "messages", message_keys=sorted(selected))
                )
        else:
            for index, line in enumerate((source.get("content") or "").splitlines(), 1):
                if matches(line):
                    plan["source_spans"].append(_span(source, "line", start=index, end=index))
    # Sort/deduplicate selectors; no raw source key or input locator enters the ledger.
    spans = {json.dumps(s, sort_keys=True): s for s in plan["source_spans"]}
    plan["source_spans"] = [spans[k] for k in sorted(spans)]
    locators = {
        _source_key(source)
        for source in sources.values()
        if isinstance(source.get("external_locator"), str) and matches(source["external_locator"])
    }
    for span in plan["source_spans"]:
        source = sources[span["source_id"]]
        if span["mode"] == "whole":
            plan["source_ids"].append(source["id"])
        if span["mode"] == "messages":
            locators.update(
                message_locator_key(source.get("external_locator", ""), m["key"])
                for m in _messages(source.get("content")) or []
                if digest(m["key"]) in span["message_keys"]
            )
        else:
            locators.add(span["source_key"])
    ended = {
        e["id"]
        for e in rows["evidence"]
        for span in plan["source_spans"]
        if e.get("source_id") == span["source_id"]
        and _overlap(
            e.get("locator"), span, sources[span["source_id"]], message_key=e.get("message_key")
        )
    }
    while True:
        wider = ended | {
            e["id"] for e in rows["evidence"] if set(e.get("premises") or []) & (ended | gone)
        }
        if wider == ended:
            break
        ended = wider
    episodes = {r["id"]: r for r in rows["episodes"]}
    for name in ("holds", "unresolved_mentions"):
        for row in rows[name]:
            episode = episodes.get(row.get("episode_id"), {})
            affected = any(
                span["source_id"] in (episode.get("source_ids") or [])
                and _overlap(row.get("locator"), span, sources[span["source_id"]])
                for span in plan["source_spans"]
            )
            if affected or row.get("resolved_to") in gone:
                plan["delete"].append(selector(name, row))
                gone.add(row["id"])
                marks.update(
                    row[p]
                    for col in WORLD_TABLES[name].columns
                    if col.type == "value_id"
                    for p in col.props
                    if row.get(p)
                )
    for entry in rows["change_entries"]:
        if entry.get("entity_id") in ended and entry.get("field") in {
            "active",
            "end_reason",
            "ended_at",
        }:
            plan["evidence_values"].extend(
                {"id": entry[p], "field": entry["field"]}
                for p in ("old_value_id", "new_value_id")
                if entry.get(p)
            )
    beliefs = {b.get("item_id", b.get("id")) for b in rows["beliefs"]}
    affected = {
        e.get("target_id")
        for e in rows["evidence"]
        if e["id"] in ended | gone and e.get("polarity") == "supports"
    }
    remaining = {
        e.get("target_id")
        for e in rows["evidence"]
        if e["id"] not in ended | gone and e.get("active") and e.get("polarity") == "supports"
    }
    plan.update(
        hashes=sorted(hashes),
        mark_values=sorted(marks),
        end_evidence=sorted(ended - gone),
        clear_examples=sorted(ended | gone),
        locators=sorted(locators),
        queue_beliefs=sorted((affected & beliefs) - remaining - gone),
        source_ids=sorted(set(plan["source_ids"])),
    )
    validate_plan(plan)
    return plan


def validate_plan(plan):
    if (
        not isinstance(plan, dict)
        or set(plan) != PLAN_KEYS
        or type(plan["version"]) is not int
        or plan["version"] != 1
    ):
        raise ValueError("invalid forget plan shape")
    if any(not isinstance(plan[k], list) for k in PLAN_KEYS - {"version"}):
        raise ValueError("invalid forget plan list")

    def identifiers(values):
        return all(
            isinstance(v, str) and 0 < len(v) <= 1024 and not any(ord(c) < 32 for c in v)
            for v in values
        )

    if any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in plan["hashes"]):
        raise ValueError("invalid forget hash")
    if any(not isinstance(v, str) or not _LOCATOR.fullmatch(v) for v in plan["locators"]):
        raise ValueError("invalid forget locator")
    for key in ("mark_values", "source_ids", "end_evidence", "clear_examples", "queue_beliefs"):
        if not identifiers(plan[key]):
            raise ValueError("invalid forget identity")
    for part in plan["delete"]:
        if (
            not isinstance(part, dict)
            or set(part) != {"table", "key"}
            or part["table"] not in WORLD_TABLES
            or part["table"] in _PROTECTED | {"history_values"}
        ):
            raise ValueError("invalid forget deletion")
        keys = part["key"]
        if (
            not isinstance(keys, dict)
            or not keys
            or not identifiers(keys.values())
            or set(keys)
            not in [
                {p for p in unique.props if p != "world_id"}
                for unique in WORLD_TABLES[part["table"]].uniques
            ]
        ):
            raise ValueError("invalid forget row key")
    for key in ("history_fields", "item_fields"):
        for part in plan[key]:
            if (
                not isinstance(part, dict)
                or set(part) != {"id", "field"}
                or not identifiers([part["id"]])
                or part["field"] not in TEXT["items"]
            ):
                raise ValueError("invalid forget field")
    for part in plan["evidence_values"]:
        if (
            not isinstance(part, dict)
            or set(part) != {"id", "field"}
            or not identifiers([part["id"]])
            or part["field"] not in {"active", "end_reason", "ended_at"}
        ):
            raise ValueError("invalid evidence value selector")
    for span in plan["source_spans"]:
        if not isinstance(span, dict) or span.get("mode") not in {
            "whole",
            "messages",
            "line",
            "char",
        }:
            raise ValueError("invalid source span")
        mode = span["mode"]
        expected = {"source_id", "source_key", "mode"} | (
            {"message_keys"}
            if mode == "messages"
            else {"start", "end"}
            if mode in {"line", "char"}
            else set()
        )
        if (
            set(span) != expected
            or not identifiers([span["source_id"]])
            or not isinstance(span["source_key"], str)
            or not _LOCATOR.fullmatch(span["source_key"])
        ):
            raise ValueError("invalid source selector")
        if mode == "messages" and (
            not isinstance(span["message_keys"], list)
            or not span["message_keys"]
            or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in span["message_keys"])
        ):
            raise ValueError("invalid source message selector")
        if mode in {"line", "char"} and (
            type(span["start"]) is not int
            or type(span["end"]) is not int
            or span["start"] < (1 if mode == "line" else 0)
            or span["end"] < span["start"] + (1 if mode == "char" else 0)
        ):
            raise ValueError("invalid source range")
    if set(plan["source_ids"]) != {
        s["source_id"] for s in plan["source_spans"] if s["mode"] == "whole"
    }:
        raise ValueError("inconsistent whole-source selectors")


def _blank_field(row, field):
    if field in {"search_terms", "property_values", "status"}:
        row.pop(field, None)
        if field == "status":
            row.pop("status_role", None)
    else:
        row[field] = "{}" if field == "properties" else FORGOTTEN_MARKER
    if field == "properties":
        row.pop("property_values", None)


def _source_content(source, spans):
    result = dict(source)
    for span in spans:
        mode = span["mode"]
        if mode == "whole":
            result.update(
                content="[]", forget_state="forgotten", external_locator=span["source_key"]
            )
            result.pop("content_pointer", None)
            return result
        if mode == "messages":
            messages = _messages(result.get("content"))
            if messages is not None:
                kept = [m for m in messages if digest(m["key"]) not in span["message_keys"]]
                if kept != messages:
                    result["content"] = json.dumps(kept, ensure_ascii=True, sort_keys=True)
        elif mode == "line":
            lines = (result.get("content") or "").splitlines(keepends=True)
            for index in range(span["start"] - 1, min(span["end"], len(lines))):
                ending = (
                    "\r\n"
                    if lines[index].endswith("\r\n")
                    else "\n"
                    if lines[index].endswith("\n")
                    else ""
                )
                lines[index] = FORGOTTEN_MARKER + ending
            result["content"] = "".join(lines)
        else:
            content = result.get("content") or ""
            result["content"] = (
                content[: span["start"]]
                + " " * max(0, min(len(content), span["end"]) - span["start"])
                + content[span["end"] :]
            )
    if result != source:
        result["forget_state"] = "span_forgotten"
    return result


def transform(inventory: dict, plan: dict, at: datetime) -> tuple[dict, dict]:
    validate_plan(plan)
    result, counts = copy.deepcopy(inventory), defaultdict(int)
    hashes, marks = set(plan["hashes"]), set(plan["mark_values"])
    field_sets = {}
    for name in ("history_fields", "item_fields"):
        field_sets[name] = defaultdict(set)
        for field in plan[name]:
            field_sets[name][field["id"]].add(field["field"])
    deletions, spans = defaultdict(list), defaultdict(list)
    for part in plan["delete"]:
        deletions[part["table"]].append(part["key"])
    for span in plan["source_spans"]:
        spans[span["source_id"]].append(span)
    ended, examples = set(plan["end_evidence"]), set(plan["clear_examples"])
    evidence_values = {part["id"]: part["field"] for part in plan["evidence_values"]}
    value_fields = {
        entry[prop]: entry.get("field")
        for entry in inventory.get("change_entries", [])
        for prop in ("old_value_id", "new_value_id")
        if entry.get(prop)
    }

    def change_row(name, original, fields=()):
        row = _content(name, original, hashes)
        if name == "items":
            for field in fields:
                _blank_field(row, field)
        if name == "sources":
            row = _source_content(row, spans[row["id"]])
        if name == "evidence" and row.get("id") in ended:
            row.update(
                active=False,
                end_reason="forget",
                ended_at=at.isoformat() if isinstance(original.get("ended_at"), str) else at,
            )
        if row.get("worked_example") in examples:
            row.pop("worked_example", None)
        return row

    for name, current in result.items():
        kept = []
        for original in current:
            if any(_matches(original, key) for key in deletions[name]):
                counts[name] += 1
                continue
            if name in _PROTECTED:
                kept.append(original)
                continue
            if name == "history_values":
                row = dict(original)
                nested = _row_value(row.get("value"))
                if row["id"] in marks:
                    row["value"] = FORGOTTEN_MARKER
                elif row["id"] in evidence_values:
                    field = evidence_values[row["id"]]
                    row["value"] = json.dumps(
                        {"active": False, "end_reason": "forget", "ended_at": at.isoformat()}[field]
                    )
                elif nested:
                    changed = change_row(
                        nested["table"], nested["row"], field_sets["history_fields"][row["id"]]
                    )
                    if changed != nested["row"]:
                        nested["row"] = changed
                        row["value"] = json.dumps(
                            nested,
                            ensure_ascii=True,
                            sort_keys=True,
                            default=lambda v: v.isoformat(),
                        )
                elif value_fields.get(row["id"]) in {"status", "status_vocabulary", "name"}:
                    row["value"] = _redact(
                        row["value"],
                        hashes,
                        vocabulary=True,
                        preserve_roles=value_fields.get(row["id"]) == "status_vocabulary",
                    )
                else:
                    row = _content(name, row, hashes)
                if row != original:
                    row.update(
                        forgotten_at=at, version=int(row.get("version", 0)) + 1, touched_at=at
                    )
            else:
                row = change_row(
                    name, original, field_sets["item_fields"].get(original.get("id"), ())
                )
            if row != original:
                counts[name] += 1
            kept.append(row)
        result[name] = kept
    # Erasure ends evidence immediately. Recompute the same derived claim fields as
    # D3 replay so a later rebuild cannot change the sanitized world's projection.
    supporting = defaultdict(list)
    for evidence in result["evidence"]:
        if (
            evidence.get("active")
            and evidence.get("polarity") == "supports"
            and evidence.get("source_type") in RANK
        ):
            supporting[evidence.get("target_id")].append(evidence)
    for name, fields in (
        ("items", ("derived_source_type", "derivation", "speaker", "modality")),
        ("links", ("derived_source_type", "derivation")),
    ):
        original_rows = {row["id"]: row for row in inventory[name]}
        for row in result[name]:
            derived = claim_fields(supporting.get(row["id"], []))
            changed = any(row.get(field) != derived[field] for field in fields)
            if changed and row == original_rows.get(row["id"]):
                counts[name] += 1
            for field in fields:
                if derived[field] is None:
                    row.pop(field, None)
                else:
                    row[field] = derived[field]
    queue = result.setdefault("support_changed", [])
    world = next(
        (r["world_id"] for current in result.values() for r in current if r.get("world_id")), None
    )
    last = max((r.get("sequence", 0) for r in result.get("change_entries", [])), default=0)
    for belief_id in plan["queue_beliefs"]:
        existing = next((r for r in queue if r.get("belief_id") == belief_id), None)
        desired = {
            "world_id": world,
            "belief_id": belief_id,
            "cause_change_id": last,
            "queued_at": at,
        }
        if existing is None:
            queue.append(desired)
            counts["support_changed"] += 1
        elif existing != desired:
            existing.clear()
            existing.update(desired)
            counts["support_changed"] += 1
    return result, dict(counts)
