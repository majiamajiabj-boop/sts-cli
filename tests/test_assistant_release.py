import hashlib
import tempfile
import unittest
from pathlib import Path
from assistant_release import validate_shipped_sources


class ReleaseSourceTests(unittest.TestCase):
    def test_sealing_requires_exact_tested_bytes_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.py"
            source.write_bytes(b"verified source")
            proof = {"packaged_sources": {"agent.py": hashlib.sha256(source.read_bytes()).hexdigest()}}
            validate_shipped_sources(root, proof)
            source.write_bytes(b"changed after test")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                validate_shipped_sources(root, proof)

    def test_missing_evidence_and_path_escape_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                validate_shipped_sources(root, {})
            with self.assertRaises(ValueError):
                validate_shipped_sources(root, {"packaged_sources": {"../outside.py": "a"*64}})


if __name__ == "__main__":
    unittest.main()
