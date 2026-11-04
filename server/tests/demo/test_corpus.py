"""Pure fixed-corpus controls. No account, database or provider calls."""

from collections import Counter
from dataclasses import replace

import pytest

from grove.demo import corpus
from grove.demo.screening import screen_story
from grove.demo_policy import DEMO_INPUTS, DEMO_TEXT, DemoDenied, require_corpus_identity


def test_fixed_corpus_counts_topology_and_content():
    stories = corpus.stories()
    items = [item for story in stories for item in story.items]
    links = [link for story in stories for link in story.links]
    keys = {item.key for item in items}
    assert len(keys) == len(items) == 2080
    assert len(set(links)) == len(links) == 6279
    assert len({item.title for item in items}) == len(items)
    assert Counter(item.kind for item in items) == {
        "person": 160,
        "organisation": 80,
        "plan": 160,
        "task": 800,
        "decision": 240,
        "event": 240,
        "observation": 200,
        "assumption": 200,
    }
    adjacency = {key: set() for key in keys}
    for link in links:
        assert link.source in keys and link.target in keys and link.source != link.target
        adjacency[link.source].add(link.target)
        adjacency[link.target].add(link.source)
    seen, pending = set(), [items[0].key]
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            pending.extend(adjacency[key] - seen)
    assert seen == keys
    assert all(item.summary.startswith("Synthetic demo data.") for item in items)
    assert require_corpus_identity() == corpus.manifest()["sha256"]
    assert DEMO_INPUTS == (DEMO_TEXT,)


def test_all_fixed_sources_receive_real_screening():
    for story in corpus.stories():
        screened = screen_story(story.key)
        assert screened.content() == story.content()
        assert len(screened.messages) == 52
        assert len(screened.content()) <= 32768
        assert dict(screened.counts) == {}


def test_arbitrary_or_modified_corpus_refuses(monkeypatch):
    with pytest.raises(DemoDenied):
        screen_story("unapproved-source")
    original = corpus.stories()
    changed = replace(original[0].items[0], summary="unapproved replacement")
    monkeypatch.setattr(
        corpus,
        "stories",
        lambda: (replace(original[0], items=(changed, *original[0].items[1:])), *original[1:]),
    )
    with pytest.raises(DemoDenied):
        require_corpus_identity()


def test_topic_selection_is_bounded_and_relevant():
    selected = corpus.topic_items("Which Lantern Amber Quay tasks concern the book lockers?")
    assert 0 < len(selected) <= 32
    assert all("Lantern Amber Quay" in title for title in selected)
    assert any("book lockers" in title for title in selected)
    assert corpus.topic_items("unrelated-private-identity") == ()
    assert corpus.topic_items(None) == ()
