"""The OAuth writer and its read helpers against the real store (S41).

Codes are single use, a reuse revokes the family, a refresh rotates inside its family and grant,
a grant's revoke takes its tokens with it, a token carries its grant's world, and two redeems that
race give one winner. A lock on a row a racer deletes counts as no row, and a family revoke waits
for a rotation in flight. Names are invented; the worlds and clients are purged at the end.

The store is shared with other runs, and any run's registration cleans up the whole store. So a
client that is old and has no grant may go at any moment: a test ages a client only after its
consent (age_granted_client), and waits through more than one cleanup (_clean_until_gone).
"""

import hashlib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Barrier

import pytest

from grove import clock
from grove.ids import new_id
from grove.oauth import (
    ACCESS_TOKEN_LIFETIME,
    CODE_LIFETIME,
    METADATA_DOCUMENT_STALE_LIMIT,
    REFRESH_TOKEN_LIFETIME,
    UNUSED_DYNAMIC_CLIENT_KEPT,
)
from grove.oauth.secrets import (
    ACCESS_TOKEN_PREFIX,
    CODE_PREFIX,
    REFRESH_TOKEN_PREFIX,
    new_secret,
    token_hash,
)
from grove.oauth.store import find_access_token, find_client, list_grants
from grove.store.query import PlatformQuery
from grove.writer import oauth as oauth_module
from grove.writer.oauth import (
    DYNAMIC,
    METADATA_DOCUMENT,
    GrantRevoked,
    IssuedFamily,
    RedeemedCode,
    ScopeNotGranted,
    UnknownClient,
    delete_unused_client,
    grant_consent,
    issue_tokens,
    redeem_code,
    register_client,
    revoke_grant,
    revoke_token,
    rotate_refresh,
    store_code,
)
from grove.writer.testing import (
    DROP_GRANT,
    DROP_TOKEN,
    HeldTransaction,
    age_granted_client,
    held_transaction,
    plant_session,
)
from tests.hosts.conftest import (
    CLIENT_NAME,
    LOOPBACK_REDIRECT,
    SECOND_TITLE,
    HostsPlant,
    Person,
)

RESOURCE = "https://grove.example/mcp"
READ_WRITE = ["grove.read", "grove.write"]
CHALLENGE = "Zm9yLXRlc3RzLW9ubHktYS1jaGFsbGVuZ2Utb2YtNDMtY2hhcnM"
DOCUMENT_URL = "https://lumen-desk.example/oauth/client.json"
# How many registrations a test may wait through for one client to go.
CLEANUP_CALLS = 10


@dataclass(frozen=True)
class Chain:
    """One consent, one redeemed code and its first pair of tokens."""

    client_id: str
    world_id: str
    user_id: str
    grant_id: str
    code_id: str
    access: str
    refresh: str
    code: str


def _consent(driver, person, client_id: str, at: datetime, scopes=READ_WRITE) -> str:
    return grant_consent(
        driver,
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        scopes=scopes,
        at=at,
    )


def _code(driver, person, client_id: str, grant_id: str, at: datetime, **options) -> str:
    """Stores a fresh code and returns the code itself."""
    code = new_secret(CODE_PREFIX)
    store_code(
        driver,
        code_hash=token_hash(code),
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        grant_id=grant_id,
        code_challenge=CHALLENGE,
        redirect_uri=LOOPBACK_REDIRECT,
        redirect_given=options.get("redirect_given", True),
        scopes=options.get("scopes", READ_WRITE),
        resource=options.get("resource", RESOURCE),
        at=at,
        expires_at=options.get("expires_at", at + CODE_LIFETIME),
    )
    return code


def _issue(driver, redeemed: RedeemedCode, at: datetime, **overrides) -> tuple[str, str]:
    access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret(REFRESH_TOKEN_PREFIX)
    fields = {
        "client_id": redeemed.client_id,
        "user_id": redeemed.user_id,
        "world_id": redeemed.world_id,
        "grant_id": redeemed.grant_id,
        "family_id": redeemed.id,
        "scopes": list(redeemed.scopes),
        "resource": redeemed.resource,
    }
    issue_tokens(
        driver,
        access_hash=token_hash(access),
        refresh_hash=token_hash(refresh),
        at=at,
        access_expires_at=at + ACCESS_TOKEN_LIFETIME,
        refresh_expires_at=at + REFRESH_TOKEN_LIFETIME,
        **(fields | overrides),
    )
    return access, refresh


def _chain(driver, person, client_id: str, at: datetime | None = None) -> Chain:
    at = at or clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)
    assert redeemed is not None
    access, refresh = _issue(driver, redeemed, at)
    return Chain(
        client_id, person.world_id, person.user_id, grant_id, redeemed.id, access, refresh, code
    )


def _rotate(driver, chain_client: str, refresh: str, at: datetime, scopes=None):
    access, new_refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret(REFRESH_TOKEN_PREFIX)
    issued = rotate_refresh(
        driver,
        refresh_hash=token_hash(refresh),
        client_id=chain_client,
        new_access_hash=token_hash(access),
        new_refresh_hash=token_hash(new_refresh),
        scopes=scopes,
        at=at,
        access_expires_at=at + ACCESS_TOKEN_LIFETIME,
        refresh_expires_at=at + REFRESH_TOKEN_LIFETIME,
    )
    return issued, access, new_refresh


def _token(driver, value: str) -> dict | None:
    rows = PlatformQuery(driver).read(
        "MATCH (t:OAuthToken {token_hash: $h}) RETURN t {.*} AS row", h=token_hash(value)
    )
    return rows[0]["row"] if rows else None


def _family(driver, family_id: str) -> list[dict]:
    return [
        row["row"]
        for row in PlatformQuery(driver).read(
            "MATCH (t:OAuthToken {family_id: $f}) "
            "RETURN t {.*} AS row ORDER BY t.created_at, t.token_type",
            f=family_id,
        )
    ]


def _code_row(driver, code: str) -> dict | None:
    rows = PlatformQuery(driver).read(
        "MATCH (c:AuthCode {code_hash: $h}) RETURN c {.*} AS row", h=token_hash(code)
    )
    return rows[0]["row"] if rows else None


def _grant_row(driver, grant_id: str) -> dict:
    return PlatformQuery(driver).read(
        "MATCH (g:OAuthGrant {id: $id}) RETURN g {.*} AS row", id=grant_id
    )[0]["row"]


def _live(driver, value: str, now: datetime | None = None):
    return find_access_token(driver, token_hash(value), now or clock.now())


