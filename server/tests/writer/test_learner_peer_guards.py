"""Published peer writers cannot reuse an unadmitted typed source."""

from types import SimpleNamespace

import pytest

from grove.writer import beliefs, consolidation
from grove.writer.errors import Refused


class Tx:
    def __init__(self, typed):
        self.typed = typed

    def run(self, query, **params):
        source = {
            "id": "source-1",
            "connector_id": "connector-1",
            "content": "raw legacy",
            "parser_version": "whatsapp.export.v1" if self.typed else "legacy",
        }
        if query == beliefs._EVIDENCE:
            return [
                {
                    "e": {
                        "id": "evidence-1",
                        "active": True,
                        "source_id": "source-1",
                        "episode_id": "episode-1",
                    }
                }
            ]
        if "(n:Source" in query:
            return [{"n": source}]
        if "(n:Connector" in query:
            return [{"n": {"id": "connector-1", "type": "past_ai_conversations"}}]
        if "(s:Source" in query:
            return [{"s": source}]
        if "(c:Connector" in query:
            return [{"c": {"id": "connector-1", "type": "past_ai_conversations"}}]
        raise AssertionError(query)


def test_belief_support_read_rejects_typed_evidence():
    run = SimpleNamespace(tx=Tx(True))
    with pytest.raises(Refused) as caught:
        beliefs._rows(run, "belief-1")
    assert caught.value.rule == "typed_source_unadmitted"


def test_consolidation_checked_sources_reject_typed_and_keep_raw_legacy():
    op = {"checked_source_ids": ["source-1"]}
    with pytest.raises(Refused) as caught:
        consolidation._sources(SimpleNamespace(tx=Tx(True)), op)
    assert caught.value.rule == "typed_source_unadmitted"
    assert consolidation._sources(SimpleNamespace(tx=Tx(False)), op) == ["source-1"]
