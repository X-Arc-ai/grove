"""D9's evidence-derived beliefs and durable contradiction queue.

All handlers run under the batch's world lock. Belief state is never a model score.
Text outside item content is held by HistoryValue, including reasons and questions.
"""

import json
import re

from grove.store.normalise import normalise_phrase
from grove.store.primitives import KIND_BELIEF, MEANING_DESCRIBES, MEANING_SUPERSEDED_BY
from grove.writer import apply as ap
from grove.writer import verify
from grove.writer.clocks import UNKNOWN, Clock, clock_columns, parse_clock, parse_instant, sort_key
from grove.writer.errors import Refused
from grove.writer.history import compact, row_json
from grove.writer.provenance import _relay, connector_type_of

_GET = "MATCH (b:Belief {item_id: $id}) RETURN b"
_SET = "MATCH (b:Belief {item_id: $id}) SET b += $columns"
_NEUTRAL_VERDICTS = frozenset({"complements", "not_about_claim"})
_EVIDENCE = (
    "MATCH (e:Evidence {target_id: $id, active: true}) RETURN e ORDER BY e.source_time, e.id"
)
_DATE_WORDS = re.compile(
    r"\b(?:as\s+of|today|yesterday|tomorrow|currently|now|20\d{2}|19\d{2}|"
    r"january|february|march|april|june|july|august|september|october|november|december)\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    re.I,
)


def refuse(rule: str, message: str) -> None:
    raise Refused("invalid_arguments", rule, message, {})


def get(run, target: str, *, live: bool = False):
    item_id = run.ref(target, "item")
    item = ap._lock_item(run, item_id)
    rows = run.tx.run(_GET, id=item_id)
    if item.get("kind") != KIND_BELIEF or not rows:
        refuse("belief_not_found", "the target is not a belief of this world")
    belief = rows[0]["b"]
    if live and belief["state"] == "superseded":
        refuse("belief_superseded", "a superseded belief has no automatic transition")
    return item, belief


def set_fields(run, belief, fields, reason_id, *, evidence_id=None):
    changed = {key: value for key, value in fields.items() if belief.get(key) != value}
    if not changed:
        return
    for key, value in changed.items():
        run.entry(
            entity_type="belief",
            entity_id=belief["item_id"],
            field=key,
            old=belief.get(key),
            new=value,
            world_time=run.at,
            reason_id=reason_id,
            evidence_id=evidence_id,
        )
    run.tx.run(_SET, id=belief["item_id"], columns={**changed, "touched_at": run.at})
    # Several hooks in one operation share a reason. Rebuild counts the same groups.
    group = (belief["item_id"], reason_id)
    bumped = run.cache.setdefault("belief_bumps", set())
    if group not in bumped:
        run.tx.run(
            "MATCH (b:Belief {item_id: $id}) SET b.version = b.version + 1", id=belief["item_id"]
        )
        bumped.add(group)
    belief.update(changed)


def _rows(run, target):
    return [row["e"] for row in run.tx.run(_EVIDENCE, id=target)]


def _observed(row):
    return row.get("derivation") == "observed" and row.get("source_type") != "planned"


def counts(rows):
    supports = [row for row in rows if row["polarity"] == "supports" and _observed(row)]
    against = [row for row in rows if row["polarity"] == "contradicts"]
    return {
        "supporting_episodes": sorted({row["episode_id"] for row in supports}),
        "contradicting_episodes": sorted({row["episode_id"] for row in against}),
        "last_support_at": max((row["source_time"] for row in supports), default=None),
        "last_contradiction_at": max((row["source_time"] for row in against), default=None),
    }


def _newer(left, right):
    return left is not None and (right is None or left > right)


def _person_override(run, belief, rows):
    held = belief.get("held_by_the_person")
    return held is None or any(
        row.get("source_type") == "confirmed"
        and row.get("derivation") == "observed"
        and _newer(row.get("source_time"), held)
        and verify.can_confirm(run, row, "item", belief["item_id"])
        for row in rows
    )


def cascade_changes(run):
    """Typed D9 events for the accepted D6 adapter; this module never runs a cascade."""
    return tuple(dict(row) for row in run.cache.get("belief_cascade_changes", ()))


def _stage_change(
    run,
    belief_id,
    before,
    after,
    rows,
    reason_id,
    *,
    cause,
    person_correction=False,
    end_clock=None,
):
    if before == after:
        return
    rank = {"planned": 0, "said": 1, "reported_done": 2, "confirmed": 3}
    strongest = max(
        rows,
        key=lambda row: (
            rank.get(row.get("source_type"), -1) if row.get("derivation") == "observed" else -1,
            row.get("source_time") or run.at,
        ),
        default={},
    )
    original = strongest.get("source_type", "said")
    original_derivation = strongest.get("derivation", "inferred")
    # Evidence can be an observed fact while the regularity inferred from it remains an
    # inference. Only a validated personal correction carries that person's own authority.
    derivation = original_derivation if person_correction else "inferred"
    effective = original if derivation == "observed" else min((original, "said"), key=rank.get)
    world_time = (
        sort_key(parse_clock(end_clock)) if end_clock is not None else strongest.get("source_time")
    )
    run.cache.setdefault("belief_cascade_changes", []).append(
        {
            "item_id": belief_id,
            "change_set_id": run.stamp.change_set_id,
            "world_time": world_time,
            "source_type": effective,
            "original_source_type": original,
            "original_derivation": original_derivation,
            "derivation": derivation,
            "terminal": after == "superseded",
            "reopened": before == "superseded" and after != before,
            "person_correction": person_correction,
            "end_clock": end_clock,
            "fields": ["state"],
            "evidence_ids": sorted({row["id"] for row in rows if row.get("id")}),
            "episode_ids": sorted({row["episode_id"] for row in rows if row.get("episode_id")}),
            "reason_id": reason_id,
            "cause": cause,
            "before_state": before,
            "after_state": after,
        }
    )


