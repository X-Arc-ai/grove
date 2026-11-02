"""One normal form for a phrase: the seed writes it and the resolver will read it."""

import pytest

from grove.store.normalise import normalise_phrase

SURROGATES = range(0xD800, 0xE000)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Gated On", "gated on"),
        ("GATED ON", "gated on"),
        ("STRASSE", "strasse"),
        ("straße", "strasse"),
        ("café", "cafe"),
        ("cafe\u0301", "cafe"),
        ("Crème Brûlée", "creme brulee"),
        ("gated_on", "gated on"),
        ("__gated__on__", "gated on"),
        ("gated-on", "gated on"),
        ("gated\u2013on", "gated on"),
        ("gated, on!", "gated on"),
        ("(gated) on?", "gated on"),
        ("gated/on", "gated on"),
        ("gated+on", "gated on"),
        ("  gated   on  ", "gated on"),
        ("gated\t\non", "gated on"),
        ("gated\u00a0on", "gated on"),
        ("gated", "gated"),
        ("", ""),
        ("---", ""),
        # Invisible format characters fall away, so a phrase that reads the same is the same.
        ("‎Rowan Okonkwo", "rowan okonkwo"),
        ("﻿Rowan Okonkwo", "rowan okonkwo"),
        ("Ro­wan Okon­kwo", "rowan okonkwo"),
        ("Ro​wan Okon​kwo", "rowan okonkwo"),
        ("R​o​w​a​n", "rowan"),
        ("gated⁠on", "gatedon"),
        ("​‍﻿", ""),
    ],
)
def test_normal_form(phrase, expected):
    assert normalise_phrase(phrase) == expected


def test_the_three_spellings_of_one_phrase_are_one_phrase():
    assert {normalise_phrase(p) for p in ("gated_on", "Gated-On", "gated on")} == {"gated on"}


def test_a_letter_of_another_script_stays():
    assert normalise_phrase("Москва") == "москва"


def test_an_emoji_stays_so_the_phrase_is_not_empty():
    assert normalise_phrase("\U0001f3e0") == "\U0001f3e0"


def test_an_emoji_with_a_joiner_stays_so_the_phrase_is_not_empty():
    family = "\U0001f468‍\U0001f469‍\U0001f467"
    assert normalise_phrase(family) == "\U0001f468\U0001f469\U0001f467"


def test_a_compatibility_capital_is_folded():
    assert normalise_phrase("\u2103") == "\u00b0c"


@pytest.mark.parametrize(
    "phrase",
    ["Gated-On", "  a__b  ", "Crème Brûlée", "\u2103", "\u0390", "\ufb01", "straße", "---", ""],
)
def test_it_is_idempotent(phrase):
    once = normalise_phrase(phrase)
    assert normalise_phrase(once) == once


def test_it_is_idempotent_on_every_character_of_the_basic_plane():
    for code in range(0x10000):
        if code in SURROGATES:
            continue
        once = normalise_phrase(f"a{chr(code)}b")
        assert normalise_phrase(once) == once, hex(code)
