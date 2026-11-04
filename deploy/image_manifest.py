"""Pure mapping from reviewed checkout paths to production image paths.

This is a deliberately fixed mapping, not a Docker ignore parser. Changes to
the reviewed paths or ignore rules require another review before use.
"""

from collections.abc import Mapping
import hashlib
import re

DOCKERIGNORE_SHA256 = "bb0246ad6f5d6275c562277ce6e6704c1cc52f109bb503cbca2afdb833816bbc"
EXCLUDED = frozenset({
    "web/checks/fidelity.cjs",
    "web/checks/layout-persistence.cjs",
    "web/checks/live.cjs",
    "web/checks/workers.cjs",
})
CHECKOUT_PATHS = EXCLUDED | frozenset({
    "docs/data-flow.md",
    "server/grove/api/pipedream.py",
    "server/grove/api/uploads.py",
    "server/grove/api/views.py",
    "server/grove/ingest/upload_policy.py",
    "server/grove/privacy.py",
    "server/grove/views/workers.py",
    "web/agents.css",
    "web/agents.js",
    "web/index.html",
    "web/layout-keep.js",
    "web/live.js",
    "web/native-app.js",
    "web/pipedream.js",
    "web/product.css",
    "web/product.js",
    "web/simulation.js",
    "web/style.css",
    "web/system.css",
    "web/whatsapp-guide.js",
})


def partition(hashes: Mapping[str, str], dockerignore: bytes) -> tuple[dict[str, str], list[str]]:
    """Return image hashes and excluded paths without modifying the input.

    The caller must first verify checkout hashes. This function only maps those
    hashes; it performs no file, process, network, or application operations.
    """
    if hashlib.sha256(dockerignore).hexdigest() != DOCKERIGNORE_SHA256:
        raise ValueError("dockerignore_changed")
    if set(hashes) != CHECKOUT_PATHS:
        raise ValueError("image_path_set_changed")
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in hashes.values()):
        raise ValueError("source_digest_invalid")
    return ({path: digest for path, digest in hashes.items() if path not in EXCLUDED},
            sorted(EXCLUDED))
