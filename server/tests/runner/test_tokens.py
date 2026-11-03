"""The two runner credentials: a pass token binds one world, run and agent; the runner token is
checked by its hash."""

import secrets
from datetime import UTC, datetime

import pytest

from grove import clock
from grove.models.config import load_catalog
from grove.models.errors import Unauthenticated
from grove.models.gateway_auth import mint as mint_gateway
from grove.models.gateway_auth import verify as verify_gateway
from grove.runner.tokens import (
    PASS_SCOPES,
    TokenRefused,
    check_runner_token_hash,
    mint_pass_token,
    runner_token_hash,
    runner_token_matches,
    verify_pass_token,
)

WORLD = "t_runner_0b6c9f7e-3a51-4d2e-9a1f-6d2b8c4e1f00"
OTHER = "t_runner_5f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
RUN = "01a19fe3-690c-7976-a621-4e49cd31497e"
SECRET = secrets.token_bytes(32)


def mint(**over):
    fields = {"world_id": WORLD, "run_id": RUN, "agent": "learner", "ttl_seconds": 600} | over
    return mint_pass_token(SECRET, **fields)


def test_a_pass_token_carries_its_world_run_agent_and_the_two_scopes():
    claims = verify_pass_token(SECRET, mint())
    assert (claims.world_id, claims.run_id, claims.agent) == (WORLD, RUN, "learner")
    assert claims.scopes == PASS_SCOPES == {"grove.read", "grove.propose"}


def test_a_changed_payload_or_another_secret_is_refused():
    token = mint()
    prefix, payload, signature = token.split(".")
    forged = mint(world_id=OTHER).split(".")[1]
    with pytest.raises(TokenRefused):
        verify_pass_token(SECRET, f"{prefix}.{forged}.{signature}")
    with pytest.raises(TokenRefused):
        verify_pass_token(secrets.token_bytes(32), token)
    with pytest.raises(TokenRefused):
        verify_pass_token(SECRET, token + "x")


def test_an_expired_pass_token_is_refused():
    frozen = clock.TestClock()
    frozen.set(datetime(2026, 11, 3, 6, 0, tzinfo=UTC))
    with clock.using(frozen):
        token = mint(ttl_seconds=60)
        assert verify_pass_token(SECRET, token).world_id == WORLD
        frozen.advance(seconds=61)
        with pytest.raises(TokenRefused, match="expired"):
            verify_pass_token(SECRET, token)


def test_a_gateway_token_is_no_pass_token_and_a_pass_token_is_no_gateway_token():
    catalog = load_catalog()
    gateway = mint_gateway(SECRET, catalog, world_id=WORLD, run_id=RUN, job="update_pass")
    with pytest.raises(TokenRefused):
        verify_pass_token(SECRET, gateway)
    with pytest.raises(Unauthenticated):
        verify_gateway(SECRET, catalog, mint())


def test_minting_refuses_a_short_secret_an_unknown_agent_and_a_long_life():
    with pytest.raises(TokenRefused):
        mint_pass_token(b"short", world_id=WORLD, run_id=RUN, agent="learner", ttl_seconds=60)
    with pytest.raises(TokenRefused):
        mint(agent="operator")
    with pytest.raises(TokenRefused):
        mint(ttl_seconds=6 * 3600 + 1)


def test_the_runner_token_matches_only_by_its_hash():
    token = secrets.token_urlsafe(32)
    expected = check_runner_token_hash(runner_token_hash(token))
    assert runner_token_matches(token, expected)
    assert not runner_token_matches(token + "x", expected)
    assert not runner_token_matches(token, None)
    assert not runner_token_matches(None, expected)
    assert not runner_token_matches("short", runner_token_hash("short"))


def test_a_runner_token_hash_must_be_64_lowercase_hex():
    assert check_runner_token_hash(None) is None
    with pytest.raises(ValueError):
        check_runner_token_hash("ABC")