def _waits_on(driver, hold: Callable[[HeldTransaction], None], work: Callable[[], object]):
    """`hold` runs in an open transaction, `work` starts in a thread and must wait on that
    transaction's locks, the transaction commits, and `work` gives its answer."""
    with ThreadPoolExecutor(1) as pool, held_transaction(driver) as held:
        hold(held)
        job = pool.submit(work)
        assert held.blocks(job.done), "the work did not wait on the open transaction"
        held.commit()
        return job.result(timeout=60)


def _dynamic_client(driver, plant: HostsPlant, at: datetime) -> str:
    client_id = plant.track_client(new_id())
    register_client(
        driver,
        client_id=client_id,
        host=CLIENT_NAME,
        redirect_uris=[LOOPBACK_REDIRECT],
        registration_type=DYNAMIC,
        auth_method="none",
        secret_hash=None,
        at=at,
    )
    return client_id


def _document_client(
    driver, plant: HostsPlant, at: datetime, *, url: str | None = None, host: str = CLIENT_NAME
) -> str:
    url = url or plant.track_client(f"{DOCUMENT_URL}?run={new_id()}")
    register_client(
        driver,
        client_id=url,
        host=host,
        redirect_uris=["https://lumen-desk.example/callback"],
        registration_type=METADATA_DOCUMENT,
        auth_method="none",
        secret_hash=None,
        at=at,
    )
    return url


def _clean_until_gone(driver, plant: HostsPlant, gone: str, kept: Sequence[str] = ()) -> None:
    """Registrations, each with its cleanup, until `gone` is deleted, and never fewer than two.
    Another run's old clients may take a cleanup's budget first, so one call proves too little
    either way. Every client in `kept` must stay through all of them."""
    for call in range(CLEANUP_CALLS):
        plant.client()
        for client_id in kept:
            assert find_client(driver, client_id) is not None, f"a cleanup deleted {client_id}"
        if call >= 1 and find_client(driver, gone) is None:
            return
    pytest.fail(f"{CLEANUP_CALLS} cleanups left {gone} in the store")


# Secrets ----------------------------------------------------------------------------------------


def test_a_secret_has_its_prefix_and_the_store_keeps_only_its_hash(driver, hosts_plant):
    value = new_secret(ACCESS_TOKEN_PREFIX)
    assert value.startswith("gva_") and len(value) == 4 + 43
    assert new_secret(ACCESS_TOKEN_PREFIX) != value
    assert token_hash(value) == hashlib.sha256(value.encode()).hexdigest()
    person = hosts_plant.person(seeded=False)
    chain = _chain(driver, person, hosts_plant.client())
    for value in (chain.access, chain.refresh):
        row = _token(driver, value)
        assert row["token_hash"] == token_hash(value)
        assert value not in {str(v) for v in row.values()}


# Codes ------------------------------------------------------------------------------------------


def test_a_code_is_redeemed_once_and_carries_what_the_authorize_request_held(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at, redirect_given=False, resource=None)

    first = redeem_code(driver, code_hash=token_hash(code), at=at + timedelta(seconds=5))
    second = redeem_code(driver, code_hash=token_hash(code), at=at + timedelta(seconds=6))

    assert isinstance(first, RedeemedCode)
    assert second is None
    assert (first.client_id, first.user_id, first.world_id, first.grant_id) == (
        client_id,
        person.user_id,
        person.world_id,
        grant_id,
    )
    assert first.code_challenge == CHALLENGE
    assert first.redirect_uri == LOOPBACK_REDIRECT
    assert first.redirect_given is False
    assert first.scopes == tuple(READ_WRITE)
    assert first.resource is None
    row = _code_row(driver, code)
    assert row["id"] == first.id
    assert row["used_at"] == at + timedelta(seconds=5)
    assert row["grant_id"] == grant_id and "resource" not in row


def test_an_unknown_or_expired_code_gives_nothing_and_stays_unused(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)

    assert redeem_code(driver, code_hash=token_hash(new_secret(CODE_PREFIX)), at=at) is None
    assert redeem_code(driver, code_hash=token_hash(code), at=at + CODE_LIFETIME) is None
    assert "used_at" not in _code_row(driver, code)
    assert redeem_code(driver, code_hash=token_hash(code), at=at + CODE_LIFETIME / 2) is not None


def test_a_reused_code_revokes_every_token_of_its_family_and_no_other(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)
    access, refresh = _issue(driver, redeemed, at)
    other = _chain(driver, person, client_id, at)
    assert other.grant_id == grant_id and other.code_id != redeemed.id

    assert redeem_code(driver, code_hash=token_hash(code), at=at + timedelta(seconds=30)) is None
    assert redeem_code(driver, code_hash=token_hash(code), at=at + timedelta(seconds=35)) is None

    assert _code_row(driver, code)["reused_at"] == at + timedelta(seconds=30)
    family = _family(driver, redeemed.id)
    assert [row["token_type"] for row in family] == ["access", "refresh"]
    assert all(row["revoked_at"] == at + timedelta(seconds=30) for row in family)
    assert _live(driver, access) is None
    assert _rotate(driver, client_id, refresh, at + timedelta(seconds=40))[0] is None
    assert _live(driver, other.access) is not None
    assert _grant_row(driver, grant_id).get("revoked_at") is None


