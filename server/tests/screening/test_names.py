"""The rename map and the denylist: whole words, case-insensitive, after NFKC and case folding."""

import unicodedata

import pytest

from grove.config import ConfigError
from grove.screening import Denylist, Message, RenameMap
from grove.screening.names import Normalised
from tests.screening._plants import DENYLIST, RENAME, screen_one, screener


def renamed(mapping: dict[str, str], text: str) -> str:
    """Apply RenameMap.find alone, so these tests see the matcher and no other step."""
    out = text
    for start, end, label in reversed(
        RenameMap.from_mapping(mapping).find(Normalised(text, fold_case=True))
    ):
        out = out[:start] + label + out[end:]
    return out


RENAME_CASES = [
    pytest.param(RENAME, "Orla Vance joined", "Co-founder joined", id="multi-word-form"),
    pytest.param(
        RENAME, "ORLA said orla agreed", "Co-founder said Co-founder agreed", id="any-case"
    ),
    pytest.param(RENAME, "Orla's deck", "Co-founder's deck", id="possessive"),
    pytest.param(RENAME, "Orla’s deck", "Co-founder’s deck", id="curly-possessive"),
    pytest.param(
        RENAME,
        '(Orla), Orla. Orla! "Orla" Orla-led @orla #orla',
        '(Co-founder), Co-founder. Co-founder! "Co-founder" Co-founder-led @Co-founder #Co-founder',
        id="punctuation-around",
    ),
    pytest.param(
        RENAME,
        "Orlaville and orla_corp and Orla2",
        "Orlaville and orla_corp and Orla2",
        id="whole-word-only",
    ),
    pytest.param(RENAME, "Orla  Vance", "Co-founder", id="double-space"),
    pytest.param(RENAME, "Orla Vance", "Co-founder", id="non-breaking-space"),
    pytest.param(RENAME, "Orla\tVance", "Co-founder", id="tab"),
    pytest.param(RENAME, "Orla\nVance", "Co-founder", id="line-break"),
    pytest.param(RENAME, "Orla    Vance", "Co-founder", id="mixed-space-run"),
    pytest.param(RENAME, "Ｏｒｌａ Ｖａｎｃｅ said", "Co-founder said", id="full-width"),
    pytest.param(RENAME, "Or​la wrote", "Co-founder wrote", id="zero-width-space-inside"),
    pytest.param(RENAME, "O. Vance and O.Vance", "Co-founder and O.Vance", id="form-with-dot"),
    pytest.param(
        {"Renée": "Partner"}, "Renée and RENÉE", "Partner and Partner", id="combining-accent"
    ),
    pytest.param({"Straße": "Street"}, "STRASSE 4", "Street 4", id="case-fold-expands"),
    pytest.param(
        {"Orla": "A", "Orla Vance": "B"}, "Orla Vance and Orla", "B and A", id="longest-first"
    ),
    pytest.param(
        {"Orla": "Vance", "Vance": "Orla"}, "Orla met Vance", "Vance met Orla", id="one-pass-swap"
    ),
    pytest.param({"Orla": "Orla Vance"}, "Orla", "Orla Vance", id="label-never-matched-again"),
    pytest.param(RENAME, "", "", id="empty"),
]


@pytest.mark.parametrize("mapping, text, expected", RENAME_CASES)
def test_rename_map(mapping, text, expected):
    assert renamed(mapping, text) == expected


def test_the_rest_of_the_text_keeps_its_original_form():
    # Only the replaced span changes: full-width digits and case elsewhere stay as written.
    text = "１２ Orla ÉTÉ ﬁx"
    assert renamed(RENAME, text) == "１２ Co-founder ÉTÉ ﬁx"


def test_rename_counts_one_per_replaced_span_and_covers_the_sender():
    screen = screener(rename=RENAME)
    screened = screen.screen_source(
        "chat.txt", [Message("m1", "Orla Vance", "Orla Vance said Orla's plan holds")]
    )
    message = screened.messages[0]
    assert (message.sender, message.text) == (
        "Co-founder",
        "Co-founder said Co-founder's plan holds",
    )
    assert dict(screened.counts) == {"renamed": 3}


