"""Speaker kind and modality ride through screening as closed codes (D5): never screened, never
free text, and absent from a stored row when the connector did not set them."""

import json

import pytest

from grove.screening import Denylist, Message, RenameMap, Screener, ScreeningConfig


def _screener(**config) -> Screener:
    return Screener(ScreeningConfig(**config))


def test_codes_pass_through_and_text_is_still_screened():
    screener = _screener(rename=RenameMap.from_mapping({"Orla Pennick": "Co-founder"}))
    screened = screener.screen_source(
        "loc",
        [
            Message(
                key="k1",
                sender=None,
                text="Orla Pennick sent it",
                speaker_kind="ai_host",
                modality="voice",
            )
        ],
    )
    (row,) = json.loads(screened.content())
    assert row == {
        "at": None,
        "key": "k1",
        "modality": "voice",
        "sender": None,
        "speaker_kind": "ai_host",
        "text": "Co-founder sent it",
    }


def test_a_row_without_codes_keeps_its_four_fields():
    screened = _screener().screen_text("a note", sender="person-x")
    (row,) = json.loads(screened.content())
    assert set(row) == {"at", "key", "sender", "text"}


def test_a_dropped_message_drops_its_codes_too():
    screener = _screener(denylist=Denylist(["Kestrel"]))
    screened = screener.screen_source(
        "loc",
        [
            Message(key="k1", sender=None, text="Kestrel invoice", speaker_kind="person"),
            Message(key="k2", sender=None, text="lunch", speaker_kind="automated"),
        ],
    )
    assert [(m.key, m.speaker_kind) for m in screened.messages] == [("k2", "automated")]


@pytest.mark.parametrize(
    "fields",
    [
        {"speaker_kind": "the person"},
        {"speaker_kind": "robot"},
        {"modality": "video"},
        {"modality": 1},
        {"speaker_kind": ""},
    ],
)
def test_only_codes_of_the_closed_lists_are_taken(fields):
    with pytest.raises(ValueError):
        Message(key="k", sender=None, text="x", **fields)


def test_a_str_subclass_cannot_pass_for_a_code():
    class Sneaky(str):
        pass

    with pytest.raises(ValueError):
        Message(key="k", sender=None, text="x", speaker_kind=Sneaky("person"))