def _transition(run, belief, state, rows, reason_id):
    episodes = {row["episode_id"] for row in rows}
    previous = set(belief.get("transition_episodes") or [])
    fresh = episodes - previous
    if not fresh:
        refuse("belief_episode_reused", "a transition needs an episode no earlier transition used")
    if not _person_override(run, belief, rows):
        refuse("person_word", "only newer confirmed evidence changes the person's decision")
    changes = {
        "state": state,
        "state_since": run.at,
        "transition_episodes": sorted(previous | episodes),
    }
    if belief.get("held_by_the_person"):
        changes["person_change_notice"] = True
    previous_state = belief["state"]
    set_fields(run, belief, changes, reason_id, evidence_id=rows[-1]["id"] if rows else None)
    trigger_rows = [row for row in rows if row["episode_id"] in fresh]
    _stage_change(
        run,
        belief["item_id"],
        previous_state,
        state,
        trigger_rows,
        reason_id,
        cause="evidence_transition",
    )


def evidence_changed(run, target, reason_id):
    """The writer calls this in the same transaction that adds or ends evidence."""
    found = run.tx.run(_GET, id=target)
    if not found:
        return
    belief = found[0]["b"]
    rows = _rows(run, target)
    measured = counts(rows)
    set_fields(run, belief, measured, reason_id)
    if belief["state"] == "superseded":
        return
    used = set(belief.get("transition_episodes") or [])
    contrary = [
        row
        for row in rows
        if row["polarity"] == "contradicts"
        and row["episode_id"] not in used
        and _newer(row["source_time"], measured["last_support_at"])
    ]
    if contrary and belief["state"] in ("tentative", "established"):
        eligible = [row for row in contrary if _person_override(run, belief, [row])]
        if eligible:
            _transition(run, belief, "weakening", eligible, reason_id)
        return
    if belief["state"] == "weakening":
        contradicting_sources = {
            row["source_id"] for row in rows if row["polarity"] == "contradicts"
        }
        supports = [
            row
            for row in rows
            if row["polarity"] == "supports"
            and _observed(row)
            and row["episode_id"] not in used
            and row["source_id"] not in contradicting_sources
            and _newer(row["source_time"], measured["last_contradiction_at"])
            and _person_override(run, belief, [row])
        ]
        if supports:
            _transition(run, belief, "established", supports, reason_id)


def _person_rows(run, plans, *, correction=False):
    accepted = []
    for plan in plans:
        row = plan.columns
        found = run.tx.run(
            "MATCH (s:Source {id: $id}) MATCH (c:Connector {id: s.connector_id}) RETURN s, c",
            id=row["source_id"],
        )
        if not found or row.get("derivation") != "observed" or row.get("speaker_kind") != "person":
            continue
        kind = connector_type_of(found[0]["c"])
        relay, verbatim = _relay(run.tx, row["source_id"])
        if kind == "person_in_views" and run.caller_kind == "person":
            accepted.append({**row, "id": plan.id})
        elif kind == "ai_host" and verbatim and (not correction or relay == "correction"):
            # Stored relay authority applies to the exact quoted source, not an op flag.
            accepted.append({**row, "id": plan.id})
    if not accepted:
        raise Refused(
            "verbatim_required",
            "verbatim_required",
            "the person's words need their stored source and authenticated relay",
            {},
        )
    return accepted


def _timeless(title, body=None):
    if _DATE_WORDS.search(title) or (body and _DATE_WORDS.search(body)):
        refuse("belief_timeless", "a belief states its regularity without dates or as-of words")
    if not normalise_phrase(title):
        refuse("belief_timeless", "a belief needs a non-blank timeless statement")


def _field_acceptance(run, op, subjects):
    if not op.get("externally_testable"):
        return
    if not op.get("recheck_by") or not op.get("retest_how"):
        refuse("belief_retest_required", "an externally testable belief needs a horizon and method")
    subject = op.get("field_subject", "")
    rows = (
        run.tx.run(
            "MATCH (v:HistoryValue {id: $id}) RETURN v.value AS value",
            id=subject.removeprefix("value:"),
        )
        if subject.startswith("value:")
        else []
    )
    expected = {
        "title": op["title"],
        "subject_refs": sorted(subjects),
        "externally_testable": True,
        "retest_how": op["retest_how"],
    }
    try:
        matched = bool(rows and json.loads(rows[0]["value"]) == expected)
    except (ValueError, TypeError):
        matched = False
    judges = ap._judgments(run, op.get("judgment_ids") or [])
    if (
        not matched
        or not judges
        or any(
            row["role"] != "field_fit" or row["subject"] != subject or row["verdict"] != "fits"
            for row in judges
        )
    ):
        refuse(
            "belief_field_judgment", "the external-test declaration needs a bound field-fit verdict"
        )
    _judge_pair(judges, "fits")


def _anchor(run, belief_id, subject_id, reason_id):
    row = {
        "id": run.new_id(),
        "from_item": belief_id,
        "to_item": subject_id,
        "meaning": MEANING_DESCRIBES,
        **clock_columns("became_true", UNKNOWN),
        "active_key": f"{run.stamp.world_id}|{belief_id}|{MEANING_DESCRIBES}|{subject_id}",
        "recorded": run.at,
        "version": 1,
        "touched_at": run.at,
        "provenance_reason": reason_id,
        "derivation": "inferred",
    }
    run.tx.run(ap._ADD_LINK, a=belief_id, b=subject_id, row=row)
    run.entry(
        entity_type="link",
        entity_id=row["id"],
        field="row",
        new=row_json("links", row, run.stamp.world_id),
        world_time=run.at,
        reason_id=reason_id,
    )