def test_store_code_drops_this_clients_codes_that_expired_over_a_day_ago(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    other_client = hosts_plant.client()
    now = clock.now()
    old_at = now - timedelta(days=2)
    grant_id = _consent(driver, person, client_id, old_at)
    other_grant = _consent(driver, person, other_client, old_at)
    old = _code(driver, person, client_id, grant_id, old_at)
    recent = _code(driver, person, client_id, grant_id, now - timedelta(hours=20))
    other_old = _code(driver, person, other_client, other_grant, old_at)

    _code(driver, person, client_id, grant_id, now)

    assert _code_row(driver, old) is None
    assert _code_row(driver, recent) is not None
    assert _code_row(driver, other_old) is not None


def test_two_concurrent_redeems_of_one_code_give_exactly_one_winner(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    for _ in range(5):
        code = _code(driver, person, client_id, grant_id, at)
        barrier = Barrier(2, timeout=30)

        def redeem(_, code=code, barrier=barrier):
            barrier.wait()
            return redeem_code(driver, code_hash=token_hash(code), at=at)

        with ThreadPoolExecutor(2) as pool:
            answers = list(pool.map(redeem, range(2)))

        winners = [answer for answer in answers if answer is not None]
        assert len(winners) == 1 and answers.count(None) == 1
        assert winners[0].grant_id == grant_id
        assert _code_row(driver, code)["used_at"] == at


def test_a_reuse_before_the_winners_issue_leaves_the_family_without_tokens(driver, hosts_plant):
    """The second redeem finds no token to revoke yet, so the issue that follows must refuse."""
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    winner = redeem_code(driver, code_hash=token_hash(code), at=at)
    assert winner is not None

    assert redeem_code(driver, code_hash=token_hash(code), at=at + timedelta(seconds=1)) is None
    with pytest.raises(GrantRevoked):
        _issue(driver, winner, at + timedelta(seconds=2))

    assert _family(driver, winner.id) == []
    assert _grant_row(driver, grant_id).get("revoked_at") is None


def test_issue_tokens_refuses_a_code_that_was_never_redeemed(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    unredeemed = RedeemedCode(
        id=_code_row(driver, code)["id"],
        client_id=client_id,
        user_id=person.user_id,
        world_id=person.world_id,
        grant_id=grant_id,
        code_challenge=CHALLENGE,
        redirect_uri=LOOPBACK_REDIRECT,
        redirect_given=True,
        scopes=tuple(READ_WRITE),
        resource=RESOURCE,
    )

    with pytest.raises(GrantRevoked):
        _issue(driver, unredeemed, at)
    with pytest.raises(GrantRevoked):
        _issue(driver, unredeemed, at, family_id=new_id())
    assert _family(driver, unredeemed.id) == []


def test_a_redeem_and_issue_that_races_a_second_redeem_leaves_no_live_token(driver, hosts_plant):
    """Each thread redeems and, when it wins, issues. Whichever order the store gives, the reuse
    revokes the pair or the issue refuses: the family ends with no live token."""
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    for _ in range(5):
        code = _code(driver, person, client_id, grant_id, at)
        barrier = Barrier(2, timeout=30)

        def redeem_and_issue(_, code=code, barrier=barrier):
            barrier.wait()
            redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)
            if redeemed is None:
                return None
            try:
                return _issue(driver, redeemed, at)
            except GrantRevoked:
                return redeemed

        with ThreadPoolExecutor(2) as pool:
            answers = list(pool.map(redeem_and_issue, range(2)))

        assert answers.count(None) == 1
        family_id = _code_row(driver, code)["id"]
        assert _code_row(driver, code)["reused_at"] == at
        assert all(row.get("revoked_at") is not None for row in _family(driver, family_id))
        issued = next(answer for answer in answers if answer is not None)
        if isinstance(issued, tuple):
            assert _live(driver, issued[0], at) is None


# Tokens -----------------------------------------------------------------------------------------


def test_issued_tokens_carry_the_grant_family_resource_and_scopes(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    chain = _chain(driver, person, hosts_plant.client())

    family = _family(driver, chain.code_id)
    assert [row["token_type"] for row in family] == ["access", "refresh"]
    for row in family:
        assert row["world_id"] == person.world_id
        assert row["grant_id"] == chain.grant_id
        assert row["client_id"] == chain.client_id
        assert row["user_id"] == person.user_id
        assert row["resource"] == RESOURCE
        assert row["scopes"] == READ_WRITE
        assert "revoked_at" not in row and "used_at" not in row
    live = _live(driver, chain.access)
    assert (live.world_id, live.grant_id, live.family_id, live.client_id, live.user_id) == (
        person.world_id,
        chain.grant_id,
        chain.code_id,
        chain.client_id,
        person.user_id,
    )
    assert live.scopes == tuple(READ_WRITE) and live.resource == RESOURCE


def test_a_token_row_carries_the_world_of_its_grant_and_no_other(driver, hosts_plant):
    first = hosts_plant.person(seeded=False)
    second = hosts_plant.person(SECOND_TITLE, seeded=False)
    client_id = hosts_plant.client()
    one = _chain(driver, first, client_id)
    two = _chain(driver, second, client_id)

    assert one.grant_id != two.grant_id
    assert _live(driver, one.access).world_id == first.world_id
    assert _live(driver, two.access).world_id == second.world_id
    assert {row["world_id"] for row in _family(driver, one.code_id)} == {first.world_id}

    at = clock.now()
    redeemed = redeem_code(
        driver,
        code_hash=token_hash(_code(driver, first, client_id, one.grant_id, at)),
        at=at,
    )
    with pytest.raises(GrantRevoked):
        _issue(driver, redeemed, at, world_id=second.world_id, user_id=second.user_id)
    with pytest.raises(GrantRevoked):
        _issue(driver, redeemed, at, grant_id=two.grant_id)
    with pytest.raises(GrantRevoked):
        _issue(driver, redeemed, at, client_id=hosts_plant.client())
    assert _family(driver, redeemed.id) == []


def test_issue_tokens_refuses_a_revoked_or_absent_grant_and_writes_nothing(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)

    with pytest.raises(GrantRevoked):
        _issue(driver, redeemed, at, grant_id=new_id())
    assert revoke_grant(driver, grant_id=grant_id, world_id=person.world_id, at=at) is True
    with pytest.raises(GrantRevoked):
        _issue(driver, redeemed, at)
    assert _family(driver, redeemed.id) == []


def test_find_access_token_refuses_expired_revoked_refresh_and_revoked_grant_tokens(
    driver, hosts_plant
):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)

    assert _live(driver, chain.access, at) is not None
    assert _live(driver, chain.access, at + ACCESS_TOKEN_LIFETIME - timedelta(seconds=1))
    assert _live(driver, chain.access, at + ACCESS_TOKEN_LIFETIME) is None
    assert _live(driver, chain.refresh, at) is None
    assert _live(driver, new_secret(ACCESS_TOKEN_PREFIX), at) is None

    revoked = _chain(driver, person, client_id, at)
    revoke_token(driver, token_hash=token_hash(revoked.access), client_id=client_id, at=at)
    assert _live(driver, revoked.access, at) is None

    other_client = hosts_plant.client()
    of_revoked_grant = _chain(driver, person, other_client, at)
    assert _live(driver, of_revoked_grant.access, at) is not None
    revoke_grant(driver, grant_id=of_revoked_grant.grant_id, world_id=person.world_id, at=at)
    assert _live(driver, of_revoked_grant.access, at) is None
    assert _live(driver, chain.access, at) is not None


def test_find_access_token_reads_the_grant_itself_not_only_the_token_row(
    driver, hosts_plant, monkeypatch
):
    """A second wall: a token that a revoke somehow missed still dies with its grant."""
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)
    monkeypatch.setattr(
        oauth_module,
        "_REVOKE_GRANT_TOKENS",
        "MATCH (t:OAuthToken {grant_id: $grant_id}) RETURN t.id AS id",
    )

    assert revoke_grant(driver, grant_id=chain.grant_id, world_id=person.world_id, at=at)

    assert "revoked_at" not in _token(driver, chain.access)
    assert _live(driver, chain.access, at) is None


def test_find_access_token_refuses_a_token_whose_world_is_gone(driver, hosts_plant):
    """hosts_plant.world() gives an id and no :World row; plant_session makes the row after."""
    world_id = hosts_plant.world()
    absent = Person(new_id(), world_id, "", "", "")
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, absent, client_id, at)

    assert _live(driver, chain.access, at) is None

    plant_session(driver, world_id, user_id=absent.user_id)
    assert _live(driver, chain.access, at).world_id == world_id


