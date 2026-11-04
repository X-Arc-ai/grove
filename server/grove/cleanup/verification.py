"""Retained-source checks and isolated D4 judges for the inactive D11 component."""

import json

from grove import clock
from grove.cleanup.access import Access, CleanupRefused, hold
from grove.cleanup.contracts import to_batch
from grove.cleanup.fetch import fetch_page
from grove.ids import new_id
from grove.models import verify_judge
from grove.prompts import build_prompt
from grove.screening import Message, Screener
from grove.store.lane import batch as lane_batch
from grove.store.query import WorldQuery
from grove.writer import apply_batch
from grove.writer.caller import Caller
from grove.writer.cleanup import RetainedFetch, retain_fetch
from grove.writer.clocks import parse_instant
from grove.writer.requests import check_request_id


def prepare_lane(driver, world_id, run_id, *, access: Access | None = None):
    """Return the published D4 batch order, including its oldest-claim reserve."""
    with hold(access, world_id, run_id, "prepare_lane"):
        return tuple(lane_batch(WorldQuery(driver, world_id), at=clock.now(world_id)))


def fetch_and_retain(
    driver,
    world_id,
    run_id,
    url,
    *,
    hosts,
    screener,
    access: Access | None = None,
    resolver=None,
    transport=None,
):
    if type(screener) is not Screener:
        raise CleanupRefused("fetch_screening_unavailable")
    options = {} if resolver is None else {"resolver": resolver}
    page = fetch_page(
        url,
        hosts=hosts,
        transport=transport,
        guard=lambda: hold(access, world_id, run_id, "fetch"),
        **options,
    )
    with hold(access, world_id, run_id, "screen_fetch"):
        # This time means an observation of the fetched page, never the event it describes.
        observed_at = clock.now(world_id)
        message = Message(new_id(world_id), None, page.text, observed_at, "third_party", "text")
        screened = screener.screen_source(page.url, [message])
    return retain_fetch(driver, world_id, run_id, screened, access=access)


def retained_piece(query, reference: RetainedFetch):
    """Read only this component's dated, screened, committed page observations.

    No TextDeltaDraft, raw request excerpt or legacy undated source is admitted.
    Typed A2 content needs its own future owner admission; it never falls back here.
    """
    if type(reference) is not RetainedFetch:
        raise CleanupRefused("judge_source_not_retained")
    rows = query.read(
        "MATCH (s:Source {id: $source}), (e:Episode {id: $episode}), "
        "(r:ScreeningReceipt), (c:Connector) "
        "WHERE s.id IN e.source_ids AND r.source_id = s.id AND c.id = s.connector_id "
        "RETURN s AS source, e AS episode, c.type AS type",
        source=reference.source_id,
        episode=reference.episode_id,
    )
    if len(rows) != 1:
        raise CleanupRefused("judge_source_not_retained")
    source, episode = rows[0]["source"], rows[0]["episode"]
    if (
        source.get("parser_version") != "curator-fetch-1"
        or source.get("forget_state") != "kept"
        or rows[0]["type"] != "web_pages"
    ):
        raise CleanupRefused("judge_source_not_admitted")
    try:
        messages = json.loads(source["content"])
        message = messages[0]
        valid = (
            len(messages) == 1
            and "source_facts" not in message
            and message["key"] == reference.message_key
            and parse_instant(message["at"]) == reference.observed_at
            and episode["start_time"] == episode["end_time"] == reference.observed_at
            and source["received_at"] >= reference.observed_at
        )
    except (KeyError, TypeError, ValueError, IndexError):
        valid = False
    if not valid:
        raise CleanupRefused("judge_source_time_or_identity_invalid")
    return verify_judge.Piece(
        verify_judge.cut(source["content"], "whole"),
        "third_party",
        "confirmed",
        source_id=reference.source_id,
        locator="whole",
    )


def _ordinary_second(layer, driver, world_id, claim_id, pieces, run_id):
    # Reuse the fixed D4 second prompt and recorder. Ordinary checks retain their
    # waiting budget jobs; they never borrow the load-bearing budget exception.
    claim_type, text = verify_judge._claim(WorldQuery(driver, world_id), claim_id)
    prompt = build_prompt(verify_judge.SECOND_PROMPT, verify_judge._blocks(claim_id, text, pieces))
    job, correlated = "second_opinion", False
    try:
        result = layer.chat(
            world_id,
            job=job,
            messages=prompt.messages(),
            schema=verify_judge.SCHEMA,
            schema_name="verdicts",
            run_id=run_id,
        )
    except verify_judge.UNREACHABLE:
        job, correlated = "second_opinion_fallback", True
        result = layer.respond(
            world_id,
            job=job,
            instructions=prompt.instructions,
            input=prompt.data,
            schema=verify_judge.SCHEMA,
            schema_name="verdicts",
            run_id=run_id,
        )
    return verify_judge._record(
        driver,
        world_id,
        result,
        job=job,
        role=verify_judge.SECOND_JUDGE,
        subject=f"{claim_type}:{claim_id}",
        pieces=pieces,
        correlated=correlated,
    )


def judge_fetches(
    layer,
    driver,
    world_id,
    run_id,
    claim_id,
    references,
    *,
    load_bearing=False,
    borderline=False,
    conflict=False,
    access: Access | None = None,
):
    """Trusted code supplies risk flags; they are absent from the agent contract.

    The first and second judge receive the same retained evidence. Neither judge
    receives the Curator's proposed answer or the other judge's output.
    """
    if (
        not isinstance(references, tuple)
        or not 1 <= len(references) <= 12
        or any(type(ref) is not RetainedFetch for ref in references)
    ):
        raise CleanupRefused("judge_sources_invalid")
    with hold(access, world_id, run_id, "judge", claim_id, *(ref.source_id for ref in references)):
        query = WorldQuery(driver, world_id)
        pieces = [retained_piece(query, ref) for ref in references]
        first = verify_judge.judge(
            layer,
            driver,
            world_id,
            claim_id=claim_id,
            pieces=pieces,
            run_id=run_id,
            conflict=conflict,
            load_bearing=load_bearing,
        )
        second = None
        if load_bearing:
            second = verify_judge.second_judge(
                layer, driver, world_id, claim_id=claim_id, pieces=pieces, run_id=run_id
            )
        elif borderline:
            second = _ordinary_second(layer, driver, world_id, claim_id, pieces, run_id)
        return first, second


def apply_dispositions(
    driver, world_id, run_id, document, *, claims, request_id, access: Access | None = None
):
    check_request_id(request_id)
    batch = to_batch(document, run_id=run_id, claims=frozenset(claims))
    with hold(access, world_id, run_id, "apply_dispositions", *sorted(claims)):
        return apply_batch(
            driver,
            world_id,
            Caller("cleanup", {"grove.propose"}, "curator"),
            batch,
            request_id=request_id,
            run_id=run_id,
        )
