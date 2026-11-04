"""Regressions for reproducible setup and evidence-bounded submission documents."""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[3]
RECORDING_SOURCE = "7d94266e1512ef9c5815d343518131a18bc6cbd8"
RECORDING_API = "a9da90686d687639a75a154172adbe2bfb4a1ea7"
SOURCE = "c145cf8b450f3ae7ab43989cf07969ee5b493057"
API = "564cc99b9c8a718f163503cc23227a8f2908fcd1"
WORKERS = "266d0cf7429eb2ca51bd5bebf9f5785acbcd7b2e"
MATERIALS = "0682eb201ad0ccfb8a8a9d074d07759ec8eb31a1"
MATERIAL_SOURCE = "d17151421e027a4fe19b269a9f06cf6dcd17fe33"
PREVIOUS_API = "cbcfe574e03880569bfcdb9a9732f79a7ef3daea"
DOCUMENTS = (
    "README.md",
    "HACKATHON.md",
    "CONTRIBUTING.md",
    "docs/architecture.md",
    "docs/submission-status.md",
    "docs/submission-checklist.md",
)
RELEASE_DOCUMENTS = tuple(
    name for name in DOCUMENTS if name not in ("README.md", "CONTRIBUTING.md")
) + (
    "docs/drafts/submission-writeup.txt",
)


@pytest.mark.parametrize("name", RELEASE_DOCUMENTS)
def test_recording_release_records_current_api_and_separate_develop_merge(name):
    text = " ".join((ROOT / name).read_text().split())
    for expected in (RECORDING_SOURCE, RECORDING_API, "PR #66", "20:06:46 UTC"):
        assert expected in text, (name, expected)
    if name == "docs/submission-status.md":
        revisions = text.split("## Source and deployed revisions", 1)[1].split("## ", 1)[0]
        assert f"| Recorded served API, PR #66 | `{RECORDING_API}` |" in revisions
        assert f"| Accepted source baseline | `{RECORDING_SOURCE}` |" in revisions
        assert f"| Retained runner and gateway | `{WORKERS}` |" in revisions
        assert f"| Verified `deployed.sha` | `{RECORDING_API}` |" in revisions
        assert f"| Previous recording API, PR #60 | `{API}` |" in revisions


@pytest.mark.parametrize("name", RELEASE_DOCUMENTS)
def test_real_zip_pass_does_not_become_graph_or_muse_acceptance(name):
    text = " ".join((ROOT / name).read_text().split())
    for expected in (
        "19:53:06 UTC",
        "real Claude ZIP upload",
        "one source, one episode, and two kept messages",
        "repeat added zero",
        "did not change the graph",
        "prepared-account Muse",
        "NOT RUN",
    ):
        assert expected in text, (name, expected)
    for stale in (
        "owner login/upload/Muse rehearsal remains **NOT RUN**",
        "owner login, upload, and Muse rehearsal remain NOT RUN",
        "Current owner login, upload, logout, provider consent, and Muse rehearsal: **NOT RUN**",
        "login/upload/Muse flow is paused and remains **NOT RUN** for this release",
    ):
        assert stale not in text, (name, stale)


def test_readme_keeps_no_stale_rehearsal_claims():
    text = " ".join((ROOT / "README.md").read_text().split())
    for stale in (
        "owner login/upload/Muse rehearsal remains **NOT RUN**",
        "owner login, upload, and Muse rehearsal remain NOT RUN",
        "Current owner login, upload, logout, provider consent, and Muse rehearsal: **NOT RUN**",
        "login/upload/Muse flow is paused and remains **NOT RUN** for this release",
    ):
        assert stale not in text, stale


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
        "The document checks need no database, provider or model",
    ):
        assert requirement in text


def test_release_record_separates_source_and_each_deployed_revision():
    for name in ("docs/submission-status.md", "docs/architecture.md"):
        text = (ROOT / name).read_text()
        for expected in (
            RECORDING_SOURCE, RECORDING_API, WORKERS, "2026-11-04", "20:06:46 UTC", "NOT RUN"
        ):
            assert expected in text, (name, expected)
        assert "Request18 remains FAILED" in text, name
        assert "Request19 closure and separate verification PASSED" in " ".join(text.split()), name
    status = (ROOT / "docs/submission-status.md").read_text()
    assert "PRs #58 and #59" in status
    assert "not the currently recorded served API" in status
    assert "does not change the served application" in status


def test_published_ui_keeps_deployment_and_materials_boundaries():
    for name in ("docs/submission-status.md", "docs/architecture.md"):
        text = (ROOT / name).read_text()
        for expected in (
            RECORDING_SOURCE, RECORDING_API, API, "PR #66", "PR #61", MATERIALS, MATERIAL_SOURCE
        ):
            assert expected in text, (name, expected)
        assert "published source" in text.lower(), name
        assert "not deployed in the recording API" in text, name
        assert "held_provenance" in text and "no file Request" in text, name
        assert "ordinary Requests" in text, name
        assert "0019_material_revisions.cypher" in text, name
        assert "canonical external ledger" in text, name
        assert "API/runner source" in text, name
    draft = (ROOT / "docs/drafts/submission-writeup.txt").read_text()
    assert RECORDING_SOURCE in draft and RECORDING_API in draft and MATERIALS in draft
    assert "recording API serves PR #66" in draft
    assert "not full deployed acceptance" in draft
    assert "held_provenance with no file Request" in draft
    assert "Notes use ordinary Requests" in draft
    assert "future materials release" in draft


