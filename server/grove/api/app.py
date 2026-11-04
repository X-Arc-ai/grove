"""The API process: `uv run uvicorn --factory grove.api.app:create_app` from server/.

The driver opens at startup from the environment (NEO4J_AUTH, GROVE_NEO4J_URI) unless a caller
hands one in. GROVE_WEB_DIST names the folder of the built pages (home.html, index.html,
privacy.html and consent.html): the API image builds them and sets it, and a local run can set it
too. The consent page is read from it at each /authorize, so a page built later needs no restart.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from grove.api import cascade, connectors, health, mcp, oauth, pipedream, uploads, wellknown
from grove.api.auth import AUTH_PREFIX, AuthDeps
from grove.api.auth import router as auth_router
from grove.api.config import WEB_DIST_ENV, ApiSettings, load_api_settings
from grove.api.errors import ApiError, install_error_handlers, not_found
from grove.api.internal import PREFIX as INTERNAL_PREFIX
from grove.api.internal import router as internal_router
from grove.api.mail import Mailer
from grove.api.oauth import OAUTH_PATHS, OAuthDeps
from grove.api.views import PREFIX, router
from grove.demo_guard import ReleaseFile, configuration
from grove.mcp.demo import DemoReleaseMiddleware
from grove.mcp.tools import DEFAULT_TOOLS, Tool
from grove.runner.passes import PassSettings, RunnerAuth
from grove.store.connection import Driver, open_driver
from grove.views.session import SESSION_COOKIE
from grove.writer.errors import InvalidRecord

__all__ = ["WEB_DIST_ENV", "create_app"]

# Answers the person's data or a secret: no cache may keep a copy.
NO_STORE_PATHS = (
    PREFIX,
    AUTH_PREFIX,
    INTERNAL_PREFIX,
    uploads.PREFIX,
    cascade.PREFIX,
    connectors.PREFIX,
    *OAUTH_PATHS,
    "/.well-known/",
)
# A store that is down must not hold a probe for the driver's default 30 s.
DRIVER_OPTIONS = {"connection_timeout": 5.0, "connection_acquisition_timeout": 10.0}


def _open_driver() -> Driver:
    return open_driver(**DRIVER_OPTIONS)


def _add_web_pages(app: FastAPI, folder: str) -> None:
    def page(name: str, headers: dict[str, str] | None = None) -> FileResponse:
        path = Path(folder) / name
        if not path.is_file():
            raise not_found("no built page")
        return FileResponse(path, media_type="text/html", headers=headers)

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> FileResponse:
        """No store read: with a cookie, the page itself asks the session and shows the sign-in
        card on a 401."""
        name = "index.html" if request.cookies.get(SESSION_COOKIE) else "home.html"
        return page(name, {"Cache-Control": "no-store", "Vary": "Cookie"})

    @app.get("/signin", include_in_schema=False)
    def signin() -> FileResponse:
        return page("index.html", {"Cache-Control": "no-store"})

    @app.get("/privacy", include_in_schema=False)
    def privacy() -> FileResponse:
        return page("privacy.html")


def create_app(
    settings: ApiSettings | None = None,
    *,
    driver: Driver | None = None,
    driver_factory: Callable[[], Driver] = _open_driver,
    mailer: Mailer | None = None,
    google_http: httpx.Client | None = None,
    world_id_for: Callable[[str], str] | None = None,
    mcp_tools: Sequence[Tool] | None = None,
    oauth_deps: OAuthDeps | None = None,
    runner_auth: RunnerAuth | None = None,
    pass_settings: PassSettings | None = None,
    explanation_dispatcher_factory: Callable[[Driver], cascade.ExplanationDispatcher] | None = None,
    connector_deps: connectors.ConnectorDeps | None = None,
) -> FastAPI:
    """With a driver, the caller owns it: the app never closes it. With no mailer or google_http,
    sign-in loads the real one at first use. world_id_for gives a test its own t_ worlds.
    mcp_tools is the tool set of /mcp (None means grove.mcp.tools.DEFAULT_TOOLS); oauth_deps hands
    the OAuth routes a fake resolver and transport for metadata documents, or a smaller window.
    With no runner_auth, the internal routes read GROVE_GATEWAY_SECRET and
    GROVE_RUNNER_TOKEN_SHA256 at first use. The explanation dispatcher starts only for an owned
    driver, unless explanation_dispatcher_factory explicitly enables it for an injected driver."""
    settings = settings or load_api_settings()
    demo_config = configuration()
    if demo_config is not None:
        ReleaseFile(demo_config).close()
    connector_config = connector_deps or connectors.ConnectorDeps()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Opening a driver does not connect, so the API starts while the store is still down.
        if driver is None:
            app.state.driver = driver_factory()
        dispatcher = None
        poll_stop = asyncio.Event()
        poll_task = None
        if os.environ.get("GROVE_CONNECTOR_POLL") == "1":
            from grove.connectors.poll import run_poll

            poll_task = asyncio.create_task(
                run_poll(app.state.driver, settings, connector_config, poll_stop)
            )
        try:
            if driver is None or explanation_dispatcher_factory is not None:
                factory = explanation_dispatcher_factory or cascade.ExplanationDispatcher
                candidate = factory(app.state.driver)
                candidate.start(close_driver=driver is None)
                dispatcher = candidate
            yield
        finally:
            poll_stop.set()
            try:
                if poll_task is not None:
                    await poll_task
            finally:
                if dispatcher is not None:
                    dispatcher.stop()
                elif driver is None:
                    app.state.driver.close()

    # redirect_slashes=False: Starlette builds that redirect from the request, and uvicorn runs
    # without proxy headers, so it would name plain http and a client would send its body in clear.
    app = FastAPI(
        title="Grove",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.auth = AuthDeps(mailer=mailer, google_http=google_http, world_id_for=world_id_for)
    app.state.oauth = oauth_deps or OAuthDeps()
    app.state.mcp_tools = tuple(DEFAULT_TOOLS if mcp_tools is None else mcp_tools)
    app.state.demo_config = demo_config
    if demo_config is not None:
        from grove.mcp.tools import demo_tools

        app.state.demo_tools = demo_tools()
    app.state.runner_auth = runner_auth
    app.state.pass_settings = pass_settings
    app.state.connectors = connector_config
    if driver is not None:
        app.state.driver = driver
    install_error_handlers(app)

    @app.exception_handler(InvalidRecord)
    async def source_refusal(request: Request, error: InvalidRecord) -> JSONResponse:
        if error.code == "scope_denied" and error.rule == "restricted_demo":
            refused = ApiError(
                403, "scope_denied", "source connections are disabled for this world"
            )
        else:
            refused = ApiError(500, "internal", "the server failed")
        return JSONResponse(
            refused.body(), status_code=refused.status, headers={"Cache-Control": "no-store"}
        )

    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """The views, sign-in, sources and OAuth answer with the person's data or a secret."""
        response = await call_next(request)
        if request.url.path.startswith(NO_STORE_PATHS):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(DemoReleaseMiddleware, config=demo_config)
    app.include_router(health.router)
    app.include_router(wellknown.router)
    app.include_router(oauth.router)
    app.include_router(mcp.router)
    app.include_router(router)
    app.include_router(uploads.router)
    app.include_router(connectors.router)
    app.include_router(pipedream.router)
    app.include_router(auth_router)
    app.include_router(internal_router)
    app.include_router(cascade.router)
    folder = os.environ.get(WEB_DIST_ENV)
    if folder:
        _add_web_pages(app, folder)
    return app
