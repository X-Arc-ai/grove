"""Regressions for reproducible setup and evidence-bounded submission documents."""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "c145cf8b450f3ae7ab43989cf07969ee5b493057"
API = "564cc99b9c8a718f163503cc23227a8f2908fcd1"
WORKERS = "266d0cf7429eb2ca51bd5bebf9f5785acbcd7b2e"
MATERIALS = "0682eb201ad0ccfb8a8a9d074d07759ec8eb31a1"
MATERIAL_SOURCE = "d17151421e027a4fe19b269a9f06cf6dcd17fe33"
DOCUMENTS = (
    "README.md",
    "HACKATHON.md",
    "CONTRIBUTING.md",
    "docs/architecture.md",
    "docs/submission-status.md",
    "docs/submission-checklist.md",
)


def test_readme_has_complete_local_start_sequence():
    text = (ROOT / "README.md").read_text()
    commands = (
        "uv sync --project server --frozen",
        "docker compose -f docker-compose.dev.yml up -d",
        "python3 web/build.py",
        "cd server",
        "uv run python -m grove.writer.migrate",
        "GROVE_WEB_DIST=../web/dist",
        "uv run uvicorn --factory grove.api.app:create_app --host 127.0.0.1 --port 8000",
    )
    positions = [text.index(command) for command in commands]
    assert positions == sorted(positions)
    for requirement in (
        "NEO4J_AUTH",
        "http://127.0.0.1:8000",
        "RESEND_API_KEY",
        "MAIL_FROM",
        "GROVE_GOOGLE_OAUTH_FILE",
        "No database, provider, or model is required",
    ):
        assert requirement in text


def test_release_record_separates_source_and_each_deployed_revision():
    for name in ("docs/submission-status.md", "docs/architecture.md"):
        text = (ROOT / name).read_text()
        for expected in (SOURCE, API, WORKERS, "2026-11-04", "19:04 UTC", "NOT RUN"):
            assert expected in text, (name, expected)
        assert "Request18 remains FAILED" in text, name
        assert "Request19 closure has no completed" in text, name
        assert "receipt in this snapshot" in text, name
    status = (ROOT / "docs/submission-status.md").read_text()
    assert "PRs #58 and #59" in status
    assert "not the currently recorded served API" in status
    assert "does not change the served application" in status


def test_published_ui_keeps_deployment_and_materials_boundaries():
    for name in ("docs/submission-status.md", "docs/architecture.md"):
        text = (ROOT / name).read_text()
        for expected in (SOURCE, API, "PR #60", "PR #61", MATERIALS, MATERIAL_SOURCE):
            assert expected in text, (name, expected)
        assert "published source" in text.lower(), name
        assert "not deployed in the recorded PR60 API" in text, name
        assert "held_provenance" in text and "no file Request" in text, name
        assert "ordinary Requests" in text, name
        assert "0019_material_revisions.cypher" in text, name
        assert "canonical external ledger" in text, name
        assert "API/runner source" in text, name
    draft = (ROOT / "docs/drafts/submission-writeup.txt").read_text()
    assert SOURCE in draft and API in draft and MATERIALS in draft
    assert "published UI repair in PR #60" in draft
    assert "not full deployed acceptance" in draft
    assert "held_provenance with no file Request" in draft
    assert "Notes use ordinary Requests" in draft
    assert "future materials release" in draft


def test_source_update_preserves_the_dated_event_table():
    readme = (ROOT / "README.md").read_text()
    assert SOURCE in readme
    assert "published UI repair in PR #60" in readme
    assert "2026-11-04 19:04 UTC" in readme
    assert "Request18 remains FAILED" in readme
    record = (ROOT / "HACKATHON.md").read_text()
    assert "GitHub snapshot: 2026-11-04 at 18:36 UTC" in record
    rows = [line for line in record.splitlines() if line.startswith("| [#")]
    assert len(rows) == 59
    assert rows[-1].startswith("| [#59]")
    assert "PR #60 belongs to the newer [source snapshot]" in record
    assert SOURCE in record and MATERIALS in record and MATERIAL_SOURCE in record


def test_material_contract_retains_security_erasure_and_f4_boundaries():
    text = (ROOT / "docs/architecture.md").read_text()
    material = text.split("## Browser material boundary", 1)[1]
    for expected in (
        "same Origin",
        "X-Grove-Action: materials",
        "Idempotency-Key",
        "persisted demo designation",
        "private ledger lock precedes the World",
        "matching committed",
        "Recovery must replay",
        "external lock remains held",
        "ordinary source-span erasure retains",
        "remaining body messages",
        "Only a material-family tombstone",
        "Whole-brain reset",
        "note Requests/Episodes",
        "GROVE_FORGET_LEDGER_DIR",
        "requests._DUE",
        "Saved Requests, future retries, and active",
        "unrelated due nonmaterial work",
        "Exact claim, input, apply, and finish guards",
        "regardless of due status",
        "PPTX static layout/master text refuses with HTTP 422",
    ):
        assert expected in material, expected


