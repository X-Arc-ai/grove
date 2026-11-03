"""Executable checks for the published setup text, not end-to-end acceptance."""

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


def test_submission_document_links_resolve():
    documents = [ROOT / "README.md", ROOT / "HACKATHON.md", ROOT / "docs/architecture.md"]
    for document in documents:
        text = document.read_text()
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            if urlparse(target).scheme or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            assert (document.parent / target).is_file(), (document.name, target)
