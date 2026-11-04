"""Pure identity controls; all names and captures are synthetic."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from grove.reads.identity import (
    IdentityAlias,
    IdentityError,
    IdentityField,
    IdentityItem,
    resolve_identity,
)

WORLD = "t_hosts-b1_11111111-1111-4111-8111-111111111111"
OTHER = "t_hosts-b1_22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def item(item_id="one", title="Elise Martel", **kwargs):
    return IdentityItem(WORLD, item_id, title, **kwargs)


def alias(phrase, target_id="one", **kwargs):
    return IdentityAlias(WORLD, target_id, phrase, "canonical", **kwargs)


def resolve(query, items=None, aliases=(), **kwargs):
    return resolve_identity(
        WORLD, query, [item()] if items is None else items, aliases, at=NOW, complete=True, **kwargs
    )


def test_exact_id_wins_over_another_items_title():
    result = resolve("one", [item(), item("two", "one")])
    assert result.answer == "found"
    assert [(c.item_id, c.classification) for c in result.candidates] == [("one", "exact_id")]


@pytest.mark.parametrize("query", ["ELISE MARTEL", "  Élise--Martel ", "élise\u200b martel"])
def test_titles_share_case_accent_and_format_normalization(query):
    result = resolve(query)
    assert result.answer == "found"
    assert result.candidates[0].classification == "exact_title"


@pytest.mark.parametrize(
    "kind,value",
    [
        ("name", "E. Martel"),
        ("nickname", "Lili"),
        ("handle", "@elise.work"),
        ("role", "my landlord"),
    ],
)
def test_explicit_authorized_fields_resolve(kind, value):
    result = resolve(value, [item(fields=[IdentityField(kind, value)])])
    assert result.answer == "found"
    assert result.candidates[0].classification == "exact_field"


def test_alias_is_identified_for_exact_alias_boost():
    result = resolve("Elise Martel", aliases=[alias("Elise Martel")])
    assert result.candidates[0].classification == "exact_alias"


@pytest.mark.parametrize("status", ["canonical", "provisional"])
def test_usable_alias_statuses_resolve(status):
    result = resolve("Lili", aliases=[replace(alias("Lili"), status=status)])
    assert result.candidates[0].item_id == "one"


@pytest.mark.parametrize("status", ["retired", "split"])
def test_unusable_alias_statuses_do_not_resolve(status):
    assert not resolve("Lili", aliases=[replace(alias("Lili"), status=status)]).candidates


@pytest.mark.parametrize("target_type", ["kind", "meaning", "property_name"])
def test_non_item_aliases_cannot_create_identity_matches(target_type):
    assert not resolve("Lili", aliases=[alias("Lili", target_type=target_type)]).candidates


def test_hidden_or_ineligible_target_alias_cannot_create_a_candidate():
    assert not resolve("Lili", aliases=[alias("Lili", target_id="not-visible")]).candidates


def test_role_handoff_uses_selected_time_with_half_open_bounds():
    boundary = datetime(2026, 3, 1, tzinfo=UTC)
    people = [item(), item("two", "Boris Nevsky")]
    roles = [
        alias("the treasurer", valid_to=boundary),
        alias("the treasurer", "two", valid_from=boundary),
    ]
    before = resolve_identity(
        WORLD,
        "the treasurer",
        people,
        roles,
        at=boundary - timedelta(microseconds=1),
        complete=True,
    )
    at = resolve_identity(WORLD, "the treasurer", people, roles, at=boundary, complete=True)
    assert [c.item_id for c in before.candidates] == ["one"]
    assert [c.item_id for c in at.candidates] == ["two"]


def test_dated_explicit_role_field_obeys_the_same_bounds():
    fields = [IdentityField("role", "my landlord", valid_to=NOW)]
    assert resolve("my landlord", [item(fields=fields)]).answer == "unmatched"
    fields = [IdentityField("role", "my landlord", valid_from=NOW)]
    assert resolve("my landlord", [item(fields=fields)]).answer == "found"


@pytest.mark.parametrize("query", ["Elise Martl", "Elise Martell", "Elise Nartel", "Eilse Martel"])
def test_small_misspellings_and_transposition_resolve(query):
    result = resolve(query)
    assert result.answer == "found"
    assert result.candidates[0].classification == "fuzzy"
    assert 0 < result.candidates[0].score < 1


def test_short_name_edit_distance_does_not_require_shared_trigrams():
    result = resolve("Axl", [item(title="Axel")])
    assert [c.item_id for c in result.candidates] == ["one"]


def test_trigram_candidates_exist_beyond_small_edit_distance():
    result = resolve("Elise Martel", [item(title="Elise Martel Rivera")])
    assert result.answer == "found"
    assert result.candidates[0].classification == "fuzzy"


def test_all_close_candidates_are_ambiguous_even_with_different_scores():
    result = resolve("Elise Martl", [item(), item("two", "Elise Martell")])
    assert result.answer == "ambiguous"
    assert {c.item_id for c in result.candidates} == {"one", "two"}


def test_exact_title_and_different_exact_alias_are_ambiguous():
    result = resolve(
        "Lili", [item(title="Lili"), item("two", "Boris Nevsky")], [alias("Lili", "two")]
    )
    assert result.answer == "ambiguous"
    assert {c.item_id for c in result.candidates} == {"one", "two"}


def test_exact_phrase_does_not_hide_a_different_close_identity():
    result = resolve("Elise Martel", [item(), item("two", "Elise Martell")])
    assert result.answer == "ambiguous"
    assert [c.item_id for c in result.candidates] == ["one", "two"]
    assert result.candidates[0].classification == "exact_title"


def test_no_top_k_limit_and_stable_id_order():
    people = [item(f"person-{index:03}", "Lili") for index in reversed(range(125))]
    result = resolve("lili", people)
    assert result.answer == "ambiguous"
    assert len(result.candidates) == 125
    assert [c.item_id for c in result.candidates] == sorted(p.item_id for p in people)


def test_duplicate_phrases_do_not_duplicate_candidates():
    result = resolve(
        "Lili",
        [item(title="Lili", fields=[IdentityField("nickname", "Lili")])],
        [alias("Lili"), alias("Lili")],
    )
    assert len(result.candidates) == 1


def test_script_transliteration_requires_an_explicit_alias():
    person = item(title="Москва")
    assert resolve("Moskva", [person]).answer == "unmatched"
    assert resolve("Moskva", [person], [alias("Moskva")]).answer == "found"


def test_voice_label_is_not_an_identity_field_and_does_not_resolve_alone():
    with pytest.raises(IdentityError, match="invalid_identity_field"):
        IdentityField("speaker", "A")
    assert resolve("A").answer == "unmatched"
    assert resolve("A", [item(title="B")]).answer == "unmatched"
    assert resolve("A", [item(title="A F")]).answer == "unmatched"


def test_hidden_retrieval_text_is_not_a_supported_identity_field():
    for field_kind in ("search_terms", "body", "history", "property_values", "speaker_label"):
        with pytest.raises(IdentityError, match="invalid_identity_field"):
            IdentityField(field_kind, "hidden handle")


def test_results_are_immutable_and_do_not_expose_matched_text():
    result = resolve("Lili", aliases=[alias("Lili")])
    assert "Lili" not in repr(asdict(result))
    with pytest.raises(FrozenInstanceError):
        result.candidates[0].score = 0
    with pytest.raises(FrozenInstanceError):
        result.answer = "unmatched"


def test_input_lists_are_copied_and_never_mutated():
    fields = [IdentityField("handle", "@elise")]
    people = [item(fields=fields)]
    aliases = [alias("Lili")]
    before = (asdict(people[0]), asdict(aliases[0]))
    resolve("Lili", people, aliases)
    fields.clear()
    assert (asdict(people[0]), asdict(aliases[0])) == before
    assert len(people) == len(aliases) == 1


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "2026-01-01T00:00Z",
        datetime(2026, 1, 1),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_bad_selected_dates_fail_with_a_safe_typed_error(bad):
    with pytest.raises(IdentityError, match="^invalid_identity_time$"):
        resolve_identity(WORLD, "Lili", [item()], [], at=bad, complete=True)


@pytest.mark.parametrize(
    "bounds",
    [
        dict(valid_from="private-date"),
        dict(valid_to=datetime(2026, 1, 1)),
        dict(valid_from=NOW, valid_to=NOW),
    ],
)
def test_malformed_role_dates_are_typed_refusals(bounds):
    with pytest.raises(IdentityError, match="^invalid_identity_time$"):
        alias("private-phrase", **bounds)


@pytest.mark.parametrize("position", ["item", "alias"])
def test_wrong_world_fails_even_after_an_exact_id_match(position):
    people = [item()]
    aliases = []
    if position == "item":
        people.append(replace(item("two"), world_id=OTHER))
    else:
        aliases.append(replace(alias("private-phrase"), world_id=OTHER))
    with pytest.raises(IdentityError, match="^invalid_identity_world$"):
        resolve("one", people, aliases)


@pytest.mark.parametrize("complete", [False, None, 1])
def test_incomplete_input_is_an_error_not_absence(complete):
    with pytest.raises(IdentityError, match="^incomplete_identity_input$"):
        resolve_identity(WORLD, "Lili", [], [], at=NOW, complete=complete)


def test_invalid_world_and_duplicate_ids_are_typed_refusals():
    with pytest.raises(IdentityError, match="^invalid_identity_world$"):
        resolve_identity("private-world", "Lili", [], [], at=NOW, complete=True)
    with pytest.raises(IdentityError, match="^duplicate_identity_item$"):
        resolve("Lili", [item(), item()])


@pytest.mark.parametrize("query", [None, "", " ", "--", "\u200b"])
def test_empty_queries_never_match_everything(query):
    with pytest.raises(IdentityError):
        resolve(query)


def _fold_role(kind, start, end):
    fields, aliases = (), ()
    if kind == "alias":
        aliases = (alias("the treasurer", valid_from=start, valid_to=end),)
    else:
        fields = (IdentityField("role", "the treasurer", valid_from=start, valid_to=end),)
    return (item(fields=fields),), aliases


@pytest.mark.parametrize("kind", ["alias", "field"])
@pytest.mark.parametrize("end_minute", [15, 30])
def test_role_valid_bounds_across_dst_fold_use_instants(kind, end_minute):
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 1, end_minute, tzinfo=zone, fold=1)
    assert start.astimezone(UTC) < end.astimezone(UTC)
    people, aliases = _fold_role(kind, start, end)
    result = resolve_identity(
        WORLD,
        "the treasurer",
        people,
        aliases,
        at=datetime(2026, 11, 1, 6, tzinfo=UTC),
        complete=True,
    )
    assert result.answer == "found"
    assert [candidate.item_id for candidate in result.candidates] == ["one"]


@pytest.mark.parametrize("kind", ["alias", "field"])
def test_role_reversed_instants_across_dst_fold_refuse(kind):
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    end = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    assert start.astimezone(UTC) > end.astimezone(UTC)
    with pytest.raises(IdentityError, match="^invalid_identity_time$"):
        _fold_role(kind, start, end)


@pytest.mark.parametrize("kind", ["alias", "field"])
@pytest.mark.parametrize(
    "hour,minute,fold,expected",
    [
        (1, 29, 0, "unmatched"),
        (1, 30, 0, "found"),
        (1, 45, 0, "found"),
        (1, 0, 1, "found"),
        (1, 14, 1, "found"),
        (1, 15, 1, "unmatched"),
    ],
)
def test_role_dst_fold_retains_half_open_boundaries(kind, hour, minute, fold, expected):
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    end = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    people, aliases = _fold_role(kind, start, end)
    selected = datetime(2026, 11, 1, hour, minute, tzinfo=zone, fold=fold)
    result = resolve_identity(WORLD, "the treasurer", people, aliases, at=selected, complete=True)
    assert result.answer == expected
    assert start.fold == 0 and end.fold == 1
    assert start.tzinfo is end.tzinfo is zone
