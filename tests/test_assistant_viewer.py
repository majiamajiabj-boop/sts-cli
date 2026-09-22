"""Include the existing report-viewer suite in the top-level release gate."""
import tempfile
import unittest
from pathlib import Path
from viewer.server import RunStore
from viewer.test_viewer import *


class FreshInstallViewerTests(unittest.TestCase):
    def test_absent_archive_is_an_empty_readonly_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual([], RunStore(root).recent_runs()["runs"])
            self.assertFalse((root / "logs").exists())