# Rotation ---------------------------------------------------------------------------------------


def test_rotation_keeps_the_family_and_the_grant_and_spends_the_old_refresh(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)
    later = at + timedelta(minutes=10)

    issued, access, refresh = _rotate(driver, chain.client_id, chain.refresh, later)

    assert issued == IssuedFamily(
        family_id=chain.code_id,
        grant_id=chain.grant_id,
        world_id=person.world_id,
        scopes=tuple(READ_WRITE),
        resource=RESOURCE,
    )
    old = _token(driver, chain.refresh)
    assert old["used_at"] == later and old["revoked_at"] == later
    for value in (access, refresh):
        row = _token(driver, value)
        assert (row["family_id"], row["grant_id"], row["world_id"]) == (
            chain.code_id,
            chain.grant_id,
            person.world_id,
        )
        assert row["created_at"] == later
    assert _token(driver, refresh)["expires_at"] == later + REFRESH_TOKEN_LIFETIME
    assert _live(driver, access, later).family_id == chain.code_id
    assert len(_family(driver, chain.code_id)) == 4

    again, _, _ = _rotate(driver, chain.client_id, refresh, later + timedelta(minutes=1))
    assert again is not None and again.family_id == chain.code_id


def test_rotation_may_narrow_the_scopes_and_never_widen_them(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)

    for asked in (["grove.read", "grove.sensitive_write"], [], ["grove.sensitive_write"]):
        with pytest.raises(ScopeNotGranted):
            _rotate(driver, chain.client_id, chain.refresh, at, scopes=asked)
    assert "used_at" not in _token(driver, chain.refresh)
    assert len(_family(driver, chain.code_id)) == 2

    issued, access, refresh = _rotate(driver, chain.client_id, chain.refresh, at, ["grove.read"])
    assert issued.scopes == ("grove.read",)
    assert _live(driver, access, at).scopes == ("grove.read",)
    assert _token(driver, refresh)["scopes"] == ["grove.read"]
    with pytest.raises(ScopeNotGranted):
        _rotate(driver, chain.client_id, refresh, at, scopes=["grove.write"])


def test_a_reused_refresh_token_revokes_the_whole_family_and_no_other(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)
    other = _chain(driver, person, client_id, at)
    _, access, refresh = _rotate(driver, client_id, chain.refresh, at + timedelta(minutes=1))
    assert _live(driver, access, at) is not None

    replay = _rotate(driver, client_id, chain.refresh, at + timedelta(minutes=2))[0]

    assert replay is None
    assert all(row.get("revoked_at") is not None for row in _family(driver, chain.code_id))
    assert _live(driver, access, at) is None
    assert _rotate(driver, client_id, refresh, at + timedelta(minutes=3))[0] is None
    assert _live(driver, other.access, at) is not None
    assert _rotate(driver, client_id, other.refresh, at + timedelta(minutes=3))[0] is not None


def test_rotation_refuses_unknown_access_foreign_and_expired_tokens_without_a_revoke(
    driver, hosts_plant
):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)

    assert _rotate(driver, client_id, new_secret(REFRESH_TOKEN_PREFIX), at)[0] is None
    assert _rotate(driver, client_id, chain.access, at)[0] is None
    assert _rotate(driver, hosts_plant.client(), chain.refresh, at)[0] is None
    expired = at + REFRESH_TOKEN_LIFETIME
    assert _rotate(driver, client_id, chain.refresh, expired)[0] is None

    assert all("revoked_at" not in row for row in _family(driver, chain.code_id))
    assert _rotate(driver, client_id, chain.refresh, at)[0] is not None


def test_rotation_under_a_revoked_grant_gives_nothing(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)
    revoke_grant(driver, grant_id=chain.grant_id, world_id=person.world_id, at=at)

    assert _rotate(driver, chain.client_id, chain.refresh, at)[0] is None
    assert len(_family(driver, chain.code_id)) == 2


# A family revoke and a rotation in flight --------------------------------------------------------


_REPLAYS = {
    "rotate_refresh": lambda driver, chain, at: _rotate(driver, chain.client_id, chain.refresh, at),
    "redeem_code": lambda driver, chain, at: redeem_code(
        driver, code_hash=token_hash(chain.code), at=at
    ),
    "revoke_token": lambda driver, chain, at: revoke_token(
        driver, token_hash=token_hash(chain.refresh), client_id=chain.client_id, at=at
    ),
}


@pytest.mark.parametrize("replay", _REPLAYS)
def test_a_family_revoke_waits_for_a_rotation_in_flight_and_revokes_its_pair(
    driver, hosts_plant, replay
):
    """The steps of rotate_refresh up to its commit, in an open transaction: the old token's lock,
    the grant's lock, the spend, the new pair. The revoke must wait, and must then see the pair."""
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)
    _, _, second_refresh = _rotate(driver, client_id, chain.refresh, at)
    access, refresh = new_secret(ACCESS_TOKEN_PREFIX), new_secret(REFRESH_TOKEN_PREFIX)

    def rotation_in_flight(held: HeldTransaction) -> None:
        held.run(oauth_module._LOCK_TOKEN, token_hash=token_hash(second_refresh))
        held.run(oauth_module._LOCK_GRANT, grant_id=chain.grant_id)
        held.run(oauth_module._SPEND_REFRESH, id=_token(driver, second_refresh)["id"], at=at)
        oauth_module._add_pair(
            held.tx,
            access_hash=token_hash(access),
            refresh_hash=token_hash(refresh),
            access_expires_at=at + ACCESS_TOKEN_LIFETIME,
            refresh_expires_at=at + REFRESH_TOKEN_LIFETIME,
            client_id=client_id,
            user_id=person.user_id,
            world_id=person.world_id,
            grant_id=chain.grant_id,
            family_id=chain.code_id,
            scopes=READ_WRITE,
            resource=RESOURCE,
            at=at,
        )

    _waits_on(
        driver,
        rotation_in_flight,
        lambda: _REPLAYS[replay](driver, chain, at + timedelta(seconds=1)),
    )

    family = _family(driver, chain.code_id)
    assert len(family) == 6
    assert all(row.get("revoked_at") is not None for row in family)
    assert _live(driver, access, at) is None
    assert _rotate(driver, client_id, refresh, at + timedelta(seconds=2))[0] is None


