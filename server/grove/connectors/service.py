"""A connection read commits its cursor only after every source delta succeeds."""

import json

from grove.connectors import calendar, google
from grove.connectors.secrets import Vault
from grove.ingest.take_in import take_parsed
from grove.writer.connectors import ConnectionBusy, check_sync, finish_sync, start_sync


def sync_calendar(
    driver,
    world: str,
    connection_id: str,
    *,
    http,
    client,
    vault: Vault,
    screener,
    base_url: str = calendar.API_BASE,
):
    row, owner = start_sync(driver, world, connection_id)
    credential = None

    def authorize():
        check_sync(driver, world, connection_id, owner)

    try:
        tokens = vault.open(row["credential_ciphertext"], world=world, reference=connection_id)
        authorize()
        tokens = google.refresh(http, client, tokens)
        authorize()
        credential = vault.seal(tokens, world=world, reference=connection_id)
        result = calendar.read_calendar(
            http,
            tokens["calendar_id"],
            tokens["access_token"],
            sync_token=row.get("sync_cursor"),
            versions=json.loads(row.get("event_versions") or "{}"),
            base_url=base_url,
            namespace=row["instance_key"],
            authorize=authorize,
        )
        taken = take_parsed(
            driver,
            world,
            calendar.SPEC,
            connection_id,
            result.chats,
            screener=screener,
            record_coverage=False,
            sync_owner=owner,
        )
        if taken.failed_chats:
            raise calendar.CalendarRefused()
        outcome = {"taken_in": "took_in", "kept_out": "kept_out"}.get(taken.state, "nothing_new")
        finish_sync(
            driver,
            world,
            connection_id,
            owner,
            outcome=outcome,
            cursor=result.sync_token,
            versions=result.versions,
            credential=credential,
        )
        return {**taken.to_api(), "reset": result.reset}
    except Exception as error:
        denied = isinstance(error, calendar.CalendarRefused) and error.reason == "denied"
        _failed_sync(
            driver,
            world,
            connection_id,
            owner,
            outcome="denied" if denied else "failed",
            credential=credential,
        )
        raise


def _failed_sync(driver, world, connection_id, owner, *, outcome="failed", credential=None):
    try:
        finish_sync(driver, world, connection_id, owner, outcome=outcome, credential=credential)
    except ConnectionBusy:
        # A revoked/superseded reader cannot change the new owner's state. Keep the original
        # failure and never restore disconnected credentials or cursors during cleanup.
        pass


def sync_git(driver, world: str, connection_id: str, *, path, screener):
    from grove.connectors.git import read_repository
    from grove.ingest import ConnectorSpec, NotAnExport

    def not_upload(_upload):
        raise NotAnExport("git is read only through its authorized connector")

    spec = ConnectorSpec(
        "git", "git", "git_clone.v1", "commit.v1", "git", not_upload, cut_rule="commit.v1"
    )
    row, owner = start_sync(driver, world, connection_id)
    try:
        check_sync(driver, world, connection_id, owner)
        read = read_repository(path, row["instance_key"], previous_head=row.get("sync_cursor"))
        check_sync(driver, world, connection_id, owner)
        taken = take_parsed(
            driver,
            world,
            spec,
            connection_id,
            read.chats,
            screener=screener,
            record_coverage=False,
            sync_owner=owner,
        )
        if taken.failed_chats:
            raise NotAnExport("git source could not be stored")
        outcome = {"taken_in": "took_in", "kept_out": "kept_out"}.get(taken.state, "nothing_new")
        finish_sync(driver, world, connection_id, owner, outcome=outcome, cursor=read.head)
        return taken.to_api()
    except Exception:
        _failed_sync(driver, world, connection_id, owner)
        raise
