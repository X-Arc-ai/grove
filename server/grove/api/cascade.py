"""Explicit explanation requests and the post-commit dispatcher; ordinary reads stay pure."""

import math
from collections.abc import Callable
from dataclasses import asdict
from threading import Event, Thread
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from starlette.concurrency import run_in_threadpool

from grove.api.errors import ApiError, invalid_arguments, not_found
from grove.api.session import current_session
from grove.api.views import read_json_body
from grove.cascade_jobs import (
    explain_pending,
    read_explanation,
    worlds_with_pending_explanations,
)
from grove.models.config import load_model_settings
from grove.models.layer import ModelLayer
from grove.screening import ScreeningConfig
from grove.store.connection import Driver
from grove.views.session import ViewSession
from grove.writer.cascade_explanations import request_explanation

PREFIX = "/v1/cascades"
router = APIRouter(prefix=PREFIX)
Session = Annotated[ViewSession, Depends(current_session)]
MarkId = Annotated[str, Path(min_length=36, max_length=36)]


def _request(driver: Driver, world_id: str, mark_id: str) -> dict[str, Any]:
    found = read_explanation(driver, world_id, mark_id)
    if found is None:
        raise not_found("no such cascade mark")
    if found.needs_explanation:
        request_explanation(driver, world_id, mark_id)
    return asdict(found)


@router.post("/{mark_id}/explanation")
async def post_explanation(request: Request, session: Session, mark_id: MarkId) -> dict[str, Any]:
    """The session selects the world. An explicit empty JSON request only queues work."""
    origin = request.headers.get("origin")
    if origin is not None and origin != request.app.state.settings.public_base_url:
        raise ApiError(
            403,
            "forbidden_origin",
            "the Origin header names another site",
            details={"field": "origin"},
        )
    body = await read_json_body(request, 1024)
    if body != {}:
        raise invalid_arguments("the body must be an empty object", "body")
    return await run_in_threadpool(_request, request.app.state.driver, session.world_id, mark_id)


def _configured_layer(driver: Driver) -> ModelLayer:
    settings = load_model_settings()
    # An absent key or screening file must not consume a queued job's attempts.
    settings.require_meta_key()
    ScreeningConfig.from_environment()
    return ModelLayer(driver, settings=settings)


class ExplanationDispatcher:
    """One daemon worker; shutdown is bounded even while a provider request is active.

    The worker owns each model client and closes it in finally. If it owns the store driver,
    it closes that driver after its final pass, never underneath a cache transaction. A late
    response can finish its checked cache write; the stop signal prevents another pass.
    A killed process leaves a bounded job lease for the next process to recover.
    """

    def __init__(
        self,
        driver: Driver,
        *,
        interval: float = 5.0,
        layer_factory: Callable[[Driver], ModelLayer] = _configured_layer,
        inventory: Callable[[Driver], tuple[str, ...]] = worlds_with_pending_explanations,
        dispatch: Callable[..., Any] = explain_pending,
    ) -> None:
        if isinstance(interval, bool) or not math.isfinite(interval) or interval <= 0:
            raise ValueError("the dispatcher interval must be positive and finite")
        self.driver = driver
        self.interval = interval
        self.layer_factory = layer_factory
        self.inventory = inventory
        self.dispatch = dispatch
        self._stop = Event()
        self._thread: Thread | None = None
        self._close_driver = False
        self.status = "idle"

    def start(self, *, close_driver: bool = False) -> None:
        if self._thread is not None:
            raise RuntimeError("the explanation dispatcher already started")
        self._close_driver = close_driver
        self._thread = Thread(target=self._run, name="cascade-explanations", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.25)

    def run_once(self) -> None:
        """A bounded batch per world. Errors expose codes only and leave future passes possible."""
        layer = None
        try:
            worlds = self.inventory(self.driver)
            if not worlds or self._stop.is_set():
                self.status = "idle"
                return
            layer = self.layer_factory(self.driver)
            for world_id in worlds:
                if self._stop.is_set():
                    break
                try:
                    # One model call per world per pass bounds cancellation work. Templates
                    # remain available immediately for every other mark in the queue.
                    self.dispatch(layer, self.driver, world_id, limit=1)
                    self.status = "ready"
                except Exception:
                    # Exception text may contain provider content or private store values.
                    self.status = "unavailable"
        except Exception:
            self.status = "unavailable"
        finally:
            if layer is not None:
                try:
                    layer.close()
                except Exception:
                    self.status = "unavailable"

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.run_once()
                if self._stop.wait(self.interval):
                    break
        finally:
            self.status = "stopped"
            if self._close_driver:
                try:
                    self.driver.close()
                except Exception:
                    self.status = "unavailable"