def mint(run, index, op):
    _timeless(op["title"], op.get("body"))
    subjects = sorted({run.ref(value, "item") for value in op["subject_refs"]})
    if not subjects:
        refuse("belief_subject", "a belief needs at least one subject")
    for subject in subjects:
        ap._lock_item(run, subject)
    normal = normalise_phrase(op["title"])
    existing = run.tx.run(
        "MATCH (b:Belief) MATCH (i:Item {id: b.item_id}) "
        "WHERE b.state <> 'superseded' RETURN b, i.title AS title"
    )
    for entry in existing:
        if normalise_phrase(entry["title"]) != normal:
            continue
        anchors = run.tx.run(
            "MATCH (:Item)-[l:LINK {from_item: $id, meaning: $meaning}]->(:Item) "
            "WHERE l.active_key IS NOT NULL RETURN l.to_item AS id",
            id=entry["b"]["item_id"],
            meaning=MEANING_DESCRIBES,
        )
        if sorted({row["id"] for row in anchors}) == subjects:
            refuse("belief_duplicate", "an active belief already states this on these subjects")
    plans = ap._plans(run, op["evidence_refs"])
    if any(plan.polarity != "supports" for plan in plans):
        refuse("belief_mint_support", "a mint takes supporting evidence only")
    measured = counts([{**plan.columns, "polarity": plan.polarity} for plan in plans])
    generalisation = bool(op.get("generalisation"))
    if generalisation:
        _person_rows(run, plans)
    elif len(measured["supporting_episodes"]) < 2:
        refuse("belief_earning", "a tentative belief needs two distinct supporting episodes")
    _field_acceptance(run, op, subjects)
    support_items = []
    for ref in op["evidence_refs"]:
        if ref.get("evidence_id"):
            rows = run.tx.run(
                "MATCH (e:Evidence {id: $id}) RETURN e.target_id AS id, e.target_type AS type",
                id=ref["evidence_id"],
            )
            support_items.extend(row["id"] for row in rows if row["type"] in ("item", "belief"))
    temp = op.get("temp_id") or "$internal_belief"
    before = set(run.temps)
    run.cache["belief_internal"] = True
    try:
        ap._create_item(
            run,
            index,
            {
                "op": "create_item",
                "op_id": op["op_id"],
                "kind": KIND_BELIEF,
                "title": op["title"],
                "body": op.get("body"),
                "became_true": "unknown",
                "evidence_refs": op["evidence_refs"],
                "reason": op["reason"],
                "temp_id": temp,
                "recheck_by": op.get("recheck_by"),
                "recheck_how": op.get("retest_how"),
            },
        )
    finally:
        run.cache.pop("belief_internal", None)
    belief_id = run.temps[temp][1]
    run.cache.setdefault("minted_beliefs", set()).add(belief_id)
    if temp not in before and "temp_id" not in op:
        del run.temps[temp]
    reason_id = run.text(op["reason"])
    row = compact(
        {
            "item_id": belief_id,
            "state": "tentative",
            "state_since": run.at,
            "origin": "persons_generalisation" if generalisation else "earned_from_episodes",
            **measured,
            "externally_testable": bool(op.get("externally_testable")),
            "retest_how": ap._json(op.get("retest_how")),
            "retrieved": 0,
            "cited": 0,
            "transition_episodes": measured["supporting_episodes"],
            "support_items": sorted(set(support_items)),
            "version": 1,
            "touched_at": run.at,
        }
    )
    run.tx.run("CREATE (b:Belief) SET b += $row", row=row)
    run.entry(
        entity_type="belief",
        entity_id=belief_id,
        field="row",
        new=row_json("beliefs", row, run.stamp.world_id),
        world_time=run.at,
        reason_id=reason_id,
    )
    for subject in subjects:
        _anchor(run, belief_id, subject, reason_id)
    run.summaries[index] = f"belief {run.label(op.get('temp_id') or belief_id)} (tentative)"


def _attach_refs(run, target, refs, reason_id):
    plans = ap._plans(run, refs)
    if any(plan.polarity == "contradicts" for plan in plans):
        refuse(
            "belief_contradiction_gate",
            "attach contradicting evidence with recorded judgments first",
        )
    ap._write_evidence(run, plans, "belief", target, reason_id)
    return plans


def transition(run, index, op):
    item, belief = get(run, op["target"], live=True)
    ap._check_version(op, belief)
    reason_id = run.text(op["reason"])
    _attach_refs(run, item["id"], op["evidence_refs"], reason_id)
    belief = run.tx.run(_GET, id=item["id"])[0]["b"]
    rows = _rows(run, item["id"])
    measured = counts(rows)
    name = op["op"]
    if name == "promote_belief":
        if belief["state"] != "tentative":
            refuse("belief_state", "promotion starts from tentative")
        required = 2 if belief["origin"] == "persons_generalisation" else 3
        if len(measured["supporting_episodes"]) < required:
            refuse(
                "belief_earning",
                "promotion needs three episodes or generalisation plus confirmation",
            )
        if _newer(measured["last_contradiction_at"], measured["last_support_at"]):
            refuse("belief_contradicted", "a newer contradiction prevents promotion")
        selected = [row for row in rows if row["polarity"] == "supports" and _observed(row)]
        _transition(run, belief, "established", selected, reason_id)
    else:
        # Evidence attachment already performed the only permitted mechanical change.
        expected = "weakening" if name == "weaken_belief" else "established"
        if belief["state"] != expected:
            refuse("belief_evidence", "the attached evidence does not earn this transition")
    run.summaries[index] = f"belief {run.label(op['target'])}: {belief['state']}"


