"""Production image mapping regressions using synthetic file contents."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "production_image_manifest", Path(__file__).resolve().parents[3] / "deploy/image_manifest.py"
)
mapping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mapping)

DOCKERIGNORE = b""".git
.github
**/.venv
**/__pycache__
**/.pytest_cache
**/.ruff_cache
**/node_modules
server/tests
docs/*
!docs/data-flow.md
evals
web/checks
web/dist
"""
RUNTIME_PATHS = {
    "docs/data-flow.md", "server/grove/api/pipedream.py", "server/grove/api/uploads.py",
    "server/grove/api/views.py", "server/grove/ingest/upload_policy.py",
    "server/grove/privacy.py", "server/grove/views/workers.py", "web/agents.css",
    "web/agents.js", "web/index.html", "web/layout-keep.js", "web/live.js",
    "web/native-app.js", "web/pipedream.js", "web/product.css", "web/product.js",
    "web/simulation.js", "web/style.css", "web/system.css", "web/whatsapp-guide.js",
}
CHECK_PATHS = {"web/checks/fidelity.cjs", "web/checks/layout-persistence.cjs",
               "web/checks/live.cjs", "web/checks/workers.cjs"}


class ProductionImageMapping(unittest.TestCase):
    def setUp(self):
        self.contents = {path: ("synthetic " + path).encode() for path in RUNTIME_PATHS | CHECK_PATHS}
        self.hashes = {path: hashlib.sha256(data).hexdigest() for path, data in self.contents.items()}

    def test_checkout_and_image_manifests_are_distinct(self):
        included, excluded = mapping.partition(self.hashes, DOCKERIGNORE)
        self.assertEqual(set(included), RUNTIME_PATHS)
        self.assertEqual(excluded, sorted(CHECK_PATHS))
        self.assertEqual((len(self.hashes), len(included), len(excluded)), (24, 20, 4))

    def test_original_checkout_map_requires_absent_image_files(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)
            for path in RUNTIME_PATHS:
                target = image / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.contents[path])
            # The original checkout-based smoke tries to read an excluded file.
            with self.assertRaises(FileNotFoundError):
                for path in sorted(self.hashes):
                    (image / path).read_bytes()
            included, excluded = mapping.partition(self.hashes, DOCKERIGNORE)
            for path, expected in included.items():
                self.assertEqual(hashlib.sha256((image / path).read_bytes()).hexdigest(), expected)
            self.assertTrue(all(not (image / path).exists() for path in excluded))

    def test_all_runtime_paths_and_hashes_are_preserved(self):
        before = dict(self.hashes)
        included, _ = mapping.partition(self.hashes, DOCKERIGNORE)
        self.assertEqual(included, {path: self.hashes[path] for path in RUNTIME_PATHS})
        self.assertEqual(self.hashes, before)
        included.clear()
        self.assertEqual(self.hashes, before)

    def test_changed_ignore_rules_require_review(self):
        for rules in (DOCKERIGNORE + b"!web/checks\n",
                      DOCKERIGNORE.replace(b"!docs/data-flow.md\n", b""), b""):
            with self.subTest(rules=rules), self.assertRaisesRegex(ValueError, "dockerignore_changed"):
                mapping.partition(self.hashes, rules)

    def test_unknown_or_missing_paths_require_review(self):
        for path in self.hashes:
            changed = dict(self.hashes)
            del changed[path]
            with self.subTest(missing=path), self.assertRaisesRegex(ValueError, "image_path_set_changed"):
                mapping.partition(changed, DOCKERIGNORE)
            changed["server/grove/unreviewed.py"] = "0" * 64
            with self.subTest(replaced=path), self.assertRaisesRegex(ValueError, "image_path_set_changed"):
                mapping.partition(changed, DOCKERIGNORE)

    def test_unknown_exclusions_are_not_silently_accepted(self):
        for path in ("web/checks/unreviewed.cjs", "server/tests/new.py", "docs/new.md", "web/dist/new.js"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "image_path_set_changed"):
                mapping.partition({**self.hashes, path: "0" * 64}, DOCKERIGNORE)

    def test_invalid_digest_requires_review(self):
        for path in ("web/product.js", "web/checks/live.cjs"):
            for value in (None, "", "A" * 64, "0" * 63, "g" * 64):
                with self.subTest(path=path, value=value), self.assertRaisesRegex(ValueError, "source_digest_invalid"):
                    mapping.partition({**self.hashes, path: value}, DOCKERIGNORE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