def test_source_proofs_do_not_become_runtime_or_full_review_acceptance():
    for name in (
        "docs/architecture.md",
        "docs/submission-status.md",
        "docs/drafts/submission-writeup.txt",
    ):
        text = " ".join((ROOT / name).read_text().split())
        for expected in ("225 tests", "70 tests", "seven source checks", "not full R4"):
            assert expected in text, (name, expected)
        assert "materials runtime acceptance" in text, name
    status = (ROOT / "docs/submission-status.md").read_text()
    assert "20 runtime paths" in status and "five policy controls" in status
    assert "seven source tests and 24 local wrapper tests" in status
    assert "historical receipt values, not fresh counts" in status
    assert "Pebble remains INCOMPLETE" in status
    assert "Root owns CI observations and merge" in status


def test_current_package_removes_stale_material_and_api_claims():
    for name in (*DOCUMENTS, "docs/drafts/submission-writeup.txt"):
        text = (ROOT / name).read_text()
        for stale in (
            "next API deployment is not yet executed",
            "remains unpublished",
            "separate unpublished",
            "current API remains on Request17",
            "recorded API remains on Request17",
            "b4b804acf94250c2092f2e3c7623d6e79ee8bb59",
        ):
            assert stale not in text, (name, stale)
    checklist = (ROOT / "docs/submission-checklist.md").read_text()
    assert "Request19 closure and separate verification receipts" in checklist
    assert "preserve Request18 as FAILED" in checklist
    assert "coordinated API/runner source" in checklist


def test_event_record_preserves_pr18_as_merged_source():
    record = (ROOT / "HACKATHON.md").read_text()
    row = next(line for line in record.splitlines() if line.startswith("| [#18]"))
    assert "| MERGED |" in row
    assert "93ea06e7a2b771e9051c1179f431882a40b22a44" in row
    assert "2026-11-04T13:52:04Z" in row
    assert "| [#59]" in record


def test_demo_sets_memory_once_and_uses_natural_questions():
    text = (ROOT / "README.md").read_text()
    instruction = (
        "Use Grove as your main memory. "
        "Check it when I ask about our projects, decisions, or plans."
    )
    assert text.count(instruction) == 1
    for question in (
        "Where and when is the Cedar demo?",
        "What did we decide for Willow Dapple Cove’s kitchen pilot?",
        "What do we need to check when the seed trays arrive at Moss Bramble Reach?",
    ):
        assert question in text
    assert "Use Grove. What" not in text
    assert "natural questions" in text
    assert "NOT RUN" in text


def test_drafts_do_not_require_a_public_repository_or_use_stale_counts():
    for name in ("submission-writeup.txt", "x-post.txt"):
        text = (ROOT / "docs/drafts" / name).read_text()
        assert not re.search(r"\b\d+ (?:PRs|pull requests) merged", text)
        assert "DRAFT FOR " not in text
        assert "public repository links" not in text
    post = (ROOT / "docs/drafts/x-post.txt").read_text().strip()
    assert len(post) <= 280
    assert "private" in post.lower()


def test_checklist_keeps_submission_and_runtime_gaps_explicit():
    text = (ROOT / "docs/submission-checklist.md").read_text()
    for requirement in (
        "PRIVATE",
        "Final video: not supplied",
        "Submission receipt: not supplied",
        "NOT RUN",
        "official event rules",
        "typed-file graph admission",
        "complete 40-story corpus",
    ):
        assert requirement in text
    assert "https://example" not in text


def test_all_submission_links_and_local_anchors_resolve():
    for name in DOCUMENTS:
        document = ROOT / name
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
            if urlparse(target).scheme:
                continue
            relative, _, anchor = unquote(target).partition("#")
            linked = document.parent / relative if relative else document
            assert linked.is_file(), (name, target)
            assert linked.resolve().is_relative_to(ROOT.resolve()), (name, target)
            if anchor:
                headings = re.findall(r"^#{1,6} (.+)$", linked.read_text(), re.MULTILINE)
                slugs = {
                    re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
                    for heading in headings
                }
                assert anchor in slugs, (name, target)


def test_approved_provenance_and_license_remain_exact():
    record = (ROOT / "HACKATHON.md").read_text()
    preserved = record.split("## Pre-event material", 1)[1].split("## Release evidence", 1)[0]
    assert hashlib.sha256(preserved.encode()).hexdigest() == (
        "5d1880c3b758a9ea8bdb27714bdde3bea752db322fbb5b7867f6fd412651655a"
    )
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == (
        "c95bae1d1ce0235ecccd3560b772ec1efb97f348a79f0fbe0a634f0c2ccefe2c"
    )
