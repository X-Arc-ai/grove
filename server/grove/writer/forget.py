"""Authenticated D14 erasure: one world transaction and a durable external intent."""

import json
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from grove import clock
from grove.ids import new_id
from grove.store.catalogue import TABLES
from grove.store.current import read_rows
from grove.store.d3_fingerprint import SNAPSHOT_TABLES, fingerprint, key_column
from grove.store.replay import rebuild
from grove.writer.forget_ledger import append_intent, locked_ledger
from grove.writer.forget_plan import build_plan, transform, validate_plan
from grove.writer.tx import WorldTx, WorldWriter

_TABLES = {t.name: t for t in TABLES if t.scope == "world"}
_LEDGER_COLUMNS = {p for c in _TABLES["forget_ledger"].columns for p in c.props}


def _pattern(name: str) -> str:
    held = _TABLES[name]
    return (
        f"(:Item)-[n:{held.label}]->(:Item)"
        if held.element == "relationship"
        else f"(n:{held.label})"
    )


def read_inventory(source: Any) -> dict[str, list[dict]]:
    return {
        name: [r["n"] for r in read_rows(source, f"MATCH {_pattern(name)} RETURN n")]
        for name in _TABLES
    }


def _keys(name: str, row: dict) -> tuple:
    keys = ("id",) if "id" in row else _TABLES[name].uniques[0].props
    return tuple((key, row.get(key)) for key in keys if key != "world_id")


def write_inventory(tx: WorldTx, before: dict, after: dict) -> None:
    """Apply exact row differences. Untouched rows and history entries receive no write."""
    order = sorted(_TABLES, key=lambda n: _TABLES[n].element != "relationship")
    for name in order:
        old = {_keys(name, row): row for row in before.get(name, [])}
        new = {_keys(name, row): row for row in after.get(name, [])}
        for key in old.keys() | new.keys():
            previous, following = old.get(key), new.get(key)
            if previous == following:
                continue
            if name == "change_entries":
                raise ValueError("forget cannot rewrite a history entry")
            params = {f"k{i}": value for i, (_, value) in enumerate(key)}
            where = " AND ".join(f"n.{column} = $k{i}" for i, (column, _) in enumerate(key))
            pattern = _pattern(name)
            if following is None:
                verb = "DELETE" if _TABLES[name].element == "relationship" else "DETACH DELETE"
                tx.run(f"MATCH {pattern} WHERE {where} {verb} n", **params)
            elif previous is None:
                if _TABLES[name].element == "relationship":
                    raise ValueError("forget cannot create a relationship")
                tx.run(
                    f"CREATE {pattern} SET n += $row",
                    row={k: v for k, v in following.items() if k != "world_id"},
                )
            else:
                removed = sorted(previous.keys() - following.keys() - {"world_id"})
                remove = " REMOVE " + ", ".join(f"n.{prop}" for prop in removed) if removed else ""
                props = {
                    k: v for k, v in following.items() if k != "world_id" and previous.get(k) != v
                }
                tx.run(
                    f"MATCH {pattern} WHERE {where} SET n += $props{remove}", props=props, **params
                )


def inventory_fingerprint(inventory: dict) -> dict:
    """The same D3 measures computed from an in-memory inventory after approved redactions."""
    sizes = {}
    for held in SNAPSHOT_TABLES:
        rows = inventory.get(held.name, [])

        def identity(row, held=held):
            if key_column(held) == "id":
                return row["id"]
            return "|".join(str(row.get(k, "")) for k in held.uniques[0].props)

        sizes[held.name] = {
            "count": len(rows),
            "newest": max((identity(r) for r in rows), default=None),
        }
    entries = inventory["change_entries"]
    return fingerprint(
        inventory,
        {
            "last_sequence": max((r["sequence"] for r in entries), default=0),
            "entries": len(entries),
        },
        sizes,
    )


