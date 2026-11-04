"""`python -m grove.ingest token mint|revoke|list`, run in process with the test driver.

The raw token appears once, on stdout, at mint; list never shows a token or its hash; a refused
argument exits 2 with one line on stderr that holds no value. Every id here is invented (R3).
"""

import io
import json
from datetime import timedelta

import pytest

from grove.ingest.__main__ import main
from grove.store.query import PlatformQuery
from grove.writer.upload_tokens import token_hash
from tests.ingest.conftest import STORE_TIME, counts
from tests.ingest.test_upload_api import (
    bearer,
    clean_env,  # noqa: F401  Autouse: keeps box settings out.
    upload,
)

MINUTES_RULE = "python -m grove.ingest: --minutes must be a whole number from 1 to 1440\n"


def run(driver, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), driver=driver, out=out)
    return code, out.getvalue()


def lines_of(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines()]


def mint(driver, world_id: str, *extra: str) -> dict:
    code, text = run(driver, "token", "mint", "--world", world_id, *extra)
    assert code == 0
    [line] = lines_of(text)
    return line


def token_rows(driver, world_id: str) -> int:
    return PlatformQuery(driver).read(
        "MATCH (t:UploadToken {world_id: $world}) RETURN count(t) AS n", world=world_id
    )[0]["n"]


def assert_refused(code: int, out: str, capsys, *hidden: str) -> str:
    """Exit 2, nothing on stdout, and one line on stderr that echoes none of `hidden`."""
    assert code == 2
    assert out == ""
    err = capsys.readouterr().err
    assert err.endswith("\n") and err.count("\n") == 1
    assert err.startswith("python -m grove.ingest: ")
    for value in hidden:
        assert value not in err
    return err


def test_mint_prints_the_token_once_and_it_uploads(
    driver, seeded_world, anonymous, test_clock, capsys
):
    code, text = run(driver, "token", "mint", "--world", seeded_world)
    assert code == 0
    [line] = lines_of(text)
    assert set(line) == {"id", "token", "world_id", "expires_at"}
    assert line["token"].startswith("gut_")
    assert line["world_id"] == seeded_world
    assert line["expires_at"] == (STORE_TIME + timedelta(minutes=60)).isoformat()
    assert text.count(line["token"]) == 1
    assert capsys.readouterr().err == ""
    response = upload(anonymous, headers=bearer(line["token"]))
    assert response.status_code == 200
    assert response.json()["state"] == "taken_in"
    assert counts(driver, seeded_world)["sources"] == 2


def test_mint_takes_the_minutes(driver, seeded_world, anonymous, test_clock):
    line = mint(driver, seeded_world, "--minutes", "1440")
    assert line["expires_at"] == (STORE_TIME + timedelta(minutes=1440)).isoformat()
    test_clock.advance(minutes=1439)
    assert upload(anonymous, headers=bearer(line["token"])).status_code == 200
    test_clock.advance(minutes=1)
    assert upload(anonymous, headers=bearer(line["token"])).status_code == 401


def test_list_shows_ids_and_times_but_never_a_token_or_a_hash(driver, seeded_world, test_clock):
    first = mint(driver, seeded_world)
    test_clock.advance(minutes=1)
    second = mint(driver, seeded_world, "--minutes", "5")
    assert run(driver, "token", "revoke", "--id", first["id"])[0] == 0
    code, text = run(driver, "token", "list", "--world", seeded_world)
    assert code == 0
    for minted in (first, second):
        assert minted["token"] not in text
        assert token_hash(minted["token"]) not in text
        assert minted["token"].removeprefix("gut_") not in text
    rows = lines_of(text)
    assert [set(row) for row in rows] == [{"id", "created_at", "expires_at", "revoked_at"}] * 2
    assert rows == [
        {
            "id": first["id"],
            "created_at": STORE_TIME.isoformat(),
            "expires_at": first["expires_at"],
            "revoked_at": (STORE_TIME + timedelta(minutes=1)).isoformat(),
        },
        {
            "id": second["id"],
            "created_at": (STORE_TIME + timedelta(minutes=1)).isoformat(),
            "expires_at": second["expires_at"],
            "revoked_at": None,
        },
    ]


