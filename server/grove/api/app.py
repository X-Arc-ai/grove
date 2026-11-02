"""The API process: uvicorn grove.api.app:create_app --factory."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grove.api import health, wellknown
from grove.api.config import ApiSettings, load_api_settings
from grove.store.connection import open_driver

# A store that is down must not hold a probe for the driver's default 30 s.
DRIVER_OPTIONS = {"connection_timeout": 5.0, "connection_acquisition_timeout": 10.0}


def _open_driver():
    return open_driver(**DRIVER_OPTIONS)


def create_app(
    settings: ApiSettings | None = None, driver_factory: Callable = _open_driver
) -> FastAPI:
    settings = settings or load_api_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Opening a driver does not connect, so the API starts while the store is still down.
        app.state.driver = driver_factory()
        try:
            yield
        finally:
            app.state.driver.close()

    app = FastAPI(title="Grove", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(wellknown.router)
    return app
