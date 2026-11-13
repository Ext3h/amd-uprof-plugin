"""The installed plugin is self-contained: maintainer documents live in docs/, outside it.

The plugin directory is what a user installs. It must not carry the maintainer documents
(specification, derivation notes, validation records) and no shipped file may name them or any
other path of the repository outside the plugin, since none of them exist on an installed copy.

Run from the repository root: python tests/run_tests.py test_plugin_self_contained
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
SKILL_DIR = PLUGIN / "skills" / "amd-uprof"

# Names of maintainer documents and repository paths outside the plugin.
FORBIDDEN = (
    "../",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "README.md",
    "bench/",
    "docs/",
    "examples/",
    "reference.md",
    "references/",
    "spec.md",
    "tests/",
    "validation.md",
)


def _shipped_text_files():
    for path in sorted(PLUGIN.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


class PluginSelfContainedTest(unittest.TestCase):
    def test_no_references_directory_ships(self):
        self.assertFalse((SKILL_DIR / "references").exists())

    def test_maintainer_documents_live_in_docs(self):
        for name in ("reference.md", "spec.md", "validation.md"):
            with self.subTest(document=name):
                self.assertTrue((ROOT / "docs" / name).is_file())

    def test_no_shipped_file_names_a_path_outside_the_plugin(self):
        files = list(_shipped_text_files())
        self.assertTrue(files)
        for path, text in files:
            for name in FORBIDDEN:
                with self.subTest(file=str(path.relative_to(ROOT)), name=name):
                    self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
