"""Receipts (D2): built after the commit from the change entries of the batch, read back in a fresh
read transaction. A stored receipt holds ids and codes only; titles, meaning names and values are
read fresh when it is rendered, so no copy of free text sits in it.

Grammar: `+` item created, `>` link created, `*` field changed, `x` ended, `^` reopened,
`=` evidence attached (or a known link restated), `-` evidence ended, `o` ontology (a word minted,
an alias made from a mint, a word promoted).
"""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from neo4j import Driver

from grove.store.primitives import KIND_BELIEF, PRIMITIVE_KIND_BELIEF, PRIMITIVE_MEANINGS
from grove.store.query import WorldQuery
from grove.writer.errors import DeclinedReceipt

SHORT_ID_LENGTH = 8
# Item fields that make a `*` line. Bookkeeping fields (version, touched_at, provenance) make none.
VISIBLE_FIELDS = (
    "status",
    "title",
    "summary",
    "body",
    "properties",
    "search_terms",
    "became_true",
    "recheck_by",
)
_ITEM_END_WORDS = {
    "ended_in_the_world": "ended in the world",
    "refuted": "refuted",
    "superseded": "superseded",
    "retired": "retired",
    "spent": "spent",
}
_LINK_END_WORDS = {**_ITEM_END_WORDS, "superseded": "superseded by new link"}


def short_id(value: str) -> str:
    """The last 8 hex digits: the random part of a UUIDv7."""
    return value.replace("-", "")[-SHORT_ID_LENGTH:]


@dataclass(frozen=True)
class ReceiptLine:
    symbol: str
    entity_type: str
    entity_id: str
    field: str | None = None
    value_id: str | None = None
    end_class: str | None = None
    end_reason: str | None = None
    evidence_id: str | None = None
    polarity: str | None = None
    source_type: str | None = None
    history_only: bool = False
    # The world time of a history-only value (ISO text): the time the as-of reads find it at.
    world_time: str | None = None
    already_known: bool = False
    stored_ended: bool = False

    def to_json(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, False)}


@dataclass(frozen=True)
class Receipt:
    state: str
    # None only for a request a pass finished with nothing to write: no commit happened.
    commit_id: str | None
    change_set_id: str | None
    request_id: str | None
    lines: tuple[ReceiptLine, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "commit_id": self.commit_id,
            "change_set_id": self.change_set_id,
            "request_id": self.request_id,
            "lines": [line.to_json() for line in self.lines],
        }

    @classmethod
    def from_json(cls, stored: dict[str, Any]) -> "Receipt":
        return cls(
            state=stored["state"],
            commit_id=stored["commit_id"],
            change_set_id=stored["change_set_id"],
            request_id=stored.get("request_id"),
            lines=tuple(ReceiptLine(**line) for line in stored.get("lines", [])),
        )


def _values(query: WorldQuery, value_ids: list[str]) -> dict[str, Any]:
    if not value_ids:
        return {}
    rows = query.read(
        "MATCH (v:HistoryValue) WHERE v.id IN $ids RETURN v.id AS id, v.value AS value",
        ids=value_ids,
    )
    decoded = {}
    for row in rows:
        try:
            decoded[row["id"]] = json.loads(row["value"])
        except (TypeError, ValueError):
            decoded[row["id"]] = row["value"]
    return decoded


