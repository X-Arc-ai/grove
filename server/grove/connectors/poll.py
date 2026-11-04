"""Serial, code-only connector polling in the API process that holds source credentials."""

import asyncio
import json
import logging

import httpx
from starlette.concurrency import run_in_threadpool

from grove import clock
from grove.api.google import load_client
from grove.connectors.secrets import load_vault
from grove.connectors.service import sync_calendar, sync_git
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer.connectors import ConnectionBusy, connection, finish_sync, start_sync

LOG = logging.getLogger(__name__)


def poll_once(driver, settings, deps) -> bool:
    worlds = PlatformQuery(driver).read("MATCH (w:World) RETURN w.id AS id ORDER BY w.id")
    due = []
    for world in worlds:
        now = clock.now(world["id"])
        rows = WorldQuery(driver, world["id"]).read(
            "MATCH (c:Connector {connection_status: 'connected'}) "
            "RETURN c.id AS id, c.provider AS provider, c.last_attempted_read AS attempted, "
            "c.expected_read_interval AS interval, c.settings AS settings"
        )
        for row in rows:
            attempted = row["attempted"]
            if attempted is None or (now - attempted).total_seconds() >= (row["interval"] or 300):
                due.append(
                    (attempted.isoformat() if attempted else "", world["id"], row["id"], row)
                )
    if not due:
        return False
    _, world, chosen, row = min(due, key=lambda item: item[:3])
    try:
        _read_selected(driver, settings, deps, world, chosen, row)
    except Exception:
        # Configuration failure before a lease must not starve all other connections.
        current = connection(driver, world, chosen)
        if current.get("last_attempted_read") == row["attempted"]:
            try:
                _, owner = start_sync(driver, world, chosen)
                finish_sync(driver, world, chosen, owner, outcome="failed")
            except ConnectionBusy:
                pass
        raise
    return True


def _read_selected(driver, settings, deps, world, chosen, row):
    from grove.api.connectors import repository_path
    from grove.api.uploads import _screener

    screener = _screener()
    if row["provider"] == "git":
        path = repository_path(world, json.loads(row["settings"])["repository"], deps)
        sync_git(driver, world, chosen, path=path, screener=screener)
    elif row["provider"] == "google_calendar":
        client = deps.client or load_client(settings.google_oauth_file)
        if client is None:
            raise ValueError("calendar client is not configured")
        vault = deps.vault or load_vault()
        if deps.http is not None:
            sync_calendar(
                driver,
                world,
                chosen,
                http=deps.http,
                client=client,
                vault=vault,
                screener=screener,
                base_url=deps.calendar_base,
            )
        else:
            with httpx.Client() as http:
                sync_calendar(
                    driver,
                    world,
                    chosen,
                    http=http,
                    client=client,
                    vault=vault,
                    screener=screener,
                    base_url=deps.calendar_base,
                )


async def run_poll(driver, settings, deps, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await run_in_threadpool(poll_once, driver, settings, deps)
        except Exception as error:
            # The type is enough for an operator; exceptions can carry provider credentials.
            LOG.warning("source poll failed (%s)", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass
