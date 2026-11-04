"""Executable checks for the published setup text, not end-to-end acceptance."""

import ast
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from grove.mcp.tools import DEFAULT_TOOLS

ROOT = Path(__file__).resolve().parents[3]


def test_readme_muse_settings_use_public_http_resource():
    text = (ROOT / "README.md").read_text()
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    settings = [json.loads(block) for block in blocks if '"mcpServers"' in block]
    assert len(settings) == 1, "README must provide one complete Muse settings example"
    config = settings[0]
    assert config["schema_version"] == 1
    assert config["mcpServers"] == {
        "grove": {"type": "http", "url": "https://app.grove.x-arc.ai/mcp"}
    }


def test_readme_has_safe_muse_command_and_login_steps():
    text = (ROOT / "README.md").read_text()
    assert "muse mcp login grove --headless" in text
    assert "--model muse-spark-1.3" in text
    assert "--no-foreign-personal-context" in text
    assert "--contributor-mode off" in text
    assert "schema_version" in text
    assert "Approve" in text
    assert "META_API_KEY" in text


def test_readme_does_not_claim_available_tools_when_registry_is_empty():
    text = (ROOT / "README.md").read_text()
    if not DEFAULT_TOOLS:
        assert "public MCP tools are not available" in text
        assert "not yet passed" in text


def test_readme_separates_retained_upload_from_prepared_recall():
    """The public demo must not imply that the upload produced the prepared graph."""
    text = (ROOT / "README.md").read_text()
    upload = text.split("### 2. Upload the Lumen example", 1)[1].split("### 3.", 1)[0]
    fixture = ROOT / "server/grove/demo/lumen-upload.json"
    assert fixture.is_file()
    assert "server/grove/demo/lumen-upload.json" in upload
    assert "Claude" in upload
    assert "Do not edit, reformat, or ZIP" in upload
    assert "Your graph has not changed from this upload." in upload
    assert "separate prepared account" in text
    assert "Do not upload Lumen into the prepared account" in text
    assert "Do not run a seed or store command" in text
    assert "Use Grove store to remember" not in text


def test_current_release_docs_distinguish_general_tools_from_restricted_demo():
    """An empty general registry does not mean the deployed demo pair is absent."""
    registry = ast.parse((ROOT / "server/grove/mcp/tools.py").read_text())
    demo = next(
        node for node in registry.body
        if isinstance(node, ast.FunctionDef) and node.name == "demo_tools"
    )
    names = {
        node.value.value
        for node in ast.walk(demo)
        if isinstance(node, ast.keyword)
        and node.arg == "name"
        and isinstance(node.value, ast.Constant)
    }
    assert names == {"store", "recall"}
    for relative in ("README.md", "docs/architecture.md", "docs/submission-status.md"):
        text = (ROOT / relative).read_text()
        assert "Cedar" in text, relative
        assert "restricted" in text.lower(), relative
        assert "general" in text.lower(), relative
        assert "NOT RUN" in text, relative
    readme = (ROOT / "README.md").read_text()
    assert "the merged snapshot has no upload endpoint" not in readme
    assert "[Pipedream](docs/pipedream.md)" in readme
    assert "authorization and status only" in readme
    assert "imports no" in readme


def test_submission_document_links_resolve():
    documents = [ROOT / "README.md", ROOT / "HACKATHON.md", ROOT / "docs/architecture.md"]
    for document in documents:
        text = document.read_text()
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            if urlparse(target).scheme or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            assert (document.parent / target).is_file(), (document.name, target)


def test_event_record_credits_every_vendored_font_family():
    css = (ROOT / "web/vendor/fonts.css").read_text()
    families = set(re.findall(r"font-family:\s*'([^']+)'", css))
    assert families, "The provenance check must read the actual vendored font declarations"
    record = (ROOT / "HACKATHON.md").read_text()
    credits = record.split("## Pre-event material", 1)[1].split("## Prior work", 1)[0]
    for family in families:
        assert family in credits, f"Missing pre-event font credit: {family}"
