"""docs/store-map.md against the catalogue."""

import subprocess
import sys

from grove.store.catalogue import (
    B1_TABLES,
    FULLTEXT_INDEXES,
    PLATFORM_LABELS,
    RELATIONSHIP_TYPES,
    TABLES,
    TYPE_CLASSES,
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


REPO = DOC.parents[1]


def hand_written():
    return doc_text().split(BEGIN, 1)[0]


def test_every_ruling_from_s1_to_s35_is_written_once():
    text = hand_written()
    for n in range(1, 36):
        assert text.count(f"**S{n}. ") == 1, n


def test_the_type_classes_and_text_homes_are_generated():
    text = generate()
    assert text.startswith("## Type classes\n")
    for name in TYPE_CLASSES:
        assert f"| `{name}` |" in text, name
    assert "`items.status`" in text and "`kinds.status_vocabulary`" in text


def test_the_a5_table_has_backups_restore_and_trigram():
    text = hand_written()
    assert "| Backups | A whole-store offline dump each day" in text
    assert "| Restore | `neo4j-admin database load`" in text
    assert "| Full text and trigram match |" in text
    assert "reads with no lock" in text


def test_the_constraint_refusal_codes_are_in_probe_order():
    text = hand_written()
    assert "refused with 50N11" in text
    assert "51N27, cause 50N11" not in text


def test_facts_the_code_holds():
    text = hand_written()
    assert "`entity_type = registry_row`" in text
    assert "registry row`" not in text
    assert "spec lines 832, 1238, 1250, 1252, 1264, 1265, 1596, 1630, 1752, 2004, 2067," in text


def test_future_code_is_not_stated_as_present():
    text = hand_written()
    assert "A ruling states the design." in text
    assert "the writer checks required properties" not in text
    assert "are computed in code" not in text


def test_public_docs_point_to_no_private_plan():
    assert "handoff" not in doc_text().lower()
    flow = (REPO / "docs" / "data-flow.md").read_text(encoding="utf-8")
    assert "Meta Model API, Spark 1.3, Standard mode" in flow
    record = (REPO / "HACKATHON.md").read_text(encoding="utf-8")
    assert record.count("the simulation of the views (the sim)") == 1
    compose = (REPO / "docker-compose.dev.yml").read_text(encoding="utf-8")
    assert "lane" not in compose.splitlines()[0]


def test_the_open_points_of_review_round_one():
    points = hand_written().split("## Open points for later lanes", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "The value index must gain the status words",
        "`why` of a contradiction from `grove.cleanup.v1`",
        "never from the installed package",
        "`GROVE_ENV=production` must be set on the box",
        "The daily dump and the restore have not run on this store.",
        "no rows for the mail provider, Google sign-in, Calendar and backups",
        "`history_values.forgotten_at`, `budgets.spent`",
    ):
        assert phrase in points, phrase


def test_the_open_points_hand_a_forgotten_items_map_position_to_forget():
    head = doc_text().split(BEGIN, 1)[0]
    open_points = head.split("## Open points for later lanes", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in open_points.splitlines() if "`map_positions`" in line)
    assert "after its item is forgotten" in row and row.endswith("| `forget` |")