def test_a_rotation_and_a_replay_at_one_time_never_leave_a_live_pair(driver, hosts_plant):
    """The thief rotates the second refresh token as the victim replays the first. Whichever order
    the store gives, a deadlock included, the family ends with no live token."""
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    for _ in range(10):
        chain = _chain(driver, person, client_id, at)
        _, _, second_refresh = _rotate(driver, client_id, chain.refresh, at)
        barrier = Barrier(2, timeout=30)

        def spend(token: str, barrier: Barrier = barrier):
            barrier.wait()
            return _rotate(driver, client_id, token, at)

        with ThreadPoolExecutor(2) as pool:
            rotated = pool.submit(spend, second_refresh)
            replayed = pool.submit(spend, chain.refresh)
            issued, access, _ = rotated.result(timeout=60)
            assert replayed.result(timeout=60)[0] is None

        assert all(row.get("revoked_at") is not None for row in _family(driver, chain.code_id))
        if issued is not None:
            assert _live(driver, access, at) is None


# A lock on a row a racer deletes -----------------------------------------------------------------


def _hold_client_delete(client_id: str) -> Callable[[HeldTransaction], None]:
    def hold(held: HeldTransaction) -> None:
        held.run(oauth_module._LOCK_CLIENT, client_id=client_id)
        assert held.run(oauth_module._DROP_UNUSED_CLIENTS, client_ids=[client_id]) == [
            {"deleted": 1}
        ]

    return hold


def _hold_code_delete(code: str, client_id: str, at: datetime) -> Callable[[HeldTransaction], None]:
    """The cleanup statement of store_code, with a time past every code of this test's client."""

    def hold(held: HeldTransaction) -> None:
        held.run(oauth_module._LOCK_CODE, code_hash=token_hash(code))
        held.run(oauth_module._DROP_OLD_CODES, client_id=client_id, before=at + timedelta(days=1))

    return hold


def _hold_grant_delete(grant_id: str) -> Callable[[HeldTransaction], None]:
    def hold(held: HeldTransaction) -> None:
        held.run(oauth_module._LOCK_GRANT, grant_id=grant_id)
        held.run(DROP_GRANT, grant_id=grant_id)

    return hold


def _hold_token_delete(token: str) -> Callable[[HeldTransaction], None]:
    def hold(held: HeldTransaction) -> None:
        held.run(oauth_module._LOCK_TOKEN, token_hash=token_hash(token))
        held.run(DROP_TOKEN, token_hash=token_hash(token))

    return hold


