"""D12 checks ordinary claims against a source retained by this pass's belief retest.

The reader and source metadata establish authority. Caller text and caller verdicts are
never accepted. The existing D4 judges record the pieces that the writer later checks.
"""

from grove.models import verify_judge
from grove.models.belief_judge import strict_excerpt
from grove.models.verify_judge import JudgeInputError, Piece
from grove.store.primitives import KIND_BELIEF
from grove.store.query import PlatformQuery, WorldQuery
from grove.writer.registry import Registry


def _bearing(driver, world_id, query, claim_id):
    """Read D4's load-bearing conditions; the writer repeats its gate at commit time."""
    registry = Registry.load(query)
    items = query.read("MATCH (i:Item {id: $id}) RETURN i", id=claim_id)
    if items:
        if items[0]["i"].get("kind") == KIND_BELIEF:
            raise JudgeInputError("use evidence mode for a belief")
        meanings = [
            key for key, row in registry.meanings.items() if row.get("carries_consequences")
        ]
        return bool(
            query.read(
                "MATCH (a:Item)-[l:LINK]->(b:Item) WHERE (a.id = $id OR b.id = $id) "
                "AND l.active_key IS NOT NULL AND l.meaning IN $meanings RETURN l.id AS id LIMIT 1",
                id=claim_id,
                meanings=meanings,
            )
        )
    links = query.read(
        "MATCH (a:Item)-[l:LINK {id: $id}]->(b:Item) "
        "RETURN l.meaning AS meaning, a.id AS from_id, b.id AS to_id",
        id=claim_id,
    )
    if not links:
        raise JudgeInputError("claim_id names no ordinary claim in this world")
    row = links[0]
    meaning = registry.meaning(row["meaning"])
    if meaning.get("carries_consequences"):
        return True
    own = PlatformQuery(driver).read(
        "MATCH (w:World {id: $id}) RETURN w.self_item_id AS own",
        id=world_id,
    )
    return bool(
        meaning.get("temporal_class") == "state"
        and own
        and own[0]["own"] is not None
        and own[0]["own"] in (row["from_id"], row["to_id"])
    )


def judge_retest_claim(layer, driver, world_id, body, *, run_id):
    """Return two recorded D4 judgments without fetching again or accepting raw text."""
    if set(body) != {"mode", "claim_id", "source_id", "locator"} or body["mode"] != "claim":
        raise JudgeInputError("claim mode requires claim_id, source_id and locator")
    if any(
        not isinstance(body[key], str) or not 1 <= len(body[key]) <= 1024
        for key in ("claim_id", "source_id", "locator")
    ):
        raise JudgeInputError("claim and source references must be bounded text")
    query = WorldQuery(driver, world_id)
    rows = query.read(
        "MATCH (s:Source {id: $source}) MATCH (e:Episode {run_id: $run}) "
        "MATCH (r:RunRecord {id: $run}) MATCH (c:Connector {id: s.connector_id}) "
        "WHERE s.id IN e.source_ids AND e.cut_rule = 'live_check' "
        "AND s.parser_version = 'belief-retest-v1' AND c.type = 'web_pages' "
        "AND r.agent = 'connector' AND r.outcome = 'running' "
        "AND s.received_at >= r.started RETURN s",
        source=body["source_id"],
        run=run_id,
    )
    if not rows or rows[0]["s"].get("forget_state") == "forgotten":
        raise JudgeInputError("source_id names no retained retest source from this pass")
    content = rows[0]["s"].get("content")
    if not isinstance(content, str):
        raise JudgeInputError("the retest source has no retained text")
    piece = Piece(
        text=strict_excerpt(content, body["locator"]),
        speaker_kind="automated",
        source_type="confirmed",
        source_id=body["source_id"],
        locator=body["locator"],
    )
    bearing = _bearing(driver, world_id, query, body["claim_id"])
    first = verify_judge.judge(
        layer,
        driver,
        world_id,
        claim_id=body["claim_id"],
        pieces=[piece],
        run_id=run_id,
        load_bearing=bearing,
    )
    second = verify_judge.second_judge(
        layer,
        driver,
        world_id,
        claim_id=body["claim_id"],
        pieces=[piece],
        run_id=run_id,
    )
    return {
        "judgment_ids": [*first.judgment_ids, *second.judgment_ids],
        "model_call_ids": [first.model_call_id, second.model_call_id],
        "verdicts": [first.verdicts[0].verdict, second.verdicts[0].verdict],
        "correlated": second.correlated,
    }