def test_list_of_a_world_with_no_tokens_prints_nothing(driver, seeded_world, test_clock):
    assert run(driver, "token", "list", "--world", seeded_world) == (0, "")


def test_revoke_makes_the_token_401(driver, seeded_world, anonymous, test_clock):
    line = mint(driver, seeded_world)
    code, text = run(driver, "token", "revoke", "--id", line["id"])
    assert code == 0
    assert lines_of(text) == [{"id": line["id"], "revoked": True}]
    assert line["token"] not in text
    assert upload(anonymous, headers=bearer(line["token"])).status_code == 401
    assert counts(driver, seeded_world)["sources"] == 0


def test_revoke_of_an_unknown_id_says_false(driver, seeded_world, test_clock):
    code, text = run(driver, "token", "revoke", "--id", "no-such-token-id")
    assert code == 0
    assert lines_of(text) == [{"id": "no-such-token-id", "revoked": False}]


def test_revoke_refuses_the_raw_token_and_never_echoes_it(
    driver, seeded_world, anonymous, test_clock, capsys
):
    line = mint(driver, seeded_world)
    capsys.readouterr()
    code, out = run(driver, "token", "revoke", "--id", line["token"])
    assert_refused(code, out, capsys, line["token"], line["token"].removeprefix("gut_"))
    assert upload(anonymous, headers=bearer(line["token"])).status_code == 200


def test_mint_for_an_unknown_world_exits_2(driver, make_world, test_clock, capsys):
    unknown = make_world()
    code, out = run(driver, "token", "mint", "--world", unknown)
    assert_refused(code, out, capsys, unknown)
    assert token_rows(driver, unknown) == 0


def test_list_for_an_unknown_world_exits_2(driver, make_world, test_clock, capsys):
    unknown = make_world()
    code, out = run(driver, "token", "list", "--world", unknown)
    assert_refused(code, out, capsys, unknown)


@pytest.mark.parametrize("minutes", ["0", "1441", "-5", "ten", "1.5", ""])
def test_minutes_out_of_range_exit_2(driver, seeded_world, test_clock, capsys, minutes):
    code, out = run(driver, "token", "mint", "--world", seeded_world, "--minutes", minutes)
    assert assert_refused(code, out, capsys, seeded_world) == MINUTES_RULE
    assert token_rows(driver, seeded_world) == 0


def test_a_world_id_of_the_wrong_form_exits_2(driver, test_clock, capsys):
    code, out = run(driver, "token", "mint", "--world", "Orchard-Zz-Unknown")
    assert_refused(code, out, capsys, "Orchard-Zz-Unknown")


@pytest.mark.parametrize(
    "argv",
    [
        ("token",),
        ("token", "mint"),
        ("token", "burn", "--world", "zz-pasted-value"),
        ("token", "list", "--world", "x", "--extra", "zz-pasted-value"),
        ("stats",),
    ],
    ids=["no-action", "no-world", "unknown-action", "unknown-flag", "unknown-group"],
)
def test_bad_arguments_exit_2_without_echoing(driver, capsys, argv):
    code, out = run(driver, *argv)
    assert_refused(code, out, capsys, "zz-pasted-value", "burn")


def test_missing_store_settings_exit_2(seeded_world, monkeypatch, capsys):
    """With no driver handed in, the command opens one from the environment."""
    monkeypatch.delenv("NEO4J_AUTH", raising=False)
    out = io.StringIO()
    code = main(["token", "list", "--world", seeded_world], out=out)
    err = assert_refused(code, out.getvalue(), capsys, seeded_world)
    assert "NEO4J_AUTH" in err
