"""The API process: `uv run uvicorn --factory grove.api.app:create_app` from server/.

The driver opens at startup from the environment (NEO4J_AUTH, GROVE_NEO4J_URI) unless a caller
hands one in. GROVE_WEB_DIST, for local runs only, names a folder whose index.html GET / returns;
in production nginx serves the page.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse

from grove.api.errors import install_error_handlers, not_found
from grove.api.views import PREFIX, router
from grove.store.connection import Driver, open_driver

WEB_DIST_ENV = "GROVE_WEB_DIST"


def _add_web_page(app: FastAPI, folder: str) -> None:
    page = Path(folder) / "index.html"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        if not page.is_file():
            raise not_found("no built page")
        return FileResponse(page, media_type="text/html")


def create_app(*, driver: Driver | None = None) -> FastAPI:
    """With a driver, the caller owns it: the app never closes it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if driver is None:
            app.state.driver = open_driver()
        try:
            yield
        finally:
            if driver is None:
                app.state.driver.close()

    app = FastAPI(title="Grove", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
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

    app.include_router(router)
    folder = os.environ.get(WEB_DIST_ENV)
    if folder:
        _add_web_page(app, folder)
    return app
