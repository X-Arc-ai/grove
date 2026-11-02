"""Loading the rename map and the denylist from the paths in GROVE_RENAME_MAP and GROVE_DENYLIST.

Unset is empty. Set means the file must exist, be private (no group or other bits) and parse, or
the load is a ConfigError: the screener never runs half-configured. No error message names a form,
a term, a label or the path.
"""

import json
import os

import pytest

from grove.config import (
    DENYLIST_VAR,
    RENAME_MAP_VAR,
    ConfigError,
    ScreeningSettings,
    load_screening_settings,
    load_settings,
)
from grove.screening import ScreeningConfig
from grove.screening.names import Normalised, parse_denylist, parse_rename_map
from tests.screening._plants import private_file

PLANTED = ("Orla", "Vance", "Brightwell", "Co-founder", "Halvard", "Quist")
# Every test folder gets this marker, so a message that names the path is caught.
FOLDER = "pathmarker-q7"


@pytest.fixture
def folder(tmp_path):
    made = tmp_path / FOLDER
    made.mkdir(mode=0o700)
    return made


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(RENAME_MAP_VAR, raising=False)
    monkeypatch.delenv(DENYLIST_VAR, raising=False)
    return monkeypatch


def assert_clean(error: BaseException, variable: str) -> None:
    message = str(error)
    assert variable in message
    for planted in (*PLANTED, FOLDER):
        assert planted.lower() not in message.lower(), planted
        assert planted.lower() not in repr(error).lower(), planted
    # No OS or JSON error rides along: its text holds the path or the whole file.
    assert error.__context__ is None and error.__cause__ is None


def load(clean_env, rename=None, denylist=None) -> ScreeningConfig:
    if rename is not None:
        clean_env.setenv(RENAME_MAP_VAR, str(rename))
    if denylist is not None:
        clean_env.setenv(DENYLIST_VAR, str(denylist))
    return ScreeningConfig.from_environment()


def test_unset_variables_give_empty_lists(clean_env):
    config = load(clean_env)
    assert config.rename.form_count == 0
    assert config.denylist.term_count == 0
    assert load_screening_settings() == ScreeningSettings()


def test_private_files_load_with_their_counts(clean_env, folder):
    rename = private_file(
        folder,
        "rename.json",
        json.dumps({"Orla Vance": "Co-founder", "Orla": "Co-founder", "Tamsin": "Designer"}),
    )
    denylist = private_file(folder, "denylist.txt", "Brightwell Logistics\nHalvard Quist\n")
    config = load(clean_env, rename, denylist)
    assert (config.rename.form_count, config.rename.label_count) == (3, 2)
    assert config.denylist.term_count == 2


def test_own_identifiers_and_sensitive_classes_pass_through(clean_env):
    config = ScreeningConfig.from_environment(
        own_identifiers=["me@grove-dev.example"], sensitive_classes=["pay_amount"]
    )
    assert config.own_identifiers == {"me@grove-dev.example"}
    assert config.sensitive_classes == {"pay_amount"}


def test_an_unknown_sensitive_class_is_refused_without_its_value():
    with pytest.raises(ValueError) as caught:
        ScreeningConfig(sensitive_classes=frozenset({"orla_health"}))
    assert "orla_health" not in str(caught.value)