def test_source_update_preserves_the_dated_event_table():
    record = (ROOT / "HACKATHON.md").read_text()
    assert "GitHub snapshot: 2026-11-04 at 18:36 UTC" in record
    rows = [line for line in record.splitlines() if line.startswith("| [#")]
    assert len(rows) == 59
    assert rows[-1].startswith("| [#59]")
    assert hashlib.sha256("\n".join(rows).encode()).hexdigest() == (
        "03fb0e9449af189763e9dc5e5cad51d46702044f1761616af69e4747bcd1e389"
    )
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
        text = " ".join((ROOT / name).read_text().split())
        for stale in (
            "next API deployment is not yet executed",
            "remains unpublished",
            "separate unpublished",
            "current API remains on Request17",
            "recorded API remains on Request17",
            "b4b804acf94250c2092f2e3c7623d6e79ee8bb59",
            "Request19 closure has no completed",
            "no completed closure receipt",
            "no completed receipt in this snapshot",
            "open Request19 closure",
            "stale `deployed.sha`",
            "must record Request19 closure separately",
            "Obtain the Request19 closure",
        ):
            assert stale not in text, (name, stale)
    checklist = (ROOT / "docs/submission-checklist.md").read_text()
    assert "Request19 closure and separate verification receipts" in checklist
    assert "preserve Request18 as FAILED" in checklist
    assert "coordinated API/runner source" in checklist


def test_request19_dated_update_preserves_failure_and_deployment_boundary():
    for name in (
        "HACKATHON.md",
        "docs/architecture.md",
        "docs/submission-status.md",
        "docs/submission-checklist.md",
        "docs/drafts/submission-writeup.txt",
    ):
        text = " ".join((ROOT / name).read_text().split())
        for expected in (
            "19:04:34 UTC",
            "Request19 closure and separate verification PASSED",
            "Request18 remains FAILED",
            "not a new code deployment",
        ):
            assert expected in text, (name, expected)
    checklist = (ROOT / "docs/submission-checklist.md").read_text()
    assert "- [x] Record the Request19 closure and separate verification receipts" in checklist
    for pending in (
        "Verify material ledger equality",
        "Finish the complete 40-story corpus",
        "Follow the README from a fresh browser context",
        "Connect Muse to the prepared account",
    ):
        assert f"- [ ] {pending}" in checklist, pending


def test_request19_receipt_records_verified_markers_without_new_runtime_actions():
    status = (ROOT / "docs/submission-status.md").read_text()
    update = " ".join(
        status.split("## Request19 release evidence update", 1)[1]
        .split("## Recorded source and historical evidence", 1)[0]
        .split()
    )
    for expected in (
        SOURCE,
        API,
        PREVIOUS_API,
        "Both exited naturally with status 0 and empty stderr",
        "`deployed.sha` matches the PR60 API",
        "`previous.sha` retains",
        "failed barrier is archived and inactive",
        "three host locks were free",
        "local operator lock was absent at verification",
        "No rebuild, restart, migration, seed, or runtime data write occurred",
        "Materials remain merged but not deployed",
        "No new host or runtime checks ran",
        "e029b7b87ba5bd68c8447a459239dc3d3713cc727d8ed72c9f5e093ad56a1013",
        "8b25df182eaf1081439a7544280f098a382ba19dc8f546ffd4bd5fcf7f647238",
        "6b2bdcddcbe89f4548ddda7089b2a5d11ee31c99e8082f0d5a46ecf999f42e2c",
        "a2657e915f29eed845929689fe17218aa80dc45bab6e99192e8e51f41a58b4d0",
    ):
        assert expected in update, expected


def test_request19_graph_counts_remain_dated_and_corpus_acceptance_open():
    status = " ".join((ROOT / "docs/submission-status.md").read_text().split())
    for expected in (
        "At this dated Request19 verification",
        "generation 53, 1,355 nodes, and 4,082 edges",
        "1,352 Items, 26 Sources, 26 saved Requests, and 26 receipts",
        "dated verified counts, not an unconditional current claim",
        "40-story corpus remains incomplete",
        "four earlier recall passes retain their original scope",
        "Pebble remains INCOMPLETE",
        "**NOT RUN**",
    ):
        assert expected in status, expected


def test_event_record_preserves_pr18_as_merged_source():
    record = (ROOT / "HACKATHON.md").read_text()
    row = next(line for line in record.splitlines() if line.startswith("| [#18]"))
    assert "| MERGED |" in row
    assert "93ea06e7a2b771e9051c1179f431882a40b22a44" in row
    assert "2026-11-04T13:52:04Z" in row
    assert "| [#59]" in record


def test_demo_sets_memory_once_and_uses_natural_questions():
    text = (ROOT / "README.md").read_text()
    for question in (
        "Where and when is the Cedar demo?",
        "What did we decide for Willow Dapple Cove's kitchen pilot?",
        "What do we need to check when the seed trays arrive at Moss Bramble Reach?",
    ):
        assert question in text
    assert "Use Grove. What" not in text


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