def test_counts_of_a_rename_map():
    names = RenameMap.from_mapping(
        {"Orla Vance": "Co-founder", "orla vance": "Co-founder", "Tamsin": "Designer"}
    )
    assert (names.form_count, names.label_count) == (2, 2)


def test_a_conflict_in_a_mapping_is_a_config_error_naming_positions():
    with pytest.raises(ConfigError) as caught:
        RenameMap.from_mapping({"Orla": "Co-founder", "ORLA": "Client"})
    assert "entries 1 and 2" in str(caught.value)
    assert "Orla" not in str(caught.value) and "Client" not in str(caught.value)


DENY_CASES = [
    pytest.param(None, "Brightwell Logistics sent the brief", True, id="in-text"),
    pytest.param("Brightwell Logistics", "see you at noon", True, id="in-sender"),
    pytest.param("Tamsin", "BRIGHTWELL logistics again", True, id="any-case"),
    pytest.param("Tamsin", "Brightwell   Logistics", True, id="odd-whitespace"),
    pytest.param("Tamsin", "Ｂｒｉｇｈｔｗｅｌｌ Logistics", True, id="full-width"),
    pytest.param("Tamsin", "(Halvard Quist's) note", True, id="possessive-in-parens"),
    pytest.param("Tamsin", "Brightwell Logisticsville", False, id="not-inside-a-longer-word"),
    pytest.param("Tamsin", "Brightwell alone", False, id="part-of-a-term"),
    pytest.param("Tamsin", "halvard_quist", False, id="underscore-joins-words"),
]


@pytest.mark.parametrize("sender, text, dropped", DENY_CASES)
def test_denylist(sender, text, dropped):
    kept, counts = screen_one(screener(denylist=DENYLIST), text, sender=sender)
    if dropped:
        assert (kept, counts) == (None, {"denylisted": 1})
    else:
        assert (kept, counts) == (text, {})


def test_nothing_else_is_counted_for_a_dropped_message():
    screen = screener(rename=RENAME, denylist=DENYLIST, pay=True)
    screened = screen.screen_source(
        "chat.txt",
        [
            Message(
                "m1",
                "Orla Vance <orla@brightwell-mail.example>",
                "Brightwell Logistics: card 4111 1111 1111 1111, call +44 7700 900123, "
                "my salary is 30k MAD, code 482910",
            ),
            Message("m2", "Halvard Quist", "a plain line"),
            Message("m3", "Tamsin", "kept"),
        ],
    )
    assert dict(screened.counts) == {"denylisted": 2}
    assert [m.text for m in screened.messages] == ["kept"]


def test_denylist_counts_and_empty_lists():
    assert Denylist(["Brightwell", "brightwell", "BRIGHTWELL"]).term_count == 1
    assert Denylist().term_count == 0
    assert not Denylist().holds(Normalised("Brightwell", fold_case=True))
    assert RenameMap().find(Normalised("Orla", fold_case=True)) == []


def test_normalised_offsets_map_back_to_the_original():
    text = "aＯｒｌａ ß Renée ​x"
    folded = Normalised(text, fold_case=True)
    assert folded.text == unicodedata.normalize("NFKC", "aorla ss renée x")
    start = folded.text.index("orla")
    assert folded.span(start, start + 4) == (1, 5)
    # "ß" folds to "ss": both copy characters come from the one original character.
    sharp = folded.text.index("ss")
    assert folded.span(sharp, sharp + 2) == (6, 7)
    accent = folded.text.index("renée")
    assert text[slice(*folded.span(accent, accent + 5))] == "Renée"


def test_normalised_ascii_is_the_identity_and_case_is_kept_for_detectors():
    assert Normalised("Orla Vance", fold_case=True).text == "orla vance"
    copy = Normalised("ＡＢc", fold_case=False)
    assert copy.text == "ABc"
    assert copy.span(0, 3) == (0, 3)
