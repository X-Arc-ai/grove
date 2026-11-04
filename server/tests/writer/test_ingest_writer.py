"""The writer's ingest rows: the connector row, one chat's delta, the read outcome, and upload-only
tokens. Invented content only (R3)."""

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from grove import clock
from grove.clock import TestClock
from grove.ids import new_id
from grove.ingest.tokens import find_upload_grant
from grove.screening import Message, Screener, ScreeningConfig
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer.errors import InvalidRecord
from grove.writer.ingest import (
    DeltaMoved,
    EpisodeCut,
    ensure_connector,
    record_read,
    write_chat_delta,
)
from grove.writer.testing import seed_test_world
from grove.writer.upload_tokens import (
    MAX_MINUTES,
    TOKEN_PREFIX,
    UnknownWorld,
    mint_upload_token,
    revoke_upload_token,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def test_clock():
    installed = TestClock()
    installed.set(datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
    with clock.using(installed):
        yield installed


@pytest.fixture
def seeded(driver, make_world):
    world_id = make_world()
    owner = new_id()
    seed_test_world(driver, world_id, owner_user_id=owner)
    return world_id, owner


def _screened(keys: list[str], *, denylist=()):
    from grove.screening import Denylist

    screener = Screener(ScreeningConfig(denylist=Denylist(list(denylist))))
    messages = [
        Message(key=key, sender=None, text=f"line {n}", at=T0 + timedelta(minutes=n))
        for n, key in enumerate(keys)
    ]
    return screener.screen_source("zw-locator", messages)


def _cut(first: str, last: str) -> EpisodeCut:
    return EpisodeCut(first, last, T0, T0 + timedelta(minutes=5), "session_delta.v1")


def test_ensure_connector_makes_one_row_per_type_and_provider(driver, seeded, test_clock):
    world_id, _ = seeded
    first = ensure_connector(
        driver, world_id, "past_ai_conversations", "claude_code", batch_rule="session_delta.v1"
    )
    again = ensure_connector(
        driver, world_id, "past_ai_conversations", "claude_code", batch_rule="session_delta.v1"
    )
    other = ensure_connector(
        driver, world_id, "past_ai_conversations", "codex", batch_rule="session_delta.v1"
    )
    assert first == again and first != other


def test_two_first_uploads_that_race_make_one_connector(driver, seeded, test_clock):
    world_id, _ = seeded
    ids, errors = [], []

    def go():
        try:
            ids.append(
                ensure_connector(
                    driver, world_id, "past_ai_conversations", "chatgpt", batch_rule="b.v1"
                )
            )
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(set(ids)) == 1
    rows = WorldQuery(driver, world_id).read("MATCH (c:Connector) RETURN count(c) AS n")
    assert rows == [{"n": 1}]


def test_ensure_connector_refuses_free_text(driver, seeded, test_clock):
    world_id, _ = seeded
    with pytest.raises(InvalidRecord):
        ensure_connector(driver, world_id, "past_ai_conversations", "Orla's chats", batch_rule="b")
    with pytest.raises(InvalidRecord):
        ensure_connector(driver, world_id, "no_such_type", "x", batch_rule="b")


def test_a_delta_commits_whole_and_a_second_copy_is_refused(driver, seeded, test_clock):
    world_id, _ = seeded
    connector = ensure_connector(
        driver, world_id, "past_ai_conversations", "zz_w", batch_rule="session_delta.v1"
    )
    screened = _screened(["zw-a", "zw-b"])
    written = write_chat_delta(
        driver,
        world_id,
        screened,
        connector_id=connector,
        parser_version="zz.v1",
        delta_keys=["zw-a", "zw-b"],
        episodes=[_cut("zw-a", "zw-b")],
        newest_at=T0 + timedelta(minutes=1),
    )
    assert written["kept_messages"] == 2 and len(written["episode_ids"]) == 1
    with pytest.raises(DeltaMoved):
        write_chat_delta(
            driver,
            world_id,
            _screened(["zw-b", "zw-c"]),
            connector_id=connector,
            parser_version="zz.v1",
            delta_keys=["zw-b", "zw-c"],
            episodes=[_cut("zw-b", "zw-c")],
            newest_at=None,
        )
    query = WorldQuery(driver, world_id)
    assert query.read("MATCH (s:Source) RETURN count(s) AS n") == [{"n": 1}]
    assert query.read("MATCH (e:Episode) RETURN count(e) AS n") == [{"n": 1}]
    row = query.read("MATCH (c:Connector) RETURN c")[0]["c"]
    assert row["read_count"] == 1
    assert row["watermark"] == "at:" + (T0 + timedelta(minutes=1)).isoformat()


def test_a_delta_needs_matching_episodes(driver, seeded, test_clock):
    world_id, _ = seeded
    connector = ensure_connector(
        driver, world_id, "past_ai_conversations", "zz_w", batch_rule="session_delta.v1"
    )
    with pytest.raises(InvalidRecord):
        write_chat_delta(
            driver,
            world_id,
            _screened(["zw-a"]),
            connector_id=connector,
            parser_version="zz.v1",
            delta_keys=["zw-a"],
            episodes=[],
            newest_at=None,
        )
    with pytest.raises(InvalidRecord):
        write_chat_delta(
            driver,
            world_id,
            _screened(["zw-a"]),
            connector_id=connector,
            parser_version="zz.v1",
            delta_keys=["zw-a"],
            episodes=[EpisodeCut("zw-a", "zw-a", T0, T0 - timedelta(1), "session_delta.v1")],
            newest_at=None,
        )
    with pytest.raises(TypeError):
        write_chat_delta(
            driver,
            world_id,
            object(),
            connector_id=connector,
            parser_version="zz.v1",
            delta_keys=["zw-a"],
            episodes=[],
            newest_at=None,
        )
    with pytest.raises(InvalidRecord):
        write_chat_delta(
            driver,
            world_id,
            _screened(["zw-a"]),
            connector_id=new_id(world_id),
            parser_version="zz.v1",
            delta_keys=["zw-a"],
            episodes=[_cut("zw-a", "zw-a")],
            newest_at=None,
        )
    assert WorldQuery(driver, world_id).read("MATCH (s:Source) RETURN count(s) AS n") == [{"n": 0}]


def test_an_empty_delta_writes_its_empty_source_only_once(driver, seeded, test_clock):
    world_id, _ = seeded
    connector = ensure_connector(
        driver, world_id, "past_ai_conversations", "zz_w", batch_rule="session_delta.v1"
    )
    for _ in range(2):
        write_chat_delta(
            driver,
            world_id,
            _screened(["zw-a"], denylist=["line"]),
            connector_id=connector,
            parser_version="zz.v1",
            delta_keys=["zw-a"],
            episodes=[],
            newest_at=None,
        )
    rows = WorldQuery(driver, world_id).read(
        "MATCH (s:Source) RETURN s.content AS content, s.id AS id"
    )
    assert len(rows) == 1 and rows[0]["content"] == "[]"
    receipt = WorldQuery(driver, world_id).read(
        "MATCH (r:ScreeningReceipt) RETURN r.counts_per_class AS counts"
    )
    assert receipt == [{"counts": json.dumps({"denylisted": 1})}]


def test_record_read_moves_coverage(driver, seeded, test_clock):
    world_id, _ = seeded
    connector = ensure_connector(driver, world_id, "past_ai_conversations", "zz_w", batch_rule="b")
    record_read(driver, world_id, connector, outcome="took_in")
    test_clock.advance(minutes=10)
    record_read(driver, world_id, connector, outcome="failed", failed=2)
    row = WorldQuery(driver, world_id).read("MATCH (c:Connector) RETURN c")[0]["c"]
    assert row["last_outcome"] == "failed"
    assert row["failed_count"] == 2
    assert row["last_attempted_read"] == test_clock.now()
    assert row["last_successful_read"] == test_clock.now() - timedelta(minutes=10)
    with pytest.raises(InvalidRecord):
        record_read(driver, world_id, connector, outcome="great")
    with pytest.raises(InvalidRecord):
        record_read(driver, world_id, connector, outcome="took_in", failed=-1)


# Upload-only tokens ------------------------------------------------------------------------


def _tokens(driver, world_id) -> list[dict]:
    return PlatformQuery(driver).read(
        "MATCH (t:UploadToken {world_id: $world_id}) RETURN t", world_id=world_id
    )


def test_a_token_is_stored_as_its_hash_only(driver, seeded, test_clock):
    world_id, owner = seeded
    minted = mint_upload_token(driver, world_id, minutes=30)
    assert minted.token.startswith(TOKEN_PREFIX)
    assert minted.token not in repr(minted)
    (row,) = (entry["t"] for entry in _tokens(driver, world_id))
    assert row["token_hash"] == hashlib.sha256(minted.token.encode()).hexdigest()
    assert minted.token not in json.dumps(row, default=str)
    assert row["user_id"] == owner and row["world_id"] == world_id
    assert row["expires_at"] == test_clock.now() + timedelta(minutes=30)


def test_a_token_grants_its_world_until_it_expires_or_is_revoked(driver, seeded, test_clock):
    world_id, owner = seeded
    minted = mint_upload_token(driver, world_id, minutes=5)
    platform = PlatformQuery(driver)
    grant = find_upload_grant(platform, minted.token, test_clock.now())
    assert (grant.world_id, grant.user_id, grant.token_id) == (world_id, owner, minted.id)
    assert (
        find_upload_grant(platform, minted.token, test_clock.now() + timedelta(minutes=5)) is None
    )
    assert find_upload_grant(platform, minted.token[:-1] + "x", test_clock.now()) is None
    assert (
        find_upload_grant(platform, minted.token.removeprefix(TOKEN_PREFIX), test_clock.now())
        is None
    )
    assert find_upload_grant(platform, None, test_clock.now()) is None
    assert revoke_upload_token(driver, minted.id) is True
    first = _tokens(driver, world_id)[0]["t"]["revoked_at"]
    test_clock.advance(minutes=1)
    assert revoke_upload_token(driver, minted.id) is True
    assert _tokens(driver, world_id)[0]["t"]["revoked_at"] == first
    assert find_upload_grant(platform, minted.token, test_clock.now()) is None
    assert revoke_upload_token(driver, new_id()) is False


def test_mint_refuses_an_unknown_world_and_a_bad_life(driver, seeded, test_clock):
    world_id, _ = seeded
    with pytest.raises(UnknownWorld):
        mint_upload_token(driver, f"t_ingest-a1_{new_id()[:8]}-0000-4000-8000-000000000000")
    for minutes in (0, MAX_MINUTES + 1, True, 1.5):
        with pytest.raises(ValueError):
            mint_upload_token(driver, world_id, minutes=minutes)
    assert _tokens(driver, world_id) == []


def test_a_token_stops_granting_when_its_world_changes_owner(driver, seeded, test_clock):
    """The token names the owner at mint time. A world handed to another user must not take
    uploads on the old owner's token."""
    world_id, owner = seeded
    minted = mint_upload_token(driver, world_id, minutes=30)
    platform = PlatformQuery(driver)
    assert find_upload_grant(platform, minted.token, test_clock.now()) is not None
    with driver.session() as session:
        session.run(
            "MATCH (w:World {id: $world}) SET w.owner_user_id = $other",
            world=world_id,
            other="u_other_owner",
        ).consume()
    try:
        assert find_upload_grant(platform, minted.token, test_clock.now()) is None
    finally:
        with driver.session() as session:
            session.run(
                "MATCH (w:World {id: $world}) SET w.owner_user_id = $owner",
                world=world_id,
                owner=owner,
            ).consume()
