"""A belief retest reads only the exact URL authorized on its stored connector.

The existing bounded, DNS-pinned HTTPS reader supplies transport safety. No caller
can supply a URL, source text, headers, credentials, clock, or evidence verdict.
"""

import json
from typing import Any

import httpx

from grove import clock
from grove.models.verify_judge import JudgeInputError
from grove.oauth.metadata_document import _read_within_limit, _target, system_resolver
from grove.prompts import DataBlock, render_blocks
from grove.screening import Message, Screener
from grove.store.query import WorldQuery
from grove.writer.sources import add_screened_source, record_episode
from grove.writer.tx import WorldWriter


def fetch_retest(
    driver: Any,
    world_id: str,
    belief_id: str,
    *,
    screener: Screener,
    resolver=system_resolver,
    transport: httpx.BaseTransport | None = None,
    run_id: str | None = None,
) -> dict:
    query = WorldQuery(driver, world_id)
    rows = query.read("MATCH (b:Belief {item_id: $id}) RETURN b", id=belief_id)
    if not rows or rows[0]["b"].get("externally_testable") is not True:
        raise JudgeInputError("the belief has no external retest")
    try:
        how = json.loads(rows[0]["b"].get("retest_how") or "null")
    except (TypeError, ValueError):
        how = None
    if not isinstance(how, dict) or set(how) != {"connector", "locator"}:
        raise JudgeInputError("the retest requires a registered connector and locator")
    connector_id, url = how["connector"], how["locator"]
    if not isinstance(connector_id, str) or not isinstance(url, str):
        raise JudgeInputError("invalid retest connector or locator")
    connectors = query.read("MATCH (c:Connector {id: $id}) RETURN c", id=connector_id)
    if not connectors or connectors[0]["c"].get("type") != "web_pages":
        raise JudgeInputError("this retest requires a registered web-pages connector")
    try:
        scope = json.loads(connectors[0]["c"].get("scope") or "null")
    except (TypeError, ValueError):
        scope = None
    if not isinstance(scope, list) or url not in scope:
        raise JudgeInputError("the connector scope does not authorize this exact locator")
    target = _target(url)
    if target.port != 443:
        raise JudgeInputError("a retest requires the standard HTTPS port")
    raw = _read_within_limit(
        target, user_id=f"belief:{world_id}", resolver=resolver, transport=transport
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise JudgeInputError("the retest source is not UTF-8 text") from None
    at = clock.now(world_id)
    screened = screener.screen_source(
        url, [Message("retest", None, text, at, speaker_kind="automated", modality="text")]
    )
    if screened.dropped_whole or not screened.messages:
        raise JudgeInputError("screening kept no retest evidence")
    source = add_screened_source(
        WorldWriter(driver, world_id),
        screened,
        connector_id=connector_id,
        parser_version="belief-retest-v1",
        speakers={"retest": {"kind": "automated", "locator": "whole"}},
    )
    source_id = source["source_id"]
    episode_id = record_episode(
        driver,
        world_id,
        connector_id=connector_id,
        source_ids=[source_id],
        first_message_key=f"{source_id}:retest",
        last_message_key=f"{source_id}:retest",
        start_time=at,
        end_time=at,
        cut_rule="live_check",
        run_id=run_id,
    )
    return {
        "source_id": source_id,
        "episode_id": episode_id,
        "locator": "whole",
        "data": render_blocks(
            [DataBlock("source", "automated", "confirmed", source_id, screened.content())]
        ),
        "observation": {
            "ev_id": f"retest:{belief_id}:{source_id}",
            "source_id": source_id,
            "episode_id": episode_id,
            "locator": "whole",
            "message_key": "retest",
            "source_time": at.isoformat(),
            "statement_class": "report_state",
            "derivation": "observed",
            "speaker_kind": "automated",
            "modality": "text",
        },
    }
