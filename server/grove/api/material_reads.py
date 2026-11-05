"""Session-bound material reads with an exact external-intent check before disclosure.

These reads write nothing, but no text leaves while an approved forget is pending, so they hold the
D14 ledger lock. D14 stays an explicit API, so this module is here and not in grove.views.
"""

import base64
import json

from grove import clock
from grove.store.query import PlatformQuery, WorldQuery
from grove.views.session import find_session
from grove.writer.forget_ledger import checked_under_lock, locked_ledger
from grove.writer.materials import MaterialSessionEnded, latest


def _read(driver, session, cookie, ledger_dir, work):
    with locked_ledger(ledger_dir) as root:
        current = find_session(PlatformQuery(driver), cookie, clock.now())
        if (
            current is None
            or current.user_id != session.user_id
            or current.world_id != session.world_id
        ):
            raise MaterialSessionEnded()
        query = WorldQuery(driver, session.world_id)
        checked_under_lock(
            root, session.world_id, [r["f"] for r in query.read("MATCH (f:ForgetLedger) RETURN f")]
        )
        result = work(query)
        rows = PlatformQuery(driver).read(
            "MATCH (w:World {id: $world_id}) RETURN w.read_generation AS generation",
            world_id=session.world_id,
        )
        return {"account": session.user_id, "generation": rows[0]["generation"], **result}


def _source(query, source_id):
    rows = query.read("MATCH (s:Source {id: $id}) RETURN s", id=source_id)
    if not rows or rows[0]["s"].get("forget_state") == "forgotten":
        return []
    return json.loads(rows[0]["s"].get("content") or "[]")


def summary(query, row, detail=False, driver=None):
    titles = _source(query, row["title_source_id"])
    request = None
    if row.get("request_id"):
        from grove.writer.requests import receipt_of

        requests = query.read("MATCH (r:Request {id: $id}) RETURN r", id=row["request_id"])
        if requests:
            stored = requests[0]["r"]
            request = {"id": row["request_id"], "status": stored["status"], "changed_ids": []}
            if stored["status"] == "saved":
                receipt = receipt_of(driver, row["world_id"], stored)
                if receipt is not None:
                    request["receipt"] = receipt.to_json()
                    request["changed_ids"] = sorted(
                        {
                            line.entity_id
                            for line in receipt.lines
                            if line.entity_type in {"item", "link"} and not line.history_only
                        }
                    )
                else:
                    request["status"] = "applying"
    state = request["status"] if request else ("queued" if row["part_count"] else "declined")
    if state not in {"queued", "applying", "saved", "declined", "failed"}:
        state = "queued"
    result = {
        "id": row["material_id"],
        "kind": row["kind"],
        "format": row["format"],
        "title": titles[0]["text"] if titles else "",
        "revision": row["revision"],
        "received_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else row["created_at"],
        "source_state": "retained" if row["part_count"] else "screened_out",
        "graph_state": "held_provenance" if row["kind"] == "file" else state,
        "counts": {
            "parts": row["part_count"],
            "retained_characters": row["retained_characters"],
            "held": row["held_count"],
        },
        "request": request,
    }
    screening_counts = {}
    receipts = query.read(
        "MATCH (r:ScreeningReceipt) WHERE r.source_id IN $ids RETURN r.counts_per_class AS counts",
        ids=[row["title_source_id"], *row["source_ids"]],
    )
    for receipt in receipts:
        for name, count in json.loads(receipt["counts"] or "{}").items():
            screening_counts[name] = screening_counts.get(name, 0) + count
    result["screening_counts"] = screening_counts
    if detail:
        result["parts"] = [
            {"key": m["key"], "text": m["text"]}
            for sid in row["source_ids"]
            for m in _source(query, sid)
        ]
    return result


def detail(driver, session, cookie, material_id, *, ledger_dir=None):
    return _read(
        driver,
        session,
        cookie,
        ledger_dir,
        lambda q: {"material": summary(q, latest(q, material_id), True, driver)},
    )


def listing(driver, session, cookie, *, kind=None, limit=20, cursor=None, ledger_dir=None):
    if kind not in (None, "file", "note") or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid material page")
    after = ""
    if cursor:
        if len(cursor) > 128:
            raise ValueError("invalid material cursor")
        try:
            after = base64.urlsafe_b64decode(cursor.encode()).decode("ascii")
            from grove.ingest.materials import uuid_key

            uuid_key(after)
        except (ValueError, UnicodeError):
            raise ValueError("invalid material cursor") from None

    def work(q):
        rows = q.read(
            "MATCH (m:MaterialRevision {state: 'current'}) "
            "WHERE ($kind IS NULL OR m.kind = $kind) AND m.material_id > $after "
            "RETURN m ORDER BY m.material_id LIMIT $limit",
            kind=kind,
            after=after,
            limit=limit + 1,
        )
        return {
            "materials": [summary(q, r["m"], driver=driver) for r in rows[:limit]],
            "next_cursor": base64.urlsafe_b64encode(
                rows[limit - 1]["m"]["material_id"].encode()
            ).decode()
            if len(rows) > limit
            else None,
        }

    return _read(driver, session, cookie, ledger_dir, work)


def preview(driver, session, cookie, material_id, *, ledger_dir=None):
    def work(q):
        row = latest(q, material_id)
        all_rows = q.read("MATCH (m:MaterialRevision {material_id: $id}) RETURN m", id=material_id)
        return {
            "id": material_id,
            "revision": row["revision"],
            "revisions": len(all_rows),
            "sources": sum(len(r["m"]["source_ids"]) + 1 for r in all_rows),
            "scope": "all_revisions_and_derived_copies",
            "downloaded_exports": "out_of_reach",
            "backup_days": 30,
        }

    return _read(driver, session, cookie, ledger_dir, work)
