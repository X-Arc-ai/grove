"""Immutable current-packet page preparation, before atomic cursor admission.

The production orchestrator must supply the owner's filtered, fully selected/rendered result.
No policy, principal, database reader or allow-all default lives here. Every item and hub page
is serialized before CursorStore.admit can expose the first page.
"""

import hashlib
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, Protocol

from grove.reads.capture import Capture, encode
from grove.reads.coverage import CoverageStatement, decide_answer
from grove.reads.cursors import MAX_BYTES, MAX_PAGES, CursorBinding, PreparedPages, handles, prepare
from grove.reads.requests import RecallQuery
from grove.reads.runtime import deadline, snapshot_busy

if TYPE_CHECKING:
    from grove.reads.current_packet import CurrentRenderer

MAX_PAGE_BYTES = 1_048_576
LINKS_PER_PAGE = 50


class Rendered(Protocol):
    card: dict
    link_ids: tuple[str, ...]


def query_document(query: RecallQuery) -> dict:
    value = asdict(query)
    del value["cursor"]
    return value


def query_digest(query: RecallQuery) -> str:
    return hashlib.sha256(encode(query_document(query))).hexdigest()


def prepare_renderer_pages(
    captured: Capture,
    query: RecallQuery,
    binding: CursorBinding,
    renderer: "CurrentRenderer",
    selected_ids: Sequence[str],
    *,
    complete: bool,
    ambiguous: bool = False,
) -> PreparedPages:
    """Render a complete ordered selection from the same authorized captured view.

    The production caller still owns capture, D14 filtering and ranking. Run this pure join
    in ReadRuntime.worker; it honors the explicit evidence option before immutable admission.
    """
    deadline()
    if (
        complete is not True
        or renderer.world_id != captured.world_id
        or renderer.captured_at != captured.reflected_at
        or isinstance(selected_ids, str | bytes)
    ):
        raise snapshot_busy()
    rendered = []
    for identity in selected_ids:
        deadline()
        rendered.append(
            renderer.render_item(identity, include_evidence="evidence" in query.include)
        )
    return prepare_current_pages(
        captured,
        query,
        binding,
        rendered,
        renderer.coverage,
        renderer.render_link,
        complete=complete,
        ambiguous=ambiguous,
    )


def prepare_current_pages(
    captured: Capture,
    query: RecallQuery,
    binding: CursorBinding,
    rendered: Sequence[Rendered],
    coverage: CoverageStatement,
    render_link: Callable[[str, str], dict],
    *,
    complete: bool,
    ambiguous: bool = False,
) -> PreparedPages:
    """Prepare one item-page chain and independent hub continuation chains.

    Run in ReadRuntime.worker after D14 filtering, complete selection and ranking. `render_link`
    is the same pure renderer that built the card, never a source lookup. Counts retain the
    complete selected item inventory; link pages additionally name their own exact link counts.
    An oversized page refuses the entire capture, never a prefix or a false absence.
    """
    deadline()
    if (
        complete is not True
        or query.about is None
        or query.time.mode != "current"
        or query.areas
        or set(query.include) - {"evidence"}
        or type(query.limit) is not int
        or not 1 <= query.limit <= 50
        or binding.world_id != captured.world_id
        or binding.capture_id != captured.capture_id
        or binding.page_size != query.limit
        or binding.query_digest != query_digest(query)
    ):
        raise snapshot_busy()
    cards, remaining, seen = [], [], set()
    for item in rendered:
        deadline()
        card = deepcopy(item.card)
        item_id, links = card.get("id"), card.get("links")
        link_ids = item.link_ids
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in seen
            or not isinstance(links, list)
            or type(link_ids) is not tuple
            or any(not isinstance(key, str) or not key for key in link_ids)
            or len(set(link_ids)) != len(link_ids)
        ):
            raise snapshot_busy()
        shown = [link.get("id") for link in links if isinstance(link, dict)]
        if len(shown) != len(links) or len(set(shown)) != len(shown) or set(shown) - set(link_ids):
            raise snapshot_busy()
        missing = tuple(key for key in link_ids if key not in set(shown))
        if card.get("links_remaining") != len(missing) or card.get("links_need_cursor") is not bool(
            missing
        ):
            raise snapshot_busy()
        seen.add(item_id)
        cards.append(card)
        remaining.append(missing)

    item_pages = max(1, (len(cards) + query.limit - 1) // query.limit)
    link_pages = sum((len(keys) + LINKS_PER_PAGE - 1) // LINKS_PER_PAGE for keys in remaining)
    if item_pages + link_pages > MAX_PAGES:
        raise snapshot_busy()
    page_handles = handles(item_pages + link_pages)
    # Templates refer only to preallocated opaque handles. No unpromised lazy page is possible.
    link_starts, cursor = {}, item_pages
    for card, keys in zip(cards, remaining, strict=True):
        if keys:
            link_starts[card["id"]] = cursor
            card["links_cursor"] = page_handles[cursor]
            cursor += (len(keys) + LINKS_PER_PAGE - 1) // LINKS_PER_PAGE
        else:
            card["links_cursor"] = None

    document = query_document(query)
    source_coverage = asdict(coverage)
    base = {
        "commit": captured.commit,
        "history_sequence": captured.history_sequence,
        "capture_id": captured.capture_id,
        "reflected_at": captured.reflected_at,
        "query": document,
    }
    total_bytes = 0

    def packet(items: list[dict], next_cursor: str | None, page: dict) -> bytes:
        nonlocal total_bytes
        deadline()
        decision = decide_answer(
            coverage,
            total=len(cards),
            returned=len(items),
            eligibility_complete=True,
            ambiguous=ambiguous,
        )
        payload = encode(
            {
                **base,
                "answer": decision.answer,
                "items": items,
                "coverage": {
                    **source_coverage,
                    "returned": decision.returned,
                    "total": decision.total,
                    "truncated": decision.truncated,
                },
                "page": page,
                "next_cursor": next_cursor,
            }
        )
        total_bytes += len(payload)
        if len(payload) > MAX_PAGE_BYTES or total_bytes > MAX_BYTES:
            raise snapshot_busy()
        return payload

    pages = []
    for index in range(item_pages):
        chunk = cards[index * query.limit : (index + 1) * query.limit]
        next_cursor = page_handles[index + 1] if index + 1 < item_pages else None
        pages.append(packet(chunk, next_cursor, {"kind": "items"}))
    for card, keys in zip(cards, remaining, strict=True):
        start = link_starts.get(card["id"])
        if start is None:
            continue
        for offset in range(0, len(keys), LINKS_PER_PAGE):
            deadline()
            chunk_ids = keys[offset : offset + LINKS_PER_PAGE]
            chunk = [render_link(card["id"], key) for key in chunk_ids]
            if any(
                not isinstance(link, dict) or link.get("id") != key
                for key, link in zip(chunk_ids, chunk, strict=True)
            ):
                raise snapshot_busy()
            end = offset + len(chunk)
            next_cursor = (
                page_handles[start + offset // LINKS_PER_PAGE + 1] if end < len(keys) else None
            )
            continuation = {
                **card,
                "links": chunk,
                "links_remaining": len(keys) - end,
                "links_need_cursor": next_cursor is not None,
                "links_cursor": next_cursor,
            }
            pages.append(
                packet(
                    [continuation],
                    next_cursor,
                    {
                        "kind": "links",
                        "item": card["id"],
                        "returned": len(chunk),
                        "total": len(card["links"]) + len(keys),
                        "remaining": len(keys) - end,
                    },
                )
            )
    return prepare(binding, page_handles, pages)