def validate_ledger_rows(world_id: str, rows: Iterable[Mapping]) -> list[dict]:
    """Validate the complete input before the first replay transaction, including later rows."""
    from grove.writer.clocks import parse_instant

    result = []
    ids = set()
    for raw in rows:
        row = dict(raw)
        if row.get("world_id", world_id) != world_id or set(row) - _LEDGER_COLUMNS:
            raise ValueError("invalid forget ledger scope or columns")
        if "id" in row or "plan" in row:
            if not isinstance(row.get("id"), str) or row["id"] in ids:
                raise ValueError("invalid forget ledger identity")
            ids.add(row["id"])
        if row.get("target_type") not in {
            "item",
            "field_value",
            "named_value",
            "source_span",
            "material",
        }:
            raise ValueError("invalid forget target type")
        for name in ("approved_at",):
            value = row.get(name)
            if isinstance(value, dict) and value.get("$t") == "instant":
                value = value.get("v")
            if value is not None:
                row[name] = parse_instant(value)
            elif "plan" in row:
                raise ValueError("planned forget requires approval time")
        value = row.get("backups_clear_after")
        if isinstance(value, dict) and value.get("$t") == "date":
            value = value.get("v")
        if isinstance(value, str):
            row["backups_clear_after"] = date.fromisoformat(value)
        for name in ("internal_ids", "locators"):
            value = row.get(name, [])
            if not isinstance(value, list | tuple) or any(not isinstance(v, str) for v in value):
                raise ValueError("invalid forget ledger identifiers")
        if "plan" in row:
            plan = json.loads(row["plan"]) if isinstance(row["plan"], str) else row["plan"]
            validate_plan(plan)
            row["plan"] = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        elif row["target_type"] == "source_span":
            raise ValueError("source erasure needs a complete plan")
        result.append(row)
    return result


def replay_plans(tx: WorldTx, rows: list[dict]) -> tuple[set[str], dict]:
    before = read_inventory(tx)
    after = before
    gone: set[str] = set()
    for row in rows:
        plan = json.loads(row["plan"])
        after, _ = transform(after, plan, row["approved_at"])
        gone.update(s["key"]["id"] for s in plan["delete"] if "id" in s["key"])
    write_inventory(tx, before, after)
    return gone, after


def forget(
    driver,
    world_id: str,
    target: dict,
    *,
    approval_token: str,
    actor: str,
    ledger_dir: Path | str | None = None,
    material_session=None,
    session_hash=None,
) -> dict:
    from grove.writer.forget_approvals import consume_approval, target_digest, validate_target

    target = validate_target(target)
    target_hash = target_digest(target)
    at = clock.now(world_id)
    receipt_id = new_id(world_id)
    with locked_ledger(ledger_dir) as root:

        def work(tx: WorldTx):
            tx.lock_world()
            if target["type"] == "material":
                from grove.writer.forget_ledger import checked_under_lock
                from grove.writer.materials import (
                    MaterialSessionEnded,
                    check_revision,
                    check_session,
                )

                if material_session is None or session_hash is None:
                    raise MaterialSessionEnded()
                check_session(tx, material_session, session_hash)
                check_revision(tx, target)
                checked_under_lock(
                    root, world_id, [r["f"] for r in tx.run("MATCH (f:ForgetLedger) RETURN f")]
                )
            consume_approval(tx, approval_token, target_hash, actor)
            before = read_inventory(tx)
            plan = build_plan(before, target)
            after, counts = transform(before, plan, at)
            # Check the rewritten history before publishing an external intent. This is
            # the normal replay with its normal validation, never a bypass mode.
            entries = after["change_entries"]
            values = {r["id"]: r["value"] for r in after["history_values"]}
            rebuild(entries, values)
            receipt = {
                "receipt_id": receipt_id,
                "counts": counts,
                "backups_clear_after": (at.date() + timedelta(days=30)).isoformat(),
                "downloaded_exports": "out_of_reach",
            }
            row = {
                "id": receipt_id,
                "world_id": world_id,
                "target_type": target["type"],
                "internal_ids": sorted(
                    {
                        v
                        for v in target.values()
                        if isinstance(v, str)
                        and v != target.get("value")
                        and v in {r.get("id") for rs in before.values() for r in rs}
                    }
                ),
                "locators": plan["locators"],
                "requested_by": actor,
                "approved_at": at,
                "backups_clear_after": at.date() + timedelta(days=30),
                "receipt": json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                "plan": json.dumps(plan, sort_keys=True, separators=(",", ":")),
            }
            validate_ledger_rows(world_id, [row])
            append_intent(root, world_id, row, before["forget_ledger"])
            write_inventory(tx, before, after)
            tx.run(
                "CREATE (n:ForgetLedger) SET n += $row",
                row={k: v for k, v in row.items() if k != "world_id"},
            )
            return receipt

        return WorldWriter(driver, world_id, timeout=600).transaction(work)
