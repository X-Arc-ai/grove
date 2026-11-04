"""Serve a snapshot of declared, content-hashed public UI assets without store access."""

import hashlib
import json
import re
from pathlib import Path

from fastapi import FastAPI, Response

_NAME = re.compile(r"([a-f0-9]{64})\.(js|css|woff2|woff)\Z")
_TYPES = {"js": "text/javascript", "css": "text/css", "woff2": "font/woff2", "woff": "font/woff"}
_MAX_ASSET = 8 * 1024 * 1024


def add_public_assets(app: FastAPI, folder: Path) -> None:
    root = folder / "assets"
    assets: dict[str, tuple[bytes, str]] = {}
    manifest = root / "manifest.json"
    try:
        if root.is_symlink() or manifest.is_symlink() or manifest.stat().st_size > 65536:
            raise ValueError("invalid asset manifest")
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(entries, dict) or len(entries) > 128:
            raise ValueError("invalid asset manifest")
        for name, content_type in entries.items():
            match = _NAME.fullmatch(name)
            if not match or content_type != _TYPES[match[2]]:
                continue
            path = root / name
            if path.is_symlink() or path.resolve().parent != root.resolve():
                continue
            if not path.is_file() or path.stat().st_size > _MAX_ASSET:
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == match[1]:
                assets[name] = data, content_type
    except (OSError, ValueError, TypeError):
        assets = {}

    @app.get("/assets/grove/{name:path}", include_in_schema=False)
    def public_asset(name: str) -> Response:
        asset = assets.get(name)
        if asset is None:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        data, content_type = asset
        return Response(data, media_type=content_type, headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": '"' + name.split(".", 1)[0] + '"',
            "X-Content-Type-Options": "nosniff",
        })