def _end(
    run,
    index,
    item,
    belief,
    end_class,
    reason_id,
    *,
    stopped=None,
    successor=None,
    evidence=(),
    person_correction=False,
):
    if end_class not in ("world_changed", "wrong", "replaced", "person_retired"):
        refuse("belief_end_class", "a belief needs a declared ending class")
    fields = {
        "state": "superseded",
        "state_since": run.at,
        "end_class": end_class,
        "successor": successor,
    }
    before_state = belief["state"]
    set_fields(run, belief, fields, reason_id)
    world_end = stopped if stopped is not None else Clock("instant", run.at, run.at)
    columns = {"end_class": "refuted" if end_class == "wrong" else "superseded"}
    run.entry(
        entity_type="item",
        entity_id=item["id"],
        field="end_class",
        old=item.get("end_class"),
        new=columns["end_class"],
        world_time=run.at,
        reason_id=reason_id,
    )
    if end_class == "world_changed":
        if ap.ends_before_start(ap.clock_from_row(item, "became_true"), world_end):
            refuse("end_before_start", "a belief cannot stop before it began")
        run.entry(
            entity_type="item",
            entity_id=item["id"],
            field="stopped_being_true",
            old=ap.clock_from_row(item, "stopped_being_true").to_json()
            if ap.clock_from_row(item, "stopped_being_true")
            else None,
            new=world_end.to_json(),
            world_time=run.at,
            reason_id=reason_id,
        )
        columns.update(clock_columns("stopped_being_true", world_end))
    run.tx.run(ap._SET_ITEM, id=item["id"], columns=columns)
    ap._bump(run, ap._BUMP_ITEM, item["id"])
    run.touch(item["id"], index)
    _stage_change(
        run,
        item["id"],
        before_state,
        "superseded",
        evidence,
        reason_id,
        cause="person_correction" if person_correction else "belief_end",
        person_correction=person_correction,
        end_clock=world_end.to_json() if end_class == "world_changed" else None,
    )
    if successor:
        from grove.writer.composite import _successor_link

        _successor_link(
            run,
            item["id"],
            successor,
            MEANING_SUPERSEDED_BY,
            Clock("instant", run.at, run.at),
            [],
            reason_id,
            run.at,
        )


def end(run, index, op):
    item, belief = get(run, op["target"], live=True)
    ap._check_version(op, belief)
    plans = ap._plans(run, op["evidence_refs"])
    ap._settle(run, plans, item["id"])
    rows = [{**plan.columns, "id": plan.id} for plan in plans]
    if any(plan.polarity != "supports" for plan in plans):
        refuse("belief_contradiction_gate", "an ending cannot bypass the contradiction judge gate")
    if not rows:
        refuse("belief_end_evidence", "an ending records the evidence that justifies it")
    person = run.caller_kind == "person"
    if person:
        _person_rows(run, plans, correction=True)
    else:
        ranked = {"planned": 0, "said": 1, "reported_done": 2, "confirmed": 3}
        standing = [row for row in _rows(run, item["id"]) if row["polarity"] == "supports"]

        def rank(row):
            return ranked.get(row.get("source_type"), -1) if _observed(row) else -1

        prior_rank = max((rank(row) for row in standing), default=-1)
        if not any(
            rank(row) >= prior_rank and _newer(row["source_time"], belief.get("last_support_at"))
            for row in rows
        ):
            refuse(
                "belief_end_evidence",
                "an automatic ending needs newer evidence of equal or higher rank",
            )
    if not person and not _person_override(run, belief, rows):
        refuse("person_word", "only newer confirmed evidence changes the person's decision")
    reason_id = run.text(op["reason"])
    successor = None
    if op["op"] == "supersede_belief":
        successor, _ = get(run, op["successor"], live=True)
        successor = successor["id"]
        if successor == item["id"]:
            refuse("belief_successor", "a belief cannot replace itself")
    if belief.get("held_by_the_person"):
        set_fields(run, belief, {"person_change_notice": True}, reason_id)
    _end(
        run,
        index,
        item,
        belief,
        op["end_class"],
        reason_id,
        stopped=parse_clock(op["stopped_being_true"]) if "stopped_being_true" in op else None,
        successor=successor,
        evidence=rows,
        person_correction=person,
    )
    # Preserve the evidence behind the ending as well as the state inverse.
    ap._write_evidence(run, plans, "belief", item["id"], reason_id)