def read_receipt(
    driver: Driver,
    world_id: str,
    *,
    commit_id: str,
    change_set_id: str,
    request_id: str | None,
) -> Receipt:
    """The receipt of one committed change set, from a fresh read of its change entries."""
    query = WorldQuery(driver, world_id)
    entries = [
        row["e"]
        for row in query.read(
            "MATCH (e:ChangeEntry {change_set_id: $id}) RETURN e ORDER BY e.sequence",
            id=change_set_id,
        )
    ]
    touched: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        if entry["entity_type"] != "evidence":
            touched[entry["entity_id"]].add(entry["field"])
    coded = [
        entry["new_value_id"]
        for entry in entries
        if entry.get("new_value_id")
        and (
            entry["field"] in ("end_class", "end_reason")
            or (entry["entity_type"] in ("link", "registry_row") and entry["field"] == "row")
        )
    ]
    values = _values(query, coded)
    evidence_ids = [
        entry["entity_id"]
        for entry in entries
        if entry["entity_type"] == "evidence" and entry["field"] == "row"
    ]
    evidence = {}
    if evidence_ids:
        evidence = {
            row["id"]: row
            for row in query.read(
                "MATCH (e:Evidence) WHERE e.id IN $ids RETURN e.id AS id, "
                "e.target_type AS target_type, e.target_id AS target_id, "
                "e.polarity AS polarity, e.source_type AS source_type",
                ids=evidence_ids,
            )
        }

    lines: list[ReceiptLine] = []
    for entry in entries:
        line = _line(entry, values, evidence, touched)
        if line is not None:
            lines.append(line)
    return Receipt("saved", commit_id, change_set_id, request_id, tuple(lines))


def _line(
    entry: dict[str, Any],
    values: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
    touched: dict[str, set[str]],
) -> ReceiptLine | None:
    entity_type, entity_id, name = entry["entity_type"], entry["entity_id"], entry["field"]
    value_id = entry.get("new_value_id")
    if entity_type in ("item", "belief"):
        if name == "row":
            return ReceiptLine("+", "item", entity_id)
        if name == "reopened":
            return ReceiptLine("^", "item", entity_id)
        if name == "end_class" and values.get(value_id):
            return ReceiptLine("x", "item", entity_id, end_class=values[value_id])
        if name in VISIBLE_FIELDS:
            history_only = bool(entry.get("history_only"))
            when = entry.get("world_time")
            return ReceiptLine(
                "*",
                "item",
                entity_id,
                field=name,
                value_id=value_id,
                history_only=history_only,
                world_time=when.isoformat() if history_only and when is not None else None,
            )
        return None
    if entity_type == "link":
        if name == "row":
            row = (values.get(value_id) or {}).get("row", {})
            return ReceiptLine(
                ">",
                "link",
                entity_id,
                end_class=row.get("end_class"),
                stored_ended=bool(row.get("end_class")),
            )
        if name == "end_class" and values.get(value_id):
            return ReceiptLine("x", "link", entity_id, end_class=values[value_id])
        if name == "restated":
            return ReceiptLine("=", "link", entity_id, already_known=True)
        return None
    if entity_type == "evidence":
        if name == "row":
            row = evidence.get(entity_id)
            if row is None or row["target_id"] in touched:
                return None
            return ReceiptLine(
                "=",
                "link" if row["target_type"] == "link" else row["target_type"],
                row["target_id"],
                evidence_id=entity_id,
                polarity=row["polarity"],
                source_type=row["source_type"],
            )
        if name == "end_reason":
            return ReceiptLine("-", "evidence", entity_id, end_reason=values.get(value_id))
    if entity_type == "registry_row" and name in ("row", "status"):
        # O10: a mint or an alias (row), a promotion (status). The table rides in field.
        if name == "row":
            stored = values.get(value_id) or {}
            table = stored.get("table") if isinstance(stored, dict) else None
            status = (stored.get("row") or {}).get("status") if isinstance(stored, dict) else None
            return ReceiptLine("o", "registry_row", entity_id, field=table, end_class=status)
        return ReceiptLine("o", "registry_row", entity_id, field="status", value_id=value_id)
    return None


# Rendering ------------------------------------------------------------------------------------


def _shown(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(part) for part in value)
    if isinstance(value, dict) and "precision" in value:
        if value["precision"] == "unknown":
            return "unknown"
        bounds = [value.get("earliest"), value.get("latest")]
        shown = " to ".join(dict.fromkeys(bound for bound in bounds if bound))
        return f"{shown} ({value['precision']})"
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _month_day(value: str) -> str:
    """D1's receipt form of a world time: `03-03`, in UTC."""
    return datetime.fromisoformat(value).astimezone(UTC).strftime("%m-%d")


