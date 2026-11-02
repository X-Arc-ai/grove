"""plant_session and set_self_item: t_ worlds only, hashed tokens, and the purge removes them."""

from datetime import timedelta
from uuid import uuid4

import pytest

from grove import clock
from grove.store.query import PlatformQuery
from grove.views.session import token_hash
from grove.writer.testing import plant_session, purge_test_world, set_self_item

SESSION = (
    "MATCH (s:Session {token_hash: $h}) "
    "RETURN s.user_id AS user_id, s.expires_at AS expires_at, s.revoked_at AS revoked_at"
)
WORLD = "MATCH (w:World {id: $id}) RETURN w.owner_user_id AS owner, w.self_item_id AS own_item"
USER = "MATCH (u:User {id: $id}) RETURN u.id AS id"


@pytest.mark.parametrize("bad", ["", "real-world", f"x_views-a_{uuid4()}", str(uuid4())])
def test_the_helpers_refuse_a_world_that_is_not_a_test_world(bad):
    with pytest.raises(ValueError):
        plant_session(object(), bad)
    with pytest.raises(ValueError):
        set_self_item(object(), bad, "item")


def test_a_planted_session_stores_only_the_hash_and_owns_the_world(driver, make_world):
    world = make_world()
    token = plant_session(driver, world)
    platform = PlatformQuery(driver)
    assert platform.read("MATCH (s:Session {token_hash: $h}) RETURN s", h=token) == []
    session = platform.read(SESSION, h=token_hash(token))[0]
    assert session["revoked_at"] is None
    assert session["expires_at"] > clock.now() + timedelta(minutes=50)
    assert platform.read(WORLD, id=world)[0] == {"owner": session["user_id"], "own_item": None}
    assert len(token) >= 32 and plant_session(driver, world) != token

    set_self_item(driver, world, "an-item")
    assert platform.read(WORLD, id=world)[0]["own_item"] == "an-item"
    set_self_item(driver, world, None)
    assert platform.read(WORLD, id=world)[0]["own_item"] is None


def test_the_purge_removes_the_user_the_session_and_the_world(driver, make_world):
    world = make_world()
    token = plant_session(driver, world, revoked_at=clock.now())
    platform = PlatformQuery(driver)
    user_id = platform.read(SESSION, h=token_hash(token))[0]["user_id"]
    purge_test_world(driver, world)
    assert platform.read(SESSION, h=token_hash(token)) == []
    assert platform.read(USER, id=user_id) == []
    assert platform.read(WORLD, id=world) == []