def correct(run, index, op):
    item, belief = get(run, op["target"])
    ap._check_version(op, belief)
    plans = ap._plans(run, op["evidence_refs"])
    # Event provenance must use the retained row ID when this citation is already known.
    ap._settle(run, plans, item["id"])
    person = _person_rows(run, plans, correction=True)
    reason_id = run.text(op["reason"])
    held = min(run.at, max(row["source_time"] for row in person))
    action = op["correction"]
    before_state = belief["state"]
    successor = None
    if action == "restate":
        if not op.get("title"):
            refuse("belief_statement", "a restatement needs the person's new statement")
        if run.caller_kind == "ai_host":
            words = [
                row["content"]
                for row in run.tx.run(
                    "MATCH (s:Source) WHERE s.id IN $ids RETURN s.content AS content",
                    ids=[row["source_id"] for row in person],
                )
                if row.get("content")
            ]
            if not any(
                op["title"] in text and (not op.get("body") or op["body"] in text) for text in words
            ):
                refuse(
                    "verbatim_required", "a host restatement preserves the person's quoted words"
                )
        anchors = run.tx.run(
            "MATCH (:Item)-[l:LINK {from_item: $id, meaning: $meaning}]->(:Item) "
            "WHERE l.active_key IS NOT NULL RETURN l.to_item AS id",
            id=item["id"],
            meaning=MEANING_DESCRIBES,
        )
        mint(
            run,
            index,
            {
                "op": "mint_belief",
                "op_id": op["op_id"],
                "title": op["title"],
                "body": op.get("body"),
                "subject_refs": [row["id"] for row in anchors],
                "evidence_refs": op["evidence_refs"],
                "reason": op["reason"],
                "generalisation": True,
                "temp_id": "$person_successor",
            },
        )
        successor = run.temps.pop("$person_successor")[1]
        new = run.tx.run(_GET, id=successor)[0]["b"]
        set_fields(
            run,
            new,
            {
                "origin": "persons_correction",
                "held_by_the_person": held,
                "state": "established",
                "state_since": run.at,
            },
            reason_id,
        )
    if action == "right":
        # Restoring the old statement also ends the lineage that made reads prefer its successor.
        outgoing = run.tx.run(
            "MATCH (:Item)-[l:LINK {from_item:$id, meaning:$meaning}]->(:Item) "
            "WHERE l.active_key IS NOT NULL RETURN l",
            id=item["id"],
            meaning=MEANING_SUPERSEDED_BY,
        )
        for entry in outgoing:
            link = entry["l"]
            ap._end_entry(
                run,
                "link",
                link["id"],
                link.get("end_class"),
                "refuted",
                run.at,
                reason_id,
                person[0]["id"],
            )
            run.tx.run(
                ap._SET_LINK, id=link["id"], columns={"active_key": None, "end_class": "refuted"}
            )
            ap._bump(run, ap._BUMP_LINK, link["id"])
        if item.get("end_class"):
            run.entry(
                entity_type="item",
                entity_id=item["id"],
                field="end_class",
                old=item["end_class"],
                new=None,
                world_time=run.at,
                reason_id=reason_id,
            )
            stopped = ap.clock_from_row(item, "stopped_being_true")
            if stopped:
                run.entry(
                    entity_type="item",
                    entity_id=item["id"],
                    field="stopped_being_true",
                    old=stopped.to_json(),
                    new=None,
                    world_time=run.at,
                    reason_id=reason_id,
                )
            run.tx.run(
                ap._SET_ITEM,
                id=item["id"],
                columns={"end_class": None, **clock_columns("stopped_being_true", None)},
            )
            ap._bump(run, ap._BUMP_ITEM, item["id"])
        set_fields(
            run,
            belief,
            {"state": "established", "state_since": run.at, "end_class": None, "successor": None},
            reason_id,
        )
    else:
        ending = {"not_any_more": "world_changed", "wrong": "wrong", "restate": "replaced"}[action]
        _end(
            run,
            index,
            item,
            belief,
            ending,
            reason_id,
            stopped=parse_clock(op["stopped_being_true"]) if "stopped_being_true" in op else None,
            successor=successor,
            evidence=person,
            person_correction=True,
        )
    if action == "right":
        _stage_change(
            run,
            item["id"],
            before_state,
            "established",
            person,
            reason_id,
            cause="person_correction",
            person_correction=True,
        )
    set_fields(run, belief, {"held_by_the_person": held, "person_change_notice": False}, reason_id)
    # A person's correction is its own observed evidence, not a model's inferred paraphrase.
    ap._write_evidence(run, plans, "belief", item["id"], reason_id)


def merge(run, index, op):
    survivor, kept = get(run, op["survivor"], live=True)
    absorbed, removed = get(run, op["absorbed"], live=True)
    if survivor["id"] == absorbed["id"]:
        refuse("belief_merge_self", "a belief cannot merge with itself")
    judges = ap._judgments(run, op["judgment_ids"])
    pair = "|".join(sorted((f"belief:{survivor['id']}", f"belief:{absorbed['id']}")))
    if any(row["role"] != "same_belief" or row["subject"] != pair for row in judges):
        refuse("judgment_subject", "a merge needs verdicts on this belief pair")
    _judge_pair(judges, "same")
    if kept.get("held_by_the_person") or removed.get("held_by_the_person"):
        refuse("person_word", "an automatic merge cannot replace the person's decision")
    reason_id = run.text(op["reason"])
    rows = _rows(run, absorbed["id"])
    # Preserve the original evidence and provenance by copying through the writer's resolver.
    for row in rows:
        if row["polarity"] == "contradicts":
            # Two same-belief judgments do not judge an individual contradiction.
            refuse("belief_merge_contradiction", "resolve contradicting evidence before a merge")
    _attach_refs(
        run,
        survivor["id"],
        [{"evidence_id": row["id"], "polarity": "supports"} for row in rows],
        reason_id,
    )
    anchors = run.tx.run(
        "MATCH (:Item)-[l:LINK {from_item: $id, meaning: $meaning}]->(:Item) "
        "WHERE l.active_key IS NOT NULL RETURN l.to_item AS id",
        id=absorbed["id"],
        meaning=MEANING_DESCRIBES,
    )
    kept_anchors = {
        row["id"]
        for row in run.tx.run(
            "MATCH (:Item)-[l:LINK {from_item: $id, meaning: $meaning}]->(:Item) "
            "WHERE l.active_key IS NOT NULL RETURN l.to_item AS id",
            id=survivor["id"],
            meaning=MEANING_DESCRIBES,
        )
    }
    for row in anchors:
        if row["id"] not in kept_anchors:
            _anchor(run, survivor["id"], row["id"], reason_id)
    _end(
        run,
        index,
        absorbed,
        removed,
        "replaced",
        reason_id,
        successor=survivor["id"],
        evidence=rows,
    )


