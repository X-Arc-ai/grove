"""D11 snapshot writes under the ordinary World lock and generation protocol.

These functions have no production caller. Complete owner authorization must
span each call; no default authority, source type override or legacy draft path
exists. Dispositions themselves continue through the existing D4 batch writer.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from grove import clock
from grove.cleanup.access import Access, CleanupRefused, hold
from grove.cleanup.ambiguity import Ambiguity, expiry
from grove.cleanup.questions import Question
from grove.demo_policy import designation_in_tx, has_designation
from grove.ids import new_id
from grove.screening import ScreenedSource, Screener
from grove.writer.caller import Caller
from grove.writer.history import Stamp, add_text
from grove.writer.provenance import check_locator
from grove.writer.receipts import Receipt
from grove.writer.sources import write_screened_source
from grove.writer.tx import WorldWriter


@dataclass(frozen=True)
class RetainedFetch:
    source_id: str
    episode_id: str
    message_key: str
    observed_at: datetime

    def observation(self, ev_id: str) -> dict:
        return {
            "ev_id": ev_id,
            "source_id": self.source_id,
            "episode_id": self.episode_id,
            "message_key": self.message_key,
            "source_time": self.observed_at.isoformat(),
            "locator": "whole",
            "statement_class": "report_state",
            "derivation": "observed",
            "speaker_kind": "third_party",
            "modality": "text",
        }


def _ordinary(tx):
    if has_designation(designation_in_tx(tx) or {}):
        raise CleanupRefused("curator_designated_world_refused")


def _caller(caller, allowed):
    if not isinstance(caller, Caller) or caller.kind not in allowed or not caller.can_write:
        raise CleanupRefused("curator_write_scope_refused")


def retain_fetch(
    driver, world_id: str, run_id: str, screened: ScreenedSource, *, access: Access | None = None
) -> RetainedFetch:
    if (
        type(screened) is not ScreenedSource
        or screened.dropped_whole
        or len(screened.messages) != 1
    ):
        raise CleanupRefused("fetch_screening_refused")
    message = screened.messages[0]
    if message.at is None or message.source_facts is not None or not message.text.strip():
        raise CleanupRefused("fetch_observation_invalid")

    def work(tx):
        _ordinary(tx)
        at = clock.now(world_id)
        if message.at > at:
            raise CleanupRefused("fetch_observation_in_future")
        found = tx.run(
            "MATCH (c:Connector {type: 'web_pages', provider: 'curator'}) RETURN c.id AS id"
        )
        connector_id = found[0]["id"] if found else new_id(world_id)
        if not found:
            tx.run(
                "CREATE (c:Connector) SET c += $row",
                row={
                    "id": connector_id,
                    "type": "web_pages",
                    "provider": "curator",
                    "batch_rule": "live_page",
                    "version": 1,
                    "touched_at": at,
                },
            )
        source_id, episode_id = new_id(world_id), new_id(world_id)
        write_screened_source(
            tx,
            screened,
            source_id=source_id,
            connector_id=connector_id,
            parser_version="curator-fetch-1",
        )
        tx.run(
            "CREATE (e:Episode) SET e += $row",
            row={
                "id": episode_id,
                "connector_id": connector_id,
                "source_ids": [source_id],
                "first_message_key": message.key,
                "last_message_key": message.key,
                "start_time": message.at,
                "end_time": message.at,
                "read_at": at,
                "cut_rule": "live_page_observation",
                "run_id": run_id,
                "version": 1,
                "touched_at": at,
            },
        )
        return RetainedFetch(source_id, episode_id, message.key, message.at)

    with hold(access, world_id, run_id, "retain_fetch"):
        return WorldWriter(driver, world_id).transaction(work)


def _stamp(world_id, run_id, caller):
    return Stamp(
        world_id,
        clock.now(world_id),
        caller.actor,
        new_id(world_id),
        new_id(world_id),
        run_id=run_id,
    )


def raise_question(
    driver,
    world_id,
    run_id,
    caller,
    question: Question,
    *,
    screener: Screener,
    access: Access | None = None,
) -> str:
    _caller(caller, {"cleanup"})
    if type(question) is not Question or type(screener) is not Screener:
        raise CleanupRefused("question_input_invalid")

    def work(tx):
        _ordinary(tx)
        subjects = sorted(question.subjects)
        items = tx.run("MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id", ids=subjects)
        if {row["id"] for row in items} != set(subjects):
            raise CleanupRefused("question_subject_missing")
        for source_id, judgment_id in question.legwork:
            found = tx.run(
                "MATCH (j:Judgment {id: $id}), (s:Source {id: $source}), "
                "(c:ModelCall), (r:ScreeningReceipt) "
                "WHERE j.model_call_id = c.id AND j.source_id = s.id AND r.source_id = s.id "
                "RETURN j.subject AS subject, j.verdict AS verdict, j.role AS role, "
                "s.forget_state AS state, c.outcome AS outcome",
                id=judgment_id,
                source=source_id,
            )
            if len(found) != 1 or not (
                found[0]["subject"] in {f"item:{item}" for item in subjects}
                and found[0]["verdict"] == "not_about_claim"
                and found[0]["role"] == "first_judge"
                and found[0]["state"] == "kept"
                and found[0]["outcome"] == "ok"
            ):
                raise CleanupRefused("question_legwork_not_settled")
        sources = sorted({pair[0] for pair in question.legwork})
        stamp = _stamp(world_id, run_id, caller)
        existing = tx.run(
            "MATCH (q:Question {state: 'open'}) WHERE q.subject_items = $subjects RETURN q",
            subjects=subjects,
        )
        if existing:
            row = existing[0]["q"]
            combined = sorted(set(row.get("legwork_sources") or []) | set(sources))
            if combined != row.get("legwork_sources"):
                tx.run(
                    "MATCH (q:Question {id: $id}) SET q.legwork_sources = $sources, "
                    "q.version = q.version + 1, q.touched_at = $at",
                    id=row["id"],
                    sources=combined,
                    at=stamp.at,
                )
            return row["id"]
        # Fixed options or same-world item ids only. Arbitrary option prose needs its own value id.
        if any(
            option not in {"confirm", "deny", "check_sources"} | set(subjects)
            for option in question.options
        ):
            raise CleanupRefused("question_option_not_bound")
        text_values = []
        for text in (question.ask, question.reason):
            cleaned = screener.screen_text(text)
            if cleaned.dropped_whole or len(cleaned.messages) != 1:
                raise CleanupRefused("question_screening_refused")
            text_values.append(add_text(tx, stamp, cleaned.messages[0].text))
        question_id = new_id(world_id)
        tx.run(
            "CREATE (q:Question) SET q += $row",
            row={
                "id": question_id,
                "subject_items": subjects,
                "ask": text_values[0],
                "class": question.kind,
                "legwork_sources": sources,
                "legwork_reason": text_values[1],
                "answer_options": json.dumps(question.options),
                "raised_by": "curator",
                "raised_at": stamp.at,
                "priority": question.priority,
                "state": "open",
                "unblocks": subjects,
                "version": 1,
                "touched_at": stamp.at,
            },
        )
        return question_id

    with hold(
        access,
        world_id,
        run_id,
        "raise_question",
        *question.subjects,
        *(p[0] for p in question.legwork),
    ):
        return WorldWriter(driver, world_id).transaction(work)


def close_question(
    driver,
    world_id,
    run_id,
    caller,
    question_id,
    *,
    answer_request_id=None,
    withdraw_reason=None,
    screener=None,
    access: Access | None = None,
):
    """Close only after the interactive writer saved an answer, or a screened withdrawal.

    An interrupted caller can repeat the same request. No close-then-apply window
    exists: an unsaved, paraphrased or unrelated request leaves the question open.
    """
    if (answer_request_id is None) == (withdraw_reason is None):
        raise CleanupRefused("question_close_kind_invalid")
    _caller(caller, {"person", "ai_host"} if answer_request_id else {"cleanup", "person"})

    def work(tx):
        _ordinary(tx)
        rows = tx.run("MATCH (q:Question {id: $id}) RETURN q", id=question_id)
        if len(rows) != 1:
            raise CleanupRefused("question_missing")
        question = rows[0]["q"]
        stamp = _stamp(world_id, run_id, caller)
        if answer_request_id is not None:
            requests = tx.run("MATCH (r:Request {id: $id}) RETURN r", id=answer_request_id)
            row = requests[0]["r"] if len(requests) == 1 else {}
            if not (
                row.get("status") == "saved"
                and row.get("relay") == "answer"
                and question_id in (row.get("about") or [])
                and row.get("source_id")
                and row.get("receipt")
            ):
                raise CleanupRefused("question_answer_not_applied")
            try:
                applied = Receipt.from_json(json.loads(row["receipt"]))
            except (TypeError, ValueError, KeyError):
                raise CleanupRefused("question_answer_receipt_invalid") from None
            if not (
                applied.state == "saved"
                and applied.request_id == answer_request_id
                and applied.commit_id
                and applied.commit_id == row.get("commit_id")
                and applied.change_set_id
                and applied.change_set_id == row.get("change_set_id")
            ):
                raise CleanupRefused("question_answer_receipt_invalid")
            touched = {
                line.entity_id
                for line in applied.lines
                if line.entity_type == "item" and not line.history_only
            }
            if not set(question["subject_items"]).issubset(touched):
                raise CleanupRefused("question_answer_subject_not_applied")
            sources = tx.run(
                "MATCH (s:Source {id: $id}), (c:Connector) WHERE s.connector_id = c.id "
                "RETURN s.forget_state AS state, c.type AS type",
                id=row["source_id"],
            )
            source = sources[0] if len(sources) == 1 else {}
            if source.get("state") != "kept" or source.get("type") not in (
                "person_in_views",
                "ai_host",
            ):
                raise CleanupRefused("question_answer_source_invalid")
            if source["type"] == "ai_host" and not (
                row.get("verbatim") is True and row.get("host_message_id")
            ):
                raise CleanupRefused("question_answer_not_verbatim")
            receipt = json.dumps({"request_id": answer_request_id}, sort_keys=True)
            if question["state"] == "answered" and question.get("receipt") == receipt:
                return question_id
            if question["state"] != "open":
                raise CleanupRefused("question_already_closed")
            columns = {
                "state": "answered",
                "answer_source_id": row["source_id"],
                "applied_at": stamp.at,
                "receipt": receipt,
            }
        else:
            if (
                question["state"] != "open"
                or type(screener) is not Screener
                or not isinstance(withdraw_reason, str)
                or not withdraw_reason.strip()
                or len(withdraw_reason) > 2000
            ):
                raise CleanupRefused("question_withdrawal_invalid")
            screened = screener.screen_text(withdraw_reason)
            if len(screened.messages) != 1 or screened.dropped_whole:
                raise CleanupRefused("question_screening_refused")
            columns = {
                "state": "withdrawn",
                "withdraw_reason": add_text(tx, stamp, screened.messages[0].text),
            }
        tx.run(
            "MATCH (q:Question {id: $id}) SET q += $columns, "
            "q.version = q.version + 1, q.touched_at = $at",
            id=question_id,
            columns=columns,
            at=stamp.at,
        )
        return question_id

    with hold(access, world_id, run_id, "close_question", question_id):
        return WorldWriter(driver, world_id).transaction(work)


def retain_ambiguity(
    driver,
    world_id,
    run_id,
    caller,
    *,
    episode_id,
    source_id,
    locator,
    result: Ambiguity,
    waiting_links: tuple[str, ...],
    access: Access | None = None,
):
    """Persist an unresolved mention; waiting links remain unapplied op identities.

    Resolved links and aliases stay with the D10 owner. This writer never invents
    a link from a score or guesses a new identity when context has no survivor.
    """
    _caller(caller, {"cleanup"})
    if type(result) is not Ambiguity or result.resolved_to is not None:
        raise CleanupRefused("ambiguity_not_unresolved")
    if (
        type(result.candidates) is not tuple
        or len(result.candidates) > 100
        or any(type(value) is not str or not 0 < len(value) <= 128 for value in result.candidates)
        or len(set(result.candidates)) != len(result.candidates)
        or any(rule not in ("participants", "co_mentions", "time") for rule in result.context)
    ):
        raise CleanupRefused("ambiguity_candidates_invalid")
    if not isinstance(locator, str) or not 0 < len(locator) <= 256 or len(waiting_links) > 100:
        raise CleanupRefused("ambiguity_locator_or_links_invalid")
    if any(type(value) is not str or not 0 < len(value) <= 128 for value in waiting_links):
        raise CleanupRefused("ambiguity_waiting_link_invalid")

    def work(tx):
        _ordinary(tx)
        episodes = tx.run(
            "MATCH (e:Episode {id: $episode}), (s:Source {id: $source}), (c:Connector) "
            "WHERE s.id IN e.source_ids AND s.connector_id = c.id "
            "RETURN s.content AS content, s.forget_state AS state, c.type AS type",
            episode=episode_id,
            source=source_id,
        )
        items = tx.run(
            "MATCH (i:Item) WHERE i.id IN $ids RETURN i.id AS id", ids=list(result.candidates)
        )
        if len(episodes) != 1 or {r["id"] for r in items} != set(result.candidates):
            raise CleanupRefused("ambiguity_reference_missing")
        if episodes[0]["state"] != "kept":
            raise CleanupRefused("ambiguity_source_not_kept")
        check_locator(locator, content=episodes[0]["content"], connector_type=episodes[0]["type"])
        existing = tx.run(
            "MATCH (m:UnresolvedMention {episode_id: $episode, locator: $locator}) RETURN m",
            episode=episode_id,
            locator=locator,
        )
        candidates = json.dumps([{"item_id": item} for item in result.candidates], sort_keys=True)
        context = json.dumps({"rules": result.context, "source_id": source_id}, sort_keys=True)
        if existing:
            row = existing[0]["m"]
            if (
                row.get("candidate_item_ids") != candidates
                or row.get("context_used") != context
                or set(row.get("waiting_links") or []) != set(waiting_links)
            ):
                raise CleanupRefused("ambiguity_replay_conflict")
            return row["id"]
        at, mention_id = clock.now(world_id), new_id(world_id)
        tx.run(
            "CREATE (m:UnresolvedMention) SET m += $row",
            row={
                "id": mention_id,
                "episode_id": episode_id,
                "locator": locator,
                "candidate_item_ids": candidates,
                "context_used": context,
                "waiting_links": list(waiting_links),
                "state": "open",
                "expires_at": expiry(at),
                "version": 1,
                "touched_at": at,
            },
        )
        return mention_id

    with hold(
        access, world_id, run_id, "retain_ambiguity", episode_id, source_id, *result.candidates
    ):
        return WorldWriter(driver, world_id).transaction(work)