@pytest.mark.parametrize("variable", [RENAME_MAP_VAR, DENYLIST_VAR], ids=["rename-map", "denylist"])
@pytest.mark.parametrize(
    "case, expected",
    [
        ("missing", "does not exist"),
        ("empty-value", "set but empty"),
        ("unknown-user", "cannot be expanded"),
        ("group-readable", "group or other"),
        ("other-readable", "group or other"),
        ("group-writable", "group or other"),
        ("directory", "not a regular file"),
        ("fifo", "not a regular file"),
        ("unreadable", "cannot be read"),
    ],
)
def test_a_set_variable_whose_file_is_not_private_and_readable_fails_closed(
    clean_env, folder, variable, case, expected
):
    content = '{"Orla": "Co-founder"}' if variable == RENAME_MAP_VAR else "Orla Vance\n"
    path = folder / "list"
    if case == "missing":
        value = str(folder / "absent-Orla.json")
    elif case == "empty-value":
        value = "  "
    elif case == "unknown-user":
        value = f"~{FOLDER}/list"
    elif case == "directory":
        path.mkdir(mode=0o700)
        value = str(path)
    elif case == "fifo":
        os.mkfifo(path, 0o600)
        value = str(path)
    else:
        mode = {
            "group-readable": 0o640,
            "other-readable": 0o604,
            "group-writable": 0o620,
            "unreadable": 0o000,
        }[case]
        value = str(private_file(folder, "list", content, mode))
    clean_env.setenv(variable, value)

    with pytest.raises(ConfigError) as caught:
        ScreeningConfig.from_environment()
    assert expected in str(caught.value)
    assert_clean(caught.value, variable)


def test_a_tilde_in_a_path_reads_the_home_folder(clean_env, folder):
    clean_env.setenv("HOME", str(folder))
    private_file(folder, "rename.json", '{"Orla": "Co-founder"}')
    assert load(clean_env, rename="~/rename.json").rename.form_count == 1


RENAME_FAULTS = [
    pytest.param('{"Orla Vance": "Co-founder",', "line 1 column 29", id="truncated"),
    pytest.param(
        '{\n  "Orla": "Co-founder"\n  "Vance": "Co-founder"\n}', "line 3", id="missing-comma"
    ),
    pytest.param("", "line 1 column 1", id="empty-file"),
    pytest.param('["Orla Vance", "Co-founder"]', "one JSON object", id="an-array"),
    pytest.param('"Orla Vance"', "one JSON object", id="a-string"),
    pytest.param("null", "one JSON object", id="null"),
    pytest.param('{"": "Co-founder"}', "the form of entry 1 is empty", id="empty-key"),
    pytest.param(
        '{"Orla": "x", "   ": "Co-founder"}', "the form of entry 2 is empty", id="blank-key"
    ),
    pytest.param('{"--": "Co-founder"}', "entry 1 holds no letter or digit", id="key-without-word"),
    pytest.param('{"Orla": "   "}', "the label of entry 1 is empty", id="blank-label"),
    pytest.param(
        '{"Orla": "--"}', "the label of entry 1 holds no letter or digit", id="label-dashes"
    ),
    pytest.param('{"Orla": 3}', "entry 1 must map a string to a string", id="number-label"),
    pytest.param(
        '{"Orla": {"Vance": "x"}}', "entry 1 must map a string to a string", id="object-label"
    ),
    pytest.param(
        '{"Orla Vance": "Co-founder", "ORLA  VANCE": "Client"}',
        "entries 1 and 2 give one form two labels",
        id="case-and-space-variant-with-another-label",
    ),
    pytest.param(
        '{"Orla": "Co-founder", "Tamsin": "Designer", "Ｏｒｌａ": "Client"}',
        "entries 1 and 3 give one form two labels",
        id="full-width-variant-with-another-label",
    ),
    pytest.param(
        '{"Orla": "Co-founder", "Orla": "Client"}',
        "entries 1 and 2 give one form two labels",
        id="exact-duplicate-with-another-label",
    ),
    pytest.param("[" * 100000, "not valid JSON", id="too-deep"),
]


@pytest.mark.parametrize("content, expected", RENAME_FAULTS)
def test_a_malformed_rename_map_names_the_position_never_the_content(
    clean_env, folder, content, expected
):
    path = private_file(folder, "rename.json", content)
    with pytest.raises(ConfigError) as caught:
        load(clean_env, rename=path)
    assert expected in str(caught.value)
    assert_clean(caught.value, RENAME_MAP_VAR)