def _judge_pair(judges, verdict):
    calls = {row.get("model_call_id") for row in judges if row.get("model_call_id")}
    models = {row.get("model") for row in judges if row.get("model")}
    correlated = any(row.get("correlated") for row in judges)
    if (
        len({row["id"] for row in judges}) < 2
        or len(calls) < 2
        or (len(models) < 2 and not correlated)
    ):
        raise Refused(
            "second_judge_required",
            "two_models",
            "a gate needs two distinct judge calls, including a declared fallback",
            {},
        )
    values = {row["verdict"] for row in judges}
    if verdict == "contradicts" and len(values) == 1 and values != {verdict}:
        raise Refused(
            "judge_not_supports", "judge_not_contradicts", "the judges do not say contradicts", {}
        )
    if verdict is not None and values != {verdict}:
        raise Refused("judges_disagree", "judges_disagree", "the recorded judges do not agree", {})
    roles = {row.get("role") for row in judges}
    if roles not in (
        {"first_judge", "second_judge"},
        {"field_fit"},
        {"same_belief"},
        {"contradicts_or_complements"},
    ):
        refuse("judgment_role", "the two calls must have first and second judge roles")


def retest(run, index, op):
    item, belief = get(run, op["target"], live=True)
    if not belief.get("externally_testable"):
        refuse("belief_not_testable", "the belief declares no external test")
    pieces = verify._pieces(
        run,
        op,
        f"item:{item['id']}",
        run.caller_kind,
        allowed_verdicts=frozenset({"supports", "contradicts", "complements", "not_about_claim"}),
    )
    verify._second(run, op, f"item:{item['id']}", pieces)
    verdicts = {piece.verdict for piece in pieces}
    verdicts.update(j["verdict"] for piece in pieces for j in piece.second)
    if not verdicts <= {"supports", "contradicts", "complements", "not_about_claim"}:
        refuse("belief_retest_unrelated", "the re-test must judge the declared regularity")
    neutral = verdicts <= _NEUTRAL_VERDICTS
    disagreement = len(verdicts) > 1 and not neutral
    if neutral:
        outcome = "complements" if "complements" in verdicts else "not_about_claim"
    else:
        outcome = "disagreement" if disagreement else next(iter(verdicts))
    for piece in pieces:
        if not piece.second:
            refuse("second_judge_required", "a belief re-test needs the recorded second opinion")
        # Different neutral labels agree about absence of support or contradiction. Keep the
        # original verdicts and validate the same distinct-call/role perimeter as every check.
        _judge_pair([piece.judgment, *piece.second], None if len(verdicts) > 1 else piece.verdict)
    accepted = []
    records = (
        run.tx.run("MATCH (r:RunRecord {id: $id}) RETURN r.started AS started", id=run.stamp.run_id)
        if run.stamp.run_id
        else []
    )
    since = records[0]["started"] if records else item.get("last_confirmed") or item.get("recorded")
    method = belief.get("retest_how")
    if isinstance(method, str):
        method = json.loads(method)
    if not isinstance(method, dict) or not method.get("connector") or not method.get("locator"):
        refuse("belief_retest_method", "a re-test needs the declared source and locator")
    for piece in pieces:
        source, connector, _ = verify._source_and_connector(run, piece.columns["source_id"])
        if (
            connector["id"] != method["connector"]
            or source.get("external_locator") != method["locator"]
        ):
            refuse("belief_retest_method", "the read must use the declared source and locator")
        capability = verify.capability_of(run, piece.columns, "item", item)
        fresh = piece.fetched_at is not None and since is not None and piece.fetched_at >= since
        if (
            piece.columns["derivation"] == "observed"
            and piece.columns["source_type"] == "confirmed"
            and capability.capability == "confirms"
            and fresh
            and piece.spoken_for >= since
        ):
            accepted.append(piece)
    if len(accepted) != len(pieces) or not accepted:
        refuse(
            "belief_retest_unconfirmed",
            "a re-test needs a fresh capable source for every judged piece",
        )
    horizon = parse_instant(op["recheck_by"])
    if horizon <= run.at:
        refuse("belief_retest_horizon", "the new re-check time must follow this pass")
    reason_id = run.text(op["reason"])
    newest = max(piece.spoken_for for piece in accepted)
    evidence_rows = []
    for piece in accepted:
        judgment_ids = [piece.judgment["id"], *[row["id"] for row in piece.second]]
        if disagreement:
            raw = op["pieces"][piece.index]
            fresh_ref = (
                {"fresh_evidence_id": raw["evidence_id"]}
                if "evidence_id" in raw
                else {"ev_id": raw["ev_id"]}
            )
            open_contradiction(
                run,
                index,
                {
                    "op": "open_contradiction",
                    "op_id": op["op_id"],
                    "target": item["id"],
                    "judgment_ids": judgment_ids,
                    "reason": op["reason"],
                    **fresh_ref,
                },
            )
            continue
        if outcome in {"complements", "not_about_claim"}:
            continue
        plan = ap._Plan(
            run.new_id(), piece.verdict, {**piece.columns, "judgment_ids": sorted(judgment_ids)}
        )
        ap._write_evidence(run, [plan], "belief", item["id"], reason_id)
        evidence_rows.extend(
            row["e"] for row in run.tx.run("MATCH (e:Evidence {id: $id}) RETURN e", id=plan.id)
        )
    belief = run.tx.run(_GET, id=item["id"])[0]["b"]
    set_fields(
        run,
        belief,
        {
            "last_retest_outcome": outcome,
            "last_retested_at": run.at,
            "last_retest_reason": reason_id,
            "last_retest_source_ids": sorted({piece.columns["source_id"] for piece in accepted}),
            "last_retest_judgment_ids": sorted(
                {row["id"] for piece in accepted for row in [piece.judgment, *piece.second]}
            ),
        },
        reason_id,
    )
    positive = not disagreement and verdicts == {"supports"}
    if positive and belief["state"] == "weakening":
        if not _newer(newest, belief.get("last_contradiction_at")):
            refuse("belief_retest_old", "a re-test must speak after the contradiction")
        _transition(run, belief, "established", evidence_rows, reason_id)
    changes = {"recheck_by": horizon}
    if positive:
        changes["last_confirmed"] = newest
    for field, value in changes.items():
        run.entry(
            entity_type="item",
            entity_id=item["id"],
            field=field,
            old=item.get(field),
            new=value,
            world_time=newest,
            reason_id=reason_id,
        )
    run.tx.run(ap._SET_ITEM, id=item["id"], columns=changes)
    ap._bump(run, ap._BUMP_ITEM, item["id"])


