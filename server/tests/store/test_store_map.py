"""docs/store-map.md against the catalogue."""

import subprocess
import sys

from grove.store.catalogue import (
    B1_TABLES,
    FULLTEXT_INDEXES,
    PLATFORM_LABELS,
    RELATIONSHIP_TYPES,
    TABLES,
    WORLD_LABELS,
)
from grove.store.storemap import BEGIN, DOC, END, generate, render


def doc_text():
    return DOC.read_text(encoding="utf-8")


def test_check_passes_as_ci_runs_it():
    run = subprocess.run(
        [sys.executable, "-m", "grove.store.storemap", "--check"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr


def test_check_fails_on_a_changed_generated_part(monkeypatch, tmp_path):
    from grove.store import storemap

    changed = doc_text().replace("### items\n", "### items changed\n", 1)
    path = tmp_path / "store-map.md"
    path.write_text(changed, encoding="utf-8")
    monkeypatch.setattr(storemap, "DOC", path)
    assert storemap.main(["--check"]) == 1


def test_render_keeps_the_hand_written_part():
    text = doc_text()
    assert render(text) == text
    assert text.count(BEGIN) == 1 and text.count(END) == 1
    assert generate() in text


def test_every_b1_table_is_a_heading():
    headings = {ln[4:] for ln in doc_text().splitlines() if ln.startswith("### ")}
    assert set(B1_TABLES) <= headings
    assert {t.name for t in TABLES} == headings


def test_every_label_occurs():
    text = doc_text()
    for label in WORLD_LABELS | PLATFORM_LABELS:
        assert f":{label}" in text, label
    for rel in RELATIONSHIP_TYPES:
        assert f"[:{rel}]" in text


def test_every_schema_name_occurs():
    text = generate()
    for t in TABLES:
        for item in (*t.uniques, *t.indexes):
            assert f"`{item.name}`" in text
    for f in FULLTEXT_INDEXES:
        assert f"`{f.name}`" in text


def test_every_property_occurs_under_its_table():
    text = generate()
    for t in TABLES:
        section = text.split(f"### {t.name}\n", 1)[1].split("\n### ", 1)[0]
        for c in t.columns:
            for prop in c.props:
                assert f"`{prop}`" in section, (t.name, prop)
