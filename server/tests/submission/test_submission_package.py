"""Regressions for reproducible setup and evidence-bounded submission documents."""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "564cc99b9c8a718f163503cc23227a8f2908fcd1"
API = "cbcfe574e03880569bfcdb9a9732f79a7ef3daea"
WORKERS = "266d0cf7429eb2ca51bd5bebf9f5785acbcd7b2e"
MATERIALS = "b4b804acf94250c2092f2e3c7623d6e79ee8bb59"
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
        assert SOURCE in text, name
        assert API in text, name
        assert WORKERS in text, name
        assert "Request17" in text, name
        assert "NOT RUN" in text, name
    status = (ROOT / "docs/submission-status.md").read_text()
    assert "PRs #58 and #59" in status
    assert "not deployed" in status
    assert "unpublished" in status


def test_published_ui_keeps_deployment_and_materials_boundaries():
    for name in ("docs/submission-status.md", "docs/architecture.md"):
        text = (ROOT / name).read_text()
        assert SOURCE in text, name
        assert "PR #60" in text, name
        assert "published source" in text.lower(), name
        assert "not deployed in Request17" in text, name
        assert "next API deployment is not yet executed" in text, name
        assert MATERIALS in text, name
        assert "unpublished" in text, name
        assert "held_provenance" in text, name
        assert "no file Request" in text, name
        assert "0019_material_revisions.cypher" in text, name
    draft = (ROOT / "docs/drafts/submission-writeup.txt").read_text()
    assert SOURCE in draft
    assert "published UI repair in PR #60" in draft
    assert "not deployed acceptance" in draft
    assert MATERIALS in draft and "remains unpublished" in draft
    assert "held_provenance with no file Request" in draft


def test_source_update_preserves_the_dated_event_table():
    readme = (ROOT / "README.md").read_text()
    assert SOURCE in readme
    assert "published UI repair in PR #60" in readme
    assert "next API deployment is not yet executed" in readme
    record = (ROOT / "HACKATHON.md").read_text()
    assert "GitHub snapshot: 2026-11-04 at 18:36 UTC" in record
    rows = [line for line in record.splitlines() if line.startswith("| [#")]
    assert len(rows) == 59
    assert rows[-1].startswith("| [#59]")
    assert "PR #60 belongs to the newer [source snapshot]" in record


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