def open_contradiction(run, index, op):
    target = run.ref(op["target"], "any")
    ap._lock_item(run, target)
    if op.get("fresh_evidence_id"):
        fresh = ap._existing_evidence(run, op["fresh_evidence_id"])
    else:
        fresh = run.observation(op["ev_id"])
    judgments = ap._judgments(run, op.get("judgment_ids") or [])
    if judgments:
        if {row["verdict"] for row in judgments} <= _NEUTRAL_VERDICTS:
            refuse("belief_complement", "neutral readings do not enter the contradiction queue")
        for row in judgments:
            if row.get("subject") not in (f"belief:{target}", f"item:{target}"):
                refuse("judgment_subject", "the judgment names another claim")
            if not verify._matches(row, op.get("fresh_evidence_id"), fresh):
                refuse("judgment_evidence", "the judgment names another source or locator")
    prior = run.tx.run(
        "MATCH (c:Contradiction {target: $target, status: 'open'}) "
        "WHERE c.fresh_source_id = $source AND c.fresh_locator = $locator RETURN c",
        target=target,
        source=fresh["source_id"],
        locator=fresh["locator"],
    )
    if prior:
        if op.get("temp_id"):
            run.temps[op["temp_id"]] = ("contradiction", prior[0]["c"]["id"])
        return
    changes = run.tx.run(
        "MATCH (e:ChangeEntry {entity_id: $id}) RETURN max(e.sequence) AS n", id=target
    )
    row = compact(
        {
            "id": run.new_id(),
            "target": target,
            "standing_target_id": target,
            "standing_change_id": changes[0]["n"] or 0,
            "fresh_evidence_id": op.get("fresh_evidence_id"),
            "fresh_source_id": fresh["source_id"],
            "fresh_locator": fresh["locator"],
            "fresh_episode_id": fresh["episode_id"],
            "fresh_source_time": fresh["source_time"],
            "fresh_source_type": fresh["source_type"],
            "fresh_derivation": fresh["derivation"],
            "judgment_ids": op.get("judgment_ids") or [],
            "detected_by": run.stamp.actor,
            "detected_at": run.at,
            "request_id": run.stamp.request_id,
            "status": "open",
            "why": run.text(op["reason"]),
            "version": 1,
            "touched_at": run.at,
        }
    )
    run.tx.run("CREATE (c:Contradiction) SET c += $row", row=row)
    if op.get("temp_id"):
        run.temps[op["temp_id"]] = ("contradiction", row["id"])


def resolve_contradiction(run, index, op):
    row_id = run.ref(op["contradiction"], "contradiction")
    rows = run.tx.run("MATCH (c:Contradiction {id: $id}) RETURN c", id=row_id)
    if not rows or rows[0]["c"]["status"] != "open":
        refuse("contradiction_not_open", "the contradiction is not open in this world")
    row = rows[0]["c"]
    done = run.cache.get("done_ops", {})
    if any(value not in done for value in op["operation_ids"]):
        refuse(
            "contradiction_operations", "resolving operations must precede resolution in this batch"
        )
    outcome = op["outcome"]
    if outcome == "fresh_wins" and not any(
        done[value].get("target") == row["target"] or done[value].get("absorbed") == row["target"]
        for value in op["operation_ids"]
    ):
        refuse("contradiction_operations", "fresh evidence wins through an operation in this batch")
    reason_id = run.text(op["reason"])
    question_id = None
    if outcome == "to_question":
        if not op.get("question") or len(op.get("options") or []) != 2:
            refuse("contradiction_question", "a question needs its text and both readings")
        sources = op.get("checked_source_ids") or []
        if not sources:
            refuse("question_legwork", "both readings must survive a recorded source check")
        if op["options"][0].strip() == op["options"][1].strip():
            refuse("contradiction_question", "the two readings must differ")
        for source in sources:
            if not run.tx.run("MATCH (s:Source {id: $id}) RETURN s.id AS id", id=source):
                refuse("question_source", "legwork cites stored sources of this world")
        question_id = run.new_id()
        question = {
            "id": question_id,
            "subject_items": [row["target"]],
            "ask": run.text(op["question"]),
            "class": "fact_only_the_person_holds",
            "legwork_sources": sources,
            "legwork_reason": reason_id,
            "answer_options": json.dumps(
                [{"value_id": run.text(value)} for value in op["options"]]
            ),
            "raised_by": run.stamp.actor,
            "raised_at": run.at,
            "priority": 1,
            "state": "open",
            "version": 1,
            "touched_at": run.at,
        }
        run.tx.run("CREATE (q:Question) SET q += $row", row=compact(question))
    run.tx.run(
        "MATCH (c:Contradiction {id: $id}) SET c.status = $status, c.resolution = $reason, "
        "c.resolution_outcome = $outcome, c.resolution_operation_ids = $ops, "
        "c.resolved_at = $at, c.resolved_by = $actor, c.question_id = $question, "
        "c.version = c.version + 1, c.touched_at = $at",
        id=row_id,
        status="with_the_person" if question_id else "resolved",
        reason=reason_id,
        outcome=outcome,
        ops=op["operation_ids"],
        at=run.at,
        actor=run.stamp.actor,
        question=question_id,
    )


