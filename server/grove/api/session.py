"""The session dependency: the signed-in person's world, read from the cookie, or a typed 401."""

from fastapi import Request

from grove import clock
from grove.api.errors import unauthenticated
from grove.store.query import PlatformQuery
from grove.views.session import SESSION_COOKIE, ViewSession, find_session


def current_session(request: Request) -> ViewSession:
    driver = request.app.state.driver
    found = find_session(PlatformQuery(driver), request.cookies.get(SESSION_COOKIE), clock.now())
    if found is None:
        raise unauthenticated()
    return found
