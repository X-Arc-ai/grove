"""D14's whole-store fixture includes S13's paired Item and Belief identities."""

import pytest

from grove.store.replay import FORGOTTEN_MARKER, ReplayRefused
from tests.store.test_replay import Book, item, recorded, registry, world


def paired():
    book = Book()
    kinds = registry(book)
    item_id = item(book, book.op(), kinds["place"], "Paired belief")
    belief = book.row(book.op(), "belief", "beliefs", {"item_id": item_id})
    return book, item_id, belief


def test_paired_belief_rebuild_keeps_separate_rows_versions_and_touches():
    book, item_id, _ = paired()
    book.add(book.op(), "belief", item_id, "externally_testable", True)
    belief_at = recorded(book.minute)
    book.add(book.op(), "item", item_id, "title", "Retained title", old="Paired belief")
    found = book.rebuild()
    assert found.rows["items"][item_id]["title"] == "Retained title"
    assert found.rows["items"][item_id]["version"] == 2
    assert found.rows["items"][item_id]["touched_at"] == recorded(book.minute)
    assert found.rows["beliefs"][item_id]["externally_testable"] is True
    assert found.rows["beliefs"][item_id]["version"] == 2
    assert found.rows["beliefs"][item_id]["touched_at"] == belief_at


def test_duplicate_belief_identity_still_refuses():
    book, item_id, _ = paired()
    book.row(book.op(), "belief", "beliefs", {"item_id": item_id})
    with pytest.raises(ReplayRefused) as failure:
        book.rebuild()
    assert failure.value.rule == "id_not_new"
    assert failure.value.sequence == book.entries[-1]["sequence"]


def test_forgotten_item_also_omits_its_belief_side_row():
    book, item_id, _ = paired()
    create = next(e for e in book.entries if e["entity_type"] == "item")
    book.values[create["new_value_id"]] = FORGOTTEN_MARKER
    found = book.rebuild()
    assert item_id not in found.rows["items"]
    assert item_id not in found.rows["beliefs"]


def test_belief_creation_does_not_erase_the_item_birth_guard():
    book, item_id, _ = paired()
    book.add(
        book.op(), "item", item_id, "title", "Too early", old="Paired belief", world_time=world(0)
    )
    with pytest.raises(ReplayRefused) as failure:
        book.rebuild()
    assert failure.value.rule == "older_than_head"