def queue_support_changes(run):
    from grove.writer.composite import STILL_MARKERS

    changed = run.tx.run(
        "MATCH (e:ChangeEntry {change_set_id: $id, entity_type: 'item'}) "
        "WHERE e.field <> 'row' AND NOT e.field IN $markers AND e.history_only = false "
        "RETURN e.entity_id AS id, max(e.sequence) AS sequence",
        id=run.stamp.change_set_id,
        markers=sorted(STILL_MARKERS),
    )
    evidence_changes = run.tx.run(
        "MATCH (e:ChangeEntry {change_set_id: $id, entity_type: 'evidence', field: 'row'}) "
        "MATCH (v:Evidence {id: e.entity_id}) WHERE v.target_type = 'item' "
        "RETURN v.target_id AS id, max(e.sequence) AS sequence",
        id=run.stamp.change_set_id,
    )
    by_target = {}
    for cause in [*changed, *evidence_changes]:
        by_target[cause["id"]] = max(by_target.get(cause["id"], 0), cause["sequence"])
    for target, sequence in by_target.items():
        cause = {"id": target, "sequence": sequence}
        beliefs = run.tx.run(
            "MATCH (b:Belief) WHERE b.state <> 'superseded' AND NOT b.item_id IN $minted AND ("
            "$id IN coalesce(b.support_items, []) OR EXISTS { MATCH (:Item)-[l:LINK]->(:Item) "
            "WHERE l.from_item = b.item_id AND l.to_item = $id "
            "AND l.meaning = $meaning AND l.active_key IS NOT NULL }) "
            "RETURN b.item_id AS id",
            id=cause["id"],
            meaning=MEANING_DESCRIBES,
            minted=sorted(run.cache.get("minted_beliefs", set())),
        )
        for belief in beliefs:
            run.tx.run(
                "MERGE (s:SupportChanged {belief_id: $id}) "
                "ON CREATE SET s.version = 0 SET s.cause_change_id = $cause, "
                "s.queued_at = $at, s.taken_at = null, "
                "s.version = s.version + 1, s.touched_at = $at",
                id=belief["id"],
                cause=cause["sequence"],
                at=run.at,
            )


def resolve_support_change(run, index, op):
    target = run.ref(op["target"], "item")
    rows = run.tx.run("MATCH (s:SupportChanged {belief_id: $id}) RETURN s", id=target)
    if not rows or rows[0]["s"].get("taken_at") is not None:
        refuse("support_change_missing", "the belief has no pending support change")
    if rows[0]["s"]["cause_change_id"] != op["cause_change_id"]:
        refuse("version_conflict", "the supporting item changed after this pass read it")
    judges = ap._judgments(run, op["judgment_ids"])
    if any(
        row["subject"] not in (f"belief:{target}", f"item:{target}")
        or row.get("at") is None
        or row["at"] < rows[0]["s"]["queued_at"]
        for row in judges
    ):
        refuse(
            "support_change_judgment",
            "the re-judgment must read this belief after its support changed",
        )
    verdict = (
        "supports"
        if op["disposition"] == "still_holding"
        else judges[0]["verdict"]
        if judges
        else "supports"
    )
    _judge_pair(judges, verdict)
    if op["disposition"] == "revised":
        done = run.cache["done_ops"]
        if not any(
            value in done and done[value].get("target") == target
            for value in op.get("operation_ids", [])
        ):
            refuse(
                "support_change_revision",
                "a revised disposition cites an earlier operation on this belief",
            )
    run.tx.run(
        "MATCH (s:SupportChanged {belief_id: $id}) SET s.taken_at = $at, "
        "s.disposition = $disposition, s.reason = $reason, s.gap_reason = $gap, "
        "s.version = s.version + 1, s.touched_at = $at",
        id=target,
        at=run.at,
        disposition=op["disposition"],
        reason=run.text(op["reason"]),
        gap=run.text(op["gap_reason"]) if op.get("gap_reason") else None,
    )


def guard_consolidation(run, index, op):
    if index != 0 or op["run_id"] != run.stamp.run_id:
        refuse("consolidation_run", "the guard must be first and name this active pass")
    records = run.tx.run("MATCH (r:RunRecord {id: $id}) RETURN r", id=op["run_id"])
    if (
        not records
        or records[0]["r"].get("agent") != "connector"
        or records[0]["r"].get("finished")
    ):
        refuse("consolidation_run", "the named Connector pass is not active")
    current = {
        row["id"]
        for row in run.tx.run("MATCH (c:Contradiction {status: 'open'}) RETURN c.id AS id")
    }
    if current != set(op["contradiction_ids"]):
        refuse("consolidation_stale", "the contradiction queue changed after the packet read")
    pending = {
        (row["id"], row["cause"])
        for row in run.tx.run(
            "MATCH (s:SupportChanged) WHERE s.taken_at IS NULL "
            "RETURN s.belief_id AS id, s.cause_change_id AS cause"
        )
    }
    supplied = {(row["target"], row["cause_change_id"]) for row in op["support_change_refs"]}
    if pending != supplied:
        refuse("consolidation_stale", "the support-change queue changed after the packet read")
    from grove.writer.consolidation import guard

    guard(run, op)
    ops = run.cache["all_ops"]
    resolved = {
        entry.get("contradiction") for entry in ops if entry.get("op") == "resolve_contradiction"
    }
    taken = {
        (entry.get("target"), entry.get("cause_change_id"))
        for entry in ops
        if entry.get("op") == "resolve_support_change"
    }
    if not current <= resolved or not pending <= taken:
        refuse(
            "consolidation_unresolved", "every queued row needs a disposition in this transaction"
        )


HANDLERS = {
    "mint_belief": mint,
    "promote_belief": transition,
    "weaken_belief": transition,
    "restore_belief": transition,
    "supersede_belief": end,
    "retire_belief": end,
    "merge_beliefs": merge,
    "correct_belief": correct,
    "retest_belief": retest,
    "open_contradiction": open_contradiction,
    "resolve_contradiction": resolve_contradiction,
    "resolve_support_change": resolve_support_change,
    "guard_consolidation": guard_consolidation,
}
