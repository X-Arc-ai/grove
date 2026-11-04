"""Public assets expose only manifest-bound bytes, without a session or store read."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grove.api.app import _add_web_pages
from grove.api.assets import add_public_assets


def asset_tree(tmp_path, data=b"window.example = true;", suffix="js", media="text/javascript"):
    root = tmp_path / "assets"
    root.mkdir()
    name = hashlib.sha256(data).hexdigest() + "." + suffix
    (root / name).write_bytes(data)
    (root / "manifest.json").write_text(json.dumps({name: media}))
    return root, name, data


def client_for(folder):
    app = FastAPI()

    class NoStore:
        def __getattr__(self, key):
            raise AssertionError("Public assets must not access the store")

    app.state.driver = NoStore()
    _add_web_pages(app, str(folder))
    return TestClient(app)


@pytest.mark.parametrize("suffix,media,data", [
    ("js", "text/javascript", b"window.example = true;"),
    ("woff2", "font/woff2", b"synthetic-font-bytes"),
])
def test_exact_public_asset_bytes_cache_and_no_store(tmp_path, suffix, media, data):
    _, name, _ = asset_tree(tmp_path, data, suffix, media)
    client = client_for(tmp_path)
    response = client.get("/assets/grove/" + name)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"].split(";")[0] == media
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["etag"] == '"' + name.split(".")[0] + '"'
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("request_path", [
    "manifest.json", "fixture.json", "fixture.js", "unknown.js", "../private.env",
    "%2e%2e%2fprivate.env", "%252e%252e%252fprivate.env", "a" * 64 + ".js",
    "a" * 64 + ".env", "a" * 65 + ".js", "a" * 64 + ".js/extra",
])
def test_unknown_and_traversal_paths_never_expose_files(tmp_path, request_path):
    asset_tree(tmp_path)
    (tmp_path / "private.env").write_text("synthetic private sentinel")
    response = client_for(tmp_path).get("/assets/grove/" + request_path)
    assert response.status_code == 404
    assert b"synthetic private sentinel" not in response.content


def test_undeclared_and_changed_bytes_are_refused(tmp_path):
    root, name, _ = asset_tree(tmp_path)
    (root / name).write_bytes(b"changed after manifest generation")
    unknown = hashlib.sha256(b"undeclared").hexdigest() + ".js"
    (root / unknown).write_bytes(b"undeclared")
    client = client_for(tmp_path)
    assert client.get("/assets/grove/" + name).status_code == 404
    assert client.get("/assets/grove/" + unknown).status_code == 404


@pytest.mark.parametrize("part", ["asset", "manifest", "directory"])
def test_symlink_escape_is_refused(tmp_path, part):
    root, name, data = asset_tree(tmp_path)
    if part == "asset":
        outside = tmp_path / "outside.js"
        outside.write_bytes(data)
        (root / name).unlink()
        (root / name).symlink_to(outside)
    elif part == "manifest":
        outside = tmp_path / "outside.json"
        (root / "manifest.json").rename(outside)
        (root / "manifest.json").symlink_to(outside)
    else:
        outside = tmp_path / "outside"
        root.rename(outside)
        root.symlink_to(outside, target_is_directory=True)
    assert client_for(tmp_path).get("/assets/grove/" + name).status_code == 404


def test_missing_manifest_and_page_cache_contract(tmp_path):
    (tmp_path / "index.html").write_text("synthetic sign-in")
    (tmp_path / "home.html").write_text("synthetic home")
    client = client_for(tmp_path)
    assert client.get("/assets/grove/" + "a" * 64 + ".js").status_code == 404
    assert client.get("/signin").headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-store"


def test_assets_are_an_immutable_startup_snapshot(tmp_path):
    root, name, data = asset_tree(tmp_path)
    app = FastAPI()
    add_public_assets(app, tmp_path)
    (root / name).write_bytes(b"replacement bytes")
    assert TestClient(app).get("/assets/grove/" + name).content == data