def _lookups(query: WorldQuery, receipt: Receipt) -> dict[str, dict[str, Any]]:
    item_ids = {line.entity_id for line in receipt.lines if line.entity_type in ("item", "belief")}
    link_ids = {line.entity_id for line in receipt.lines if line.entity_type == "link"}
    links = {}
    if link_ids:
        links = {
            row["id"]: row
            for row in query.read(
                "MATCH (:Item)-[l:LINK]->(:Item) WHERE l.id IN $ids RETURN l.id AS id, "
                "l.from_item AS from_item, l.to_item AS to_item, l.meaning AS meaning",
                ids=sorted(link_ids),
            )
        }
    items = {}
    if item_ids:
        items = {
            row["id"]: row
            for row in query.read(
                "MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id, i.title AS title, "
                "i.kind AS kind",
                ids=sorted(item_ids),
            )
        }
    kinds = {KIND_BELIEF: str(PRIMITIVE_KIND_BELIEF["name"])}
    kinds |= {
        row["id"]: row["name"]
        for row in query.read("MATCH (k:Kind) RETURN k.id AS id, k.name AS name")
    }
    meanings = {key: str(row["name"]) for key, row in PRIMITIVE_MEANINGS.items()}
    meanings |= {
        row["id"]: row["name"]
        for row in query.read("MATCH (m:Meaning) RETURN m.id AS id, m.name AS name")
    }
    values = _values(query, [line.value_id for line in receipt.lines if line.value_id])
    words = _registry_words(query, receipt)
    return {
        "links": links,
        "items": items,
        "kinds": kinds,
        "meanings": meanings,
        "values": values,
        "words": words,
    }


_WORD_TYPES = {"Kind": "kind", "Meaning": "meaning", "PropertyName": "property name"}