def test_duplicate_forms_with_one_label_fold_into_one_form():
    folded = parse_rename_map(
        '{"Orla Vance": "Co-founder", "orla vance": "Co-founder", '
        '"ORLA\\u00a0VANCE": "Co-founder"}',
        RENAME_MAP_VAR,
    )
    assert (folded.form_count, folded.label_count) == (1, 1)


DENYLIST_FAULTS = [
    pytest.param(
        b"Brightwell Logistics\nHalvard\n\xff Quist\n", "line 3 is not UTF-8", id="bad-byte"
    ),
    pytest.param(
        b"\xef\xbb\xbfBrightwell\n# client\n\xc3\n", "line 3 is not UTF-8", id="bad-byte-after-bom"
    ),
    pytest.param(b"Brightwell Logistics\n---\n", "line 2 holds no letter or digit", id="no-word"),
]


@pytest.mark.parametrize("content, expected", DENYLIST_FAULTS)
def test_a_malformed_denylist_names_the_line_never_the_content(
    clean_env, folder, content, expected
):
    path = private_file(folder, "denylist.txt", content)
    with pytest.raises(ConfigError) as caught:
        load(clean_env, denylist=path)
    assert expected in str(caught.value)
    assert_clean(caught.value, DENYLIST_VAR)


def test_one_bad_file_fails_the_whole_load(clean_env, folder):
    rename = private_file(folder, "rename.json", '{"Orla": "Co-founder"}')
    denylist = private_file(folder, "denylist.txt", "Brightwell\n", mode=0o644)
    with pytest.raises(ConfigError) as caught:
        load(clean_env, rename, denylist)
    assert_clean(caught.value, DENYLIST_VAR)


def test_denylist_comments_blank_lines_and_duplicates():
    terms = parse_denylist(
        "# clients and outside names\n"
        "\n"
        "   Brightwell Logistics   # the client, a trailing comment\n"
        "Halvard Quist#no space before the comment\n"
        "BRIGHTWELL   LOGISTICS\n"
        "   \t\n"
        "#Tamsin Reyes is commented out\n",
        DENYLIST_VAR,
    )
    assert terms.term_count == 2


@pytest.mark.parametrize("line_end", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_every_line_end_splits_the_denylist_and_the_line_numbers_hold(line_end):
    terms = parse_denylist(line_end.join(["Brightwell", "Halvard", ""]), DENYLIST_VAR)
    assert terms.term_count == 2
    assert terms.holds(Normalised("Brightwell sent it", fold_case=True))
    with pytest.raises(ConfigError, match=r"line 3 holds no letter or digit"):
        parse_denylist(line_end.join(["Brightwell", "# a comment", "!!!", ""]), DENYLIST_VAR)


def test_a_byte_order_mark_does_not_glue_to_the_first_term(clean_env, folder):
    path = private_file(folder, "denylist.txt", b"\xef\xbb\xbfBrightwell Logistics\n")
    config = load(clean_env, denylist=path)
    assert config.denylist.holds(Normalised("Brightwell Logistics sent it", fold_case=True))


def test_a_rename_map_with_a_byte_order_mark_loads(clean_env, folder):
    content = b"\xef\xbb\xbf" + json.dumps({"Orla": "Co-founder"}).encode()
    config = load(clean_env, rename=private_file(folder, "rename.json", content))
    assert config.rename.form_count == 1


def test_load_settings_keeps_its_behaviour_when_a_screening_path_is_bad(clean_env, folder):
    """The store settings never read the screening files, so a bad one cannot stop the store."""
    clean_env.setenv("NEO4J_AUTH", "none")
    clean_env.setenv(RENAME_MAP_VAR, str(folder / "absent.json"))
    clean_env.setenv(DENYLIST_VAR, "")
    assert load_settings().neo4j_auth is None


def test_screening_settings_have_no_path_in_their_repr(clean_env, folder):
    clean_env.setenv(RENAME_MAP_VAR, str(folder / "rename-Orla.json"))
    settings = load_screening_settings()
    assert settings.rename_map == folder / "rename-Orla.json"
    assert FOLDER not in repr(settings) and "Orla" not in repr(settings)
