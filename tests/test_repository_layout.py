"""Keep relocated test and documentation changes inside the freeze boundary."""
import tempfile
import unittest
from pathlib import Path

import freeze_manifest


class RepositoryLayoutTests(unittest.TestCase):
    def test_relocated_sources_are_frozen_and_local_archive_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('tests/test_probe.py', 'docs/guide.md', '.local-archive/old.py'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original\n', encoding='utf-8')
            before = freeze_manifest.source_snapshot(root)
            self.assertIn('tests/test_probe.py', before)
            self.assertIn('docs/guide.md', before)
            self.assertNotIn('.local-archive/old.py', before)
            for name in ('tests/test_probe.py', 'docs/guide.md'):
                (root / name).write_text('changed\n', encoding='utf-8')
                after = freeze_manifest.source_snapshot(root)
                self.assertNotEqual(before[name], after[name])