def test_a_consent_that_waits_on_a_client_being_deleted_is_refused(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()

    with pytest.raises(UnknownClient):
        _waits_on(
            driver,
            _hold_client_delete(client_id),
            lambda: _consent(driver, person, client_id, clock.now()),
        )

    assert find_client(driver, client_id) is None
    assert list_grants(driver, person.world_id) == []


def test_a_consent_that_waits_on_a_grant_being_deleted_makes_a_new_grant(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    deleted = _consent(driver, person, client_id, at)

    grant_id = _waits_on(
        driver, _hold_grant_delete(deleted), lambda: _consent(driver, person, client_id, at)
    )

    assert grant_id is not None and grant_id != deleted
    row = _grant_row(driver, grant_id)
    assert (row["client_id"], row["world_id"]) == (client_id, person.world_id)


def test_a_document_fetch_that_waits_on_its_row_being_deleted_makes_a_new_row(driver, hosts_plant):
    at = clock.now()
    url = _document_client(driver, hosts_plant, at)

    _waits_on(
        driver,
        _hold_client_delete(url),
        lambda: _document_client(driver, hosts_plant, at, url=url, host="Lumen Desk 2"),
    )

    client = find_client(driver, url)
    assert client is not None and client.host == "Lumen Desk 2" and client.fetched_at == at


def test_a_redeem_that_waits_on_a_code_being_deleted_gives_nothing(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)

    redeemed = _waits_on(
        driver,
        _hold_code_delete(code, client_id, at),
        lambda: redeem_code(driver, code_hash=token_hash(code), at=at),
    )

    assert redeemed is None
    assert _code_row(driver, code) is None


def test_an_issue_that_waits_on_its_code_being_deleted_is_refused(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)

    with pytest.raises(GrantRevoked):
        _waits_on(
            driver, _hold_code_delete(code, client_id, at), lambda: _issue(driver, redeemed, at)
        )

    assert _family(driver, redeemed.id) == []


def test_an_issue_that_waits_on_its_grant_being_deleted_is_refused(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    grant_id = _consent(driver, person, client_id, at)
    code = _code(driver, person, client_id, grant_id, at)
    redeemed = redeem_code(driver, code_hash=token_hash(code), at=at)

    with pytest.raises(GrantRevoked):
        _waits_on(driver, _hold_grant_delete(grant_id), lambda: _issue(driver, redeemed, at))

    assert _family(driver, redeemed.id) == []


def test_a_rotation_that_waits_on_its_token_being_deleted_changes_nothing(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)

    answer = _waits_on(
        driver,
        _hold_token_delete(chain.refresh),
        lambda: _rotate(driver, chain.client_id, chain.refresh, at),
    )

    assert answer[0] is None
    assert [row["token_type"] for row in _family(driver, chain.code_id)] == ["access"]
    assert "revoked_at" not in _token(driver, chain.access)


def test_a_rotation_that_waits_on_its_grant_being_deleted_changes_nothing(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)

    answer = _waits_on(
        driver,
        _hold_grant_delete(chain.grant_id),
        lambda: _rotate(driver, chain.client_id, chain.refresh, at),
    )

    assert answer[0] is None
    assert len(_family(driver, chain.code_id)) == 2
    assert "used_at" not in _token(driver, chain.refresh)


def test_a_token_revoke_that_waits_on_its_token_being_deleted_changes_nothing(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)

    answer = _waits_on(
        driver,
        _hold_token_delete(chain.refresh),
        lambda: revoke_token(
            driver, token_hash=token_hash(chain.refresh), client_id=chain.client_id, at=at
        ),
    )

    assert answer is None
    assert [row["token_type"] for row in _family(driver, chain.code_id)] == ["access"]
    assert "revoked_at" not in _token(driver, chain.access)


def test_a_grant_revoke_that_waits_on_its_grant_being_deleted_answers_false(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    at = clock.now()
    chain = _chain(driver, person, hosts_plant.client(), at)

    answer = _waits_on(
        driver,
        _hold_grant_delete(chain.grant_id),
        lambda: revoke_grant(driver, grant_id=chain.grant_id, world_id=person.world_id, at=at),
    )

    assert answer is False
    assert "revoked_at" not in _token(driver, chain.access)


# Revokes ----------------------------------------------------------------------------------------


def test_revoke_grant_takes_every_token_of_the_grant_with_it(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    other_client = hosts_plant.client()
    at = clock.now()
    first = _chain(driver, person, client_id, at)
    second = _chain(driver, person, client_id, at)
    _, rotated_access, _ = _rotate(driver, client_id, second.refresh, at)
    kept = _chain(driver, person, other_client, at)
    later = at + timedelta(minutes=5)

    assert revoke_grant(driver, grant_id=first.grant_id, world_id=person.world_id, at=later)

    tokens = _family(driver, first.code_id) + _family(driver, second.code_id)
    assert len(tokens) == 6
    assert all(row["revoked_at"] in (at, later) for row in tokens)
    assert _grant_row(driver, first.grant_id)["revoked_at"] == later
    for value in (first.access, second.access, rotated_access):
        assert _live(driver, value, at) is None
    assert _live(driver, kept.access, at) is not None
    assert [grant.grant_id for grant in list_grants(driver, person.world_id)] == [kept.grant_id]

    again = later + timedelta(minutes=1)
    assert revoke_grant(driver, grant_id=first.grant_id, world_id=person.world_id, at=again)
    assert _grant_row(driver, first.grant_id)["revoked_at"] == later


def test_revoke_grant_answers_false_for_another_world_or_an_unknown_id(driver, hosts_plant):
    owner = hosts_plant.person(seeded=False)
    stranger = hosts_plant.person(SECOND_TITLE, seeded=False)
    at = clock.now()
    chain = _chain(driver, owner, hosts_plant.client(), at)

    assert revoke_grant(driver, grant_id=chain.grant_id, world_id=stranger.world_id, at=at) is False
    assert revoke_grant(driver, grant_id=new_id(), world_id=owner.world_id, at=at) is False
    assert "revoked_at" not in _grant_row(driver, chain.grant_id)
    assert _live(driver, chain.access, at) is not None


def test_a_new_consent_reuses_the_grant_and_clears_its_revoke_but_not_its_tokens(
    driver, hosts_plant
):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)
    revoke_grant(driver, grant_id=chain.grant_id, world_id=person.world_id, at=at)
    later = at + timedelta(hours=1)

    again = _consent(driver, person, client_id, later, scopes=["grove.read"])

    assert again == chain.grant_id
    row = _grant_row(driver, again)
    assert "revoked_at" not in row
    assert row["granted_at"] == later and row["scopes_granted"] == ["grove.read"]
    assert _live(driver, chain.access, at) is None
    fresh = _chain(driver, person, client_id, later)
    assert fresh.grant_id == chain.grant_id
    assert _live(driver, fresh.access, later) is not None


def test_a_narrower_consent_narrows_the_tokens_the_grant_already_issued(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)
    assert set(_live(driver, chain.access, at).scopes) == set(READ_WRITE)

    _consent(driver, person, client_id, at + timedelta(minutes=1), scopes=["grove.read"])

    assert _live(driver, chain.access, at).scopes == ("grove.read",)
    with pytest.raises(ScopeNotGranted):
        _rotate(driver, client_id, chain.refresh, at, scopes=["grove.write"])
    issued, access, refresh = _rotate(driver, client_id, chain.refresh, at)
    assert issued.scopes == ("grove.read",)
    assert _live(driver, access, at).scopes == ("grove.read",)
    assert _token(driver, refresh)["scopes"] == ["grove.read"]


def test_a_token_with_no_scope_left_is_not_live_and_cannot_refresh(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    chain = _chain(driver, person, client_id, at)

    _consent(driver, person, client_id, at, scopes=["grove.sensitive_write"])

    assert _live(driver, chain.access, at) is None
    assert _rotate(driver, client_id, chain.refresh, at)[0] is None
    assert "used_at" not in _token(driver, chain.refresh)


def test_consent_to_an_unknown_client_is_refused(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    with pytest.raises(UnknownClient):
        _consent(driver, person, new_id(), clock.now())
    assert list_grants(driver, person.world_id) == []


def test_revoke_token_follows_rfc_7009(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    at = clock.now()
    by_access = _chain(driver, person, client_id, at)
    by_refresh = _chain(driver, person, client_id, at)
    foreign = _chain(driver, person, client_id, at)

    revoke_token(driver, token_hash=token_hash(by_access.access), client_id=client_id, at=at)
    assert _token(driver, by_access.access)["revoked_at"] == at
    assert "revoked_at" not in _token(driver, by_access.refresh)

    revoke_token(driver, token_hash=token_hash(by_refresh.refresh), client_id=client_id, at=at)
    assert all(row["revoked_at"] == at for row in _family(driver, by_refresh.code_id))

    other_client = hosts_plant.client()
    revoke_token(driver, token_hash=token_hash(foreign.refresh), client_id=other_client, at=at)
    revoke_token(driver, token_hash=token_hash(new_secret("gvr_")), client_id=client_id, at=at)
    assert all("revoked_at" not in row for row in _family(driver, foreign.code_id))


# Clients ----------------------------------------------------------------------------------------


def test_a_dynamic_client_is_created_once_with_its_hashed_secret(driver, hosts_plant):
    at = clock.now()
    secret = new_secret("gvs_")
    client_id = hosts_plant.track_client(new_id())
    register_client(
        driver,
        client_id=client_id,
        host=CLIENT_NAME,
        redirect_uris=[LOOPBACK_REDIRECT, "lumen-desk:/callback"],
        registration_type=DYNAMIC,
        auth_method="client_secret_basic",
        secret_hash=token_hash(secret),
        at=at,
    )

    client = find_client(driver, client_id)
    assert client.host == CLIENT_NAME
    assert client.registration_type == DYNAMIC
    assert client.redirect_uris == (LOOPBACK_REDIRECT, "lumen-desk:/callback")
    assert client.auth_method == "client_secret_basic"
    assert client.secret_hash == token_hash(secret)
    assert client.registered_at == at and client.fetched_at is None
    with pytest.raises(oauth_module.ConstraintError):
        register_client(
            driver,
            client_id=client_id,
            host=None,
            redirect_uris=[LOOPBACK_REDIRECT],
            registration_type=DYNAMIC,
            auth_method="none",
            secret_hash=None,
            at=at,
        )
    assert find_client(driver, new_id()) is None
    public = find_client(driver, hosts_plant.client(host=None))
    assert public.host is None and public.secret_hash is None and public.auth_method == "none"


@pytest.mark.parametrize(
    ("registration_type", "auth_method", "secret_hash"),
    [
        ("pushed", "none", None),
        (DYNAMIC, "private_key_jwt", "a" * 64),
        (DYNAMIC, "none", "a" * 64),
        (DYNAMIC, "client_secret_post", None),
        (METADATA_DOCUMENT, "client_secret_post", "a" * 64),
    ],
)
def test_register_client_refuses_a_shape_it_cannot_store(
    driver, registration_type, auth_method, secret_hash
):
    with pytest.raises(ValueError):
        register_client(
            driver,
            client_id=new_id(),
            host=None,
            redirect_uris=[LOOPBACK_REDIRECT],
            registration_type=registration_type,
            auth_method=auth_method,
            secret_hash=secret_hash,
            at=clock.now(),
        )


def test_a_metadata_document_client_is_refreshed_in_place(driver, hosts_plant):
    url = hosts_plant.track_client(f"{DOCUMENT_URL}?run={new_id()}")
    # In the past, not the future: a registration's `at` also sets what its cleanup deletes.
    later = clock.now()
    first_at = later - timedelta(hours=2)
    for at, host, uris in (
        (first_at, "Lumen Desk", ["https://lumen-desk.example/callback"]),
        (later, "Lumen Desk 2", ["https://lumen-desk.example/callback2"]),
    ):
        register_client(
            driver,
            client_id=url,
            host=host,
            redirect_uris=uris,
            registration_type=METADATA_DOCUMENT,
            auth_method="none",
            secret_hash=None,
            at=at,
        )

    client = find_client(driver, url)
    assert client.registration_type == METADATA_DOCUMENT
    assert client.host == "Lumen Desk 2"
    assert client.redirect_uris == ("https://lumen-desk.example/callback2",)
    assert client.registered_at == first_at and client.fetched_at == later
    assert PlatformQuery(driver).read(
        "MATCH (c:OAuthClient {client_id: $id}) RETURN count(c) AS n", id=url
    ) == [{"n": 1}]


def test_registration_deletes_old_dynamic_clients_that_no_grant_names(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    now = clock.now()
    old = now - UNUSED_DYNAMIC_CLIENT_KEPT - timedelta(minutes=1)
    granted = hosts_plant.client()
    _consent(driver, person, granted, now)
    age_granted_client(driver, person.world_id, granted, old)
    unused = _dynamic_client(driver, hosts_plant, old)
    recent = _dynamic_client(driver, hosts_plant, now - timedelta(days=6))

    _clean_until_gone(driver, hosts_plant, unused, kept=[granted, recent])

    assert find_client(driver, unused) is None
    assert find_client(driver, granted).registered_at == old
    assert find_client(driver, recent) is not None


def test_old_clients_that_a_grant_names_do_not_fill_the_cleanup_limit(
    driver, hosts_plant, monkeypatch
):
    """With a limit of one, the oldest client has a grant: the cleanup must pass it and delete the
    next one, or a store with 100 old granted clients would never clean up again."""
    person = hosts_plant.person(seeded=False)
    now = clock.now()
    granted = hosts_plant.client()
    _consent(driver, person, granted, now)
    age_granted_client(driver, person.world_id, granted, now - timedelta(days=9000))
    unused = _dynamic_client(driver, hosts_plant, now - timedelta(days=8999))
    monkeypatch.setattr(oauth_module, "MAX_CLIENTS_DELETED_PER_REGISTRATION", 1)

    _clean_until_gone(driver, hosts_plant, unused, kept=[granted])

    assert find_client(driver, unused) is None
    assert find_client(driver, granted) is not None


def test_registration_deletes_document_clients_past_the_stale_limit(driver, hosts_plant):
    """The young side keeps a margin of hours: other tests register with `at` up to two hours ahead
    of the clock, and their cleanups must not reach it."""
    now = clock.now()
    past = _document_client(
        driver, hosts_plant, now - METADATA_DOCUMENT_STALE_LIMIT - timedelta(minutes=1)
    )
    young = _document_client(
        driver, hosts_plant, now - METADATA_DOCUMENT_STALE_LIMIT + timedelta(hours=3)
    )

    _clean_until_gone(driver, hosts_plant, past, kept=[young])

    assert find_client(driver, past) is None
    assert find_client(driver, young) is not None


def test_a_grant_keeps_a_document_client_past_the_stale_limit(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    now = clock.now()
    old = now - METADATA_DOCUMENT_STALE_LIMIT - timedelta(hours=1)
    granted = _document_client(driver, hosts_plant, now)
    _consent(driver, person, granted, now)
    age_granted_client(driver, person.world_id, granted, old)
    unused = _document_client(driver, hosts_plant, old)

    _clean_until_gone(driver, hosts_plant, unused, kept=[granted])

    assert find_client(driver, granted).fetched_at == old


def test_one_budget_covers_old_clients_of_both_types(driver, hosts_plant, monkeypatch):
    now = clock.now()
    monkeypatch.setattr(oauth_module, "MAX_CLIENTS_DELETED_PER_REGISTRATION", 1)
    planted = [
        _dynamic_client(driver, hosts_plant, now - timedelta(days=9100)),
        _document_client(driver, hosts_plant, now - timedelta(days=9101)),
    ]

    for _ in range(CLEANUP_CALLS):
        deleted = oauth_module._write(
            driver, lambda tx: oauth_module._drop_unused_clients(tx, at=now)
        )
        assert deleted <= 1
        if not any(find_client(driver, client_id) for client_id in planted):
            break

    assert not any(find_client(driver, client_id) for client_id in planted)


def _old_client_under_lock(held: HeldTransaction, make: Callable[[], str]) -> str:
    """An old client with no grant that this transaction has locked. Any run's cleanup may delete
    such a row before the lock, so another one is made when that happened."""
    for _ in range(CLEANUP_CALLS):
        client_id = make()
        held.run(oauth_module._LOCK_CLIENT, client_id=client_id)
        if find_client(held.driver, client_id) is not None:
            return client_id
    pytest.fail("every old client was deleted before its lock")


def _old_document(driver, plant: HostsPlant, now: datetime) -> str:
    return _document_client(
        driver, plant, now - METADATA_DOCUMENT_STALE_LIMIT - timedelta(minutes=1)
    )


def _old_dynamic(driver, plant: HostsPlant, now: datetime) -> str:
    return _dynamic_client(driver, plant, now - UNUSED_DYNAMIC_CLIENT_KEPT - timedelta(minutes=1))


def test_a_new_fetch_that_commits_while_a_cleanup_waits_keeps_its_row(driver, hosts_plant):
    """The cleanup's first statement sees a stale row and waits on its lock. The fetch commits. The
    drop looks again, in a statement of its own, and finds the row fresh."""
    now = clock.now()
    with ThreadPoolExecutor(1) as pool, held_transaction(driver) as held:
        url = _old_client_under_lock(held, lambda: _old_document(driver, hosts_plant, now))
        held.run(
            oauth_module._ENSURE_DOCUMENT_CLIENT,
            client_id=url,
            registration_type=METADATA_DOCUMENT,
            host=CLIENT_NAME,
            redirect_uris=["https://lumen-desk.example/callback"],
            auth_method="none",
            at=now,
        )
        job = pool.submit(hosts_plant.client)
        assert held.blocks(job.done), "the cleanup did not wait on the lock"
        held.commit()
        job.result(timeout=60)

    assert find_client(driver, url).fetched_at == now


@pytest.mark.parametrize("old_client", [_old_dynamic, _old_document])
def test_a_consent_that_commits_while_a_cleanup_waits_keeps_its_client(
    driver, hosts_plant, old_client
):
    """The steps of grant_consent up to its commit, in an open transaction."""
    person = hosts_plant.person(seeded=False)
    now = clock.now()
    with ThreadPoolExecutor(1) as pool, held_transaction(driver) as held:
        client_id = _old_client_under_lock(held, lambda: old_client(driver, hosts_plant, now))
        grant_id = new_id()
        held.run(
            oauth_module._ENSURE_GRANT,
            client_id=client_id,
            world_id=person.world_id,
            id=grant_id,
            user_id=person.user_id,
            scopes=READ_WRITE,
            at=now,
        )
        job = pool.submit(hosts_plant.client)
        assert held.blocks(job.done), "the cleanup did not wait on the lock"
        held.commit()
        job.result(timeout=60)

    assert find_client(driver, client_id) is not None
    assert _grant_row(driver, grant_id)["client_id"] == client_id


def test_age_granted_client_works_only_through_a_grant_of_a_test_world(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    stranger = hosts_plant.person(SECOND_TITLE, seeded=False)
    client_id = hosts_plant.client()
    now = clock.now()
    registered_at = find_client(driver, client_id).registered_at
    with pytest.raises(ValueError):
        age_granted_client(driver, "real-world", client_id, now)
    with pytest.raises(ValueError):
        age_granted_client(driver, person.world_id, client_id, now)
    _consent(driver, person, client_id, now)
    with pytest.raises(ValueError):
        age_granted_client(driver, stranger.world_id, client_id, now)
    assert find_client(driver, client_id).registered_at == registered_at

    long_ago = now - timedelta(days=30)
    age_granted_client(driver, person.world_id, client_id, long_ago)

    client = find_client(driver, client_id)
    assert client.registered_at == long_ago and client.fetched_at is None


def test_delete_unused_client_keeps_a_client_that_a_grant_names(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    client_id = hosts_plant.client()
    _consent(driver, person, client_id, clock.now())

    assert delete_unused_client(driver, client_id=client_id) is False
    assert find_client(driver, client_id) is not None
    lonely = hosts_plant.client()
    assert delete_unused_client(driver, client_id=lonely) is True
    assert find_client(driver, lonely) is None
    assert delete_unused_client(driver, client_id=lonely) is False


def test_a_delete_that_waits_on_a_client_being_deleted_answers_false(driver, hosts_plant):
    client_id = hosts_plant.client()

    answer = _waits_on(
        driver,
        _hold_client_delete(client_id),
        lambda: delete_unused_client(driver, client_id=client_id),
    )

    assert answer is False
    assert find_client(driver, client_id) is None


# Reads ------------------------------------------------------------------------------------------


def test_list_grants_gives_the_live_grants_of_one_world_newest_first(driver, hosts_plant):
    person = hosts_plant.person(seeded=False)
    stranger = hosts_plant.person(SECOND_TITLE, seeded=False)
    at = clock.now()
    older = hosts_plant.client(host="Lumen Desk")
    newer = hosts_plant.client(host="Quarry Notes")
    revoked = hosts_plant.client(host="Tern Board")
    _consent(driver, person, older, at)
    _consent(driver, person, newer, at + timedelta(minutes=1), scopes=["grove.read"])
    gone = _consent(driver, person, revoked, at + timedelta(minutes=2))
    revoke_grant(driver, grant_id=gone, world_id=person.world_id, at=at)
    _consent(driver, stranger, older, at)

    grants = list_grants(driver, person.world_id)

    assert [(g.client_id, g.host) for g in grants] == [
        (newer, "Quarry Notes"),
        (older, "Lumen Desk"),
    ]
    assert grants[0].scopes == ("grove.read",)
    assert grants[0].registration_type == DYNAMIC
    assert grants[0].user_id == person.user_id
    assert grants[0].granted_at == at + timedelta(minutes=1)
    assert [g.client_id for g in list_grants(driver, stranger.world_id)] == [older]


def test_the_person_fixture_signs_in_a_seeded_world(driver, person):
    from grove.views.session import find_session

    session = find_session(PlatformQuery(driver), person.token, clock.now())
    assert session.world_id == person.world_id
    assert session.user_id == person.user_id
    assert session.self_item_id == person.self_item_id
    assert person.world_id.startswith("t_")
    assert person.cookies == {"grove_session": person.token}


def test_the_purge_leaves_no_row_of_its_worlds_or_clients(driver, migrated):
    plant = HostsPlant(driver)
    person = plant.person(seeded=False)
    client_id = plant.client()
    chain = _chain(driver, person, client_id)
    document = plant.track_client(f"{DOCUMENT_URL}?run={new_id()}")
    register_client(
        driver,
        client_id=document,
        host=None,
        redirect_uris=["https://lumen-desk.example/callback"],
        registration_type=METADATA_DOCUMENT,
        auth_method="none",
        secret_hash=None,
        at=clock.now(),
    )

    plant.purge()

    platform = PlatformQuery(driver)
    for label, key, value in (
        ("OAuthClient", "client_id", client_id),
        ("OAuthClient", "client_id", document),
        ("OAuthGrant", "id", chain.grant_id),
        ("AuthCode", "id", chain.code_id),
        ("OAuthToken", "family_id", chain.code_id),
        ("World", "id", person.world_id),
        ("User", "id", person.user_id),
        ("Session", "user_id", person.user_id),
    ):
        found = platform.read(f"MATCH (n:{label} {{{key}: $v}}) RETURN count(n) AS n", v=value)
        assert found == [{"n": 0}], label
