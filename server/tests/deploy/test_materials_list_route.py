"""GET /v1/materials is the Settings file list, and it has no trailing slash.

With only `location /v1/materials/`, nginx answers 301 and the redirect ends in a 404.
"""

from pathlib import Path

TEMPLATE = Path(__file__).parents[3] / "deploy" / "nginx" / "grove-https.conf.template"


def test_the_materials_list_has_its_own_exact_route():
    assert "location = /v1/materials {" in TEMPLATE.read_text(encoding="utf-8")
