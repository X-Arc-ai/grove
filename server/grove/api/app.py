"""The API process: `uv run uvicorn --factory grove.api.app:create_app` from server/.

The driver opens at startup from the environment (NEO4J_AUTH, GROVE_NEO4J_URI) unless a caller
hands one in. GROVE_WEB_DIST names a folder whose index.html GET / returns: the API image builds
the page and sets it, and a local run can set it too.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

from grove.api import health, wellknown
from grove.api.config import ApiSettings, load_api_settings
from grove.api.errors import install_error_handlers, not_found
from grove.api.views import PREFIX, router
from grove.store.connection import Driver, open_driver

WEB_DIST_ENV = "GROVE_WEB_DIST"
# A store that is down must not hold a probe for the driver's default 30 s.
DRIVER_OPTIONS = {"connection_timeout": 5.0, "connection_acquisition_timeout": 10.0}


def _open_driver() -> Driver:
    return open_driver(**DRIVER_OPTIONS)


def _add_web_page(app: FastAPI, folder: str) -> None:
    page = Path(folder) / "index.html"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        if not page.is_file():
            raise not_found("no built page")
        return FileResponse(page, media_type="text/html")


def create_app(
    settings: ApiSettings | None = None,
    *,
    driver: Driver | None = None,
    driver_factory: Callable[[], Driver] = _open_driver,
) -> FastAPI:
    """With a driver, the caller owns it: the app never closes it."""
    settings = settings or load_api_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Opening a driver does not connect, so the API starts while the store is still down.
        if driver is None:
            app.state.driver = driver_factory()
        try:
            yield
        finally:
            if driver is None:
                app.state.driver.close()

    app = FastAPI(title="Grove", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    if driver is not None:
        app.state.driver = driver
    install_error_handlers(app)

    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """The views answer with the person's own data: no cache keeps a copy."""
        response = await call_next(request)
        if request.url.path.startswith(PREFIX):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(health.router)
    app.include_router(wellknown.router)
    app.include_router(router)
    folder = os.environ.get(WEB_DIST_ENV)
    if folder:
        _add_web_page(app, folder)
    return app
