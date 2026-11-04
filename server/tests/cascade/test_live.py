"""One synthetic D6 Spark call through the metered model layer; never executed in CI."""

import pytest

from grove.cascade_jobs import load_explanation, packet_fingerprint, read_explanation
from grove.models.explain_break import explain
from grove.models.layer import ModelLayer
from grove.screening import Screener, ScreeningConfig
from grove.store.query import WorldQuery
from grove.writer.cascade_explanations import cache_explanation
from tests.cascade.test_writer import complete, graph, impacts, links
from tests.models.test_live import live_settings
from tests.writer.helpers import read

pytestmark = pytest.mark.live


def test_live_spark_explains_one_computed_mark_without_changing_reach(driver, seeded, chat):
    w, root, *_ = graph(driver, seeded, chat)
    complete(driver, w, root)
    before = impacts(driver, w)
    wiring = links(driver, w)
    mark = before[0]
    loaded = load_explanation(WorldQuery(driver, w), mark["link_id"])
    assert loaded is not None and loaded.packet is not None
    screen = Screener(ScreeningConfig())
    with ModelLayer(driver, settings=live_settings()) as layer:
        result = explain(layer, w, loaded.packet, screener=screen)
    assert result.by == "spark", result.fallback_reason
    assert result.model_call_id
    assert (
        cache_explanation(
            driver,
            w,
            mark["link_id"],
            result,
            expected_version=mark["version"],
            expected_packet=packet_fingerprint(loaded.packet),
            screener=screen,
        )
        == "saved"
    )
    assert read_explanation(driver, w, mark["link_id"], screener=screen).by == "spark"
    assert links(driver, w) == wiring
    assert {m["link_id"] for m in impacts(driver, w)} == {m["link_id"] for m in before}
    calls = read(driver, w, "MATCH (c:ModelCall {id: $id}) RETURN c", id=result.model_call_id)
    assert len(calls) == 1 and calls[0]["c"]["job"] == "explain_break"