def _registry_words(query: WorldQuery, receipt: Receipt) -> dict[str, dict[str, Any]]:
    """The registry rows an `o` line names, read fresh: a word's type and name, an alias's phrase
    and its target's type and name, or a capability minted with a word and that word."""
    ids = sorted({line.entity_id for line in receipt.lines if line.symbol == "o"})
    if not ids:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for label, word_type in _WORD_TYPES.items():
        for row in query.read(
            f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id AS id, n.name AS name", ids=ids
        ):
            found[row["id"]] = {"type": word_type, "name": row["name"]}
    for row in query.read(
        "MATCH (a:Alias) WHERE a.id IN $ids RETURN a.id AS id, a.phrase AS phrase, "
        "a.target_type AS target_type, a.target_id AS target_id",
        ids=ids,
    ):
        found[row["id"]] = {
            "type": "alias",
            "phrase": row["phrase"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
        }
    for row in query.read(
        "MATCH (c:Capability) WHERE c.id IN $ids RETURN c.id AS id, "
        "c.connector_type AS connector_type, c.capability AS capability, "
        "c.registry_id AS target_id",
        ids=ids,
    ):
        found[row["id"]] = {"type": "capability", **row}
    targets = sorted(
        row["target_id"] for row in found.values() if row["type"] in ("alias", "capability")
    )
    if targets:
        for label, word_type in _WORD_TYPES.items():
            for row in query.read(
                f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id AS id, n.name AS name",
                ids=targets,
            ):
                found.setdefault(row["id"], {"type": word_type, "name": row["name"]})
    return found


def _word_text(word_id: str, found: dict[str, dict[str, Any]]) -> str:
    word = found["words"].get(word_id)
    if word is None:
        return f"word {short_id(word_id)}"
    return f'{word["type"]} "{word["name"]}"'


def _ontology_line(line: ReceiptLine, found: dict[str, dict[str, Any]]) -> str:
    word = found["words"].get(line.entity_id) or {}
    if word.get("type") == "alias":
        return f'o alias "{word["phrase"]}" for {_word_text(word["target_id"], found)}'
    if word.get("type") == "capability":
        target = _word_text(word["target_id"], found)
        return f"o capability {word['connector_type']} {word['capability']} for {target}"
    if line.field == "status":
        status = _shown(found["values"].get(line.value_id))
        return f"o promote {_word_text(line.entity_id, found)} ({status})"
    return f"o mint {_word_text(line.entity_id, found)} ({line.end_class})"


def _link_text(link_id: str, found: dict[str, dict[str, Any]]) -> str:
    link = found["links"].get(link_id)
    if link is None:
        return f"link {short_id(link_id)}"
    meaning = found["meanings"].get(link["meaning"], short_id(link["meaning"]))
    return (
        f"link {short_id(link_id)}: {short_id(link['from_item'])} -[{meaning}]-> "
        f"{short_id(link['to_item'])}"
    )


def _render_line(line: ReceiptLine, found: dict[str, dict[str, Any]]) -> str:
    entity = short_id(line.entity_id)
    if line.symbol == "+":
        item = found["items"].get(line.entity_id, {})
        kind = found["kinds"].get(item.get("kind"), "?")
        return f"+ item {entity} ({kind}): {item.get('title', '')}"
    if line.symbol == ">":
        text = f"> {_link_text(line.entity_id, found)}"
        if line.stored_ended:
            text += f" (stored ended: {_LINK_END_WORDS.get(line.end_class or '', line.end_class)})"
        return text
    if line.symbol == "*":
        value = _shown(found["values"].get(line.value_id))
        text = f"* item {entity}: {line.field}={value}"
        if line.history_only:
            if line.world_time:
                text += f" at {_month_day(line.world_time)}"
            text += ", history only (the head is newer)"
        return text
    if line.symbol == "x" and line.entity_type == "link":
        words = _LINK_END_WORDS.get(line.end_class or "", line.end_class)
        return f"x {_link_text(line.entity_id, found)} ({words})"
    if line.symbol == "x":
        return f"x item {entity} ({_ITEM_END_WORDS.get(line.end_class or '', line.end_class)})"
    if line.symbol == "^":
        return f"^ item {entity} reopened"
    if line.symbol == "=" and line.already_known:
        return f"= {_link_text(line.entity_id, found)} (already known; evidence added)"
    if line.symbol == "=":
        return (
            f"= {line.entity_type} {entity} evidence {short_id(line.evidence_id or '')} "
            f"({line.polarity}, {line.source_type})"
        )
    if line.symbol == "-":
        return f"- evidence {entity} ({line.end_reason})"
    if line.symbol == "o":
        return _ontology_line(line, found)
    return f"{line.symbol} {line.entity_type} {entity}"


def render_receipt(driver: Driver, world_id: str, receipt: Receipt | DeclinedReceipt) -> str:
    """The receipt in D2's fixed grammar, with titles, names and values read fresh."""
    if isinstance(receipt, DeclinedReceipt):
        return receipt.render()
    head = f"{receipt.state} ({len(receipt.lines)})"
    if receipt.commit_id:
        head += f", commit {short_id(receipt.commit_id)}"
    if receipt.request_id:
        head += f", request {short_id(receipt.request_id)}"
    query = WorldQuery(driver, world_id)
    if receipt.commit_id is None:
        return "\n".join([head, *_finished_source(query, receipt.request_id)])
    if not receipt.lines:
        return head
    found = _lookups(query, receipt)
    return "\n".join([head, *(f"  {_render_line(line, found)}" for line in receipt.lines)])


def _finished_source(query: WorldQuery, request_id: str | None) -> list[str]:
    """A receipt with no commit has no entry to name, so it names the source the pass read."""
    rows = query.read("MATCH (r:Request {id: $id}) RETURN r.source_id AS id", id=request_id)
    if not rows or not rows[0]["id"]:
        return []
    return [f"  source {short_id(rows[0]['id'])}: nothing to write"]
