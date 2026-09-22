import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import decision_case_corpus


def _case(attempt_id, sequence, *, decision="decision-a", controller="controller-a"):
    return {
        "case_schema_version": 2,
        "attempt_id": attempt_id,
        "before_seq": sequence,
        "phase": "EVENT",
        "decision_hash": decision,
        "controller_hash": controller,
    }


def _write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


class DecisionCaseCorpusTests(unittest.TestCase):
    def _bundle(self, root):
        root = Path(root)
        root_cases = root / "decision-cases.jsonl"
        _write_jsonl(root_cases, [_case("old-attempt", 1)])
        sources = []
        for attempt_id, sequence in (("attempt-a", 2), ("attempt-b", 3)):
            path = root / "logs" / "attempts" / attempt_id / "decision-cases.jsonl"
            _write_jsonl(path, [_case(attempt_id, sequence)])
            sources.append(path)
        manifest = decision_case_corpus.build_manifest(root_cases, sources)
        manifest_path = root / decision_case_corpus.DEFAULT_MANIFEST_NAME
        decision_case_corpus.write_manifest(manifest_path, manifest)
        return root_cases, sources, manifest_path, manifest

    def test_ordered_bounded_archives_load_without_runtime_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root_cases, sources, manifest_path, manifest = self._bundle(directory)
            self.assertEqual(3, manifest["source_count"])
            self.assertEqual([0, 1, 2], [
                row["start_case_index"] for row in manifest["sources"]
            ])
            self.assertEqual(3, len({
                row["archive_path"] for row in manifest["sources"]
            }))
            root_cases.unlink()
            for source in sources:
                source.unlink()
            cases, evidence = decision_case_corpus.load_case_corpus(
                root_cases, manifest_path, with_evidence=True,
            )
            self.assertEqual(
                ["old-attempt", "attempt-a", "attempt-b"],
                [row["attempt_id"] for row in cases],
            )
            self.assertEqual(3, evidence["source_count"])
            self.assertEqual(
                decision_case_corpus.case_corpus_digest(cases),
                evidence["case_corpus_sha256"],
            )

    def test_missing_manifest_and_archive_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root_cases, _sources, manifest_path, manifest = self._bundle(directory)
            with self.assertRaises(decision_case_corpus.CaseCorpusError):
                decision_case_corpus.load_case_corpus(
                    root_cases, Path(directory) / "missing.json"
                )
            archive = Path(directory) / manifest["sources"][1]["archive_path"]
            raw = bytearray(archive.read_bytes())
            raw[len(raw) // 2] ^= 1
            archive.write_bytes(raw)
            with self.assertRaises(decision_case_corpus.CaseCorpusError):
                decision_case_corpus.load_case_corpus(root_cases, manifest_path)

    def test_manifest_reorder_duplicate_and_policy_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root_cases, sources, _manifest_path, manifest = self._bundle(directory)
            reordered = copy.deepcopy(manifest)
            reordered["sources"][1:] = reversed(reordered["sources"][1:])
            reordered["source_manifest_sha256"] = (
                decision_case_corpus._manifest_digest(reordered["sources"])
            )
            with self.assertRaises(decision_case_corpus.CaseCorpusError):
                decision_case_corpus.validate_manifest(directory, reordered)

            duplicate = copy.deepcopy(manifest)
            duplicate["sources"].append(copy.deepcopy(duplicate["sources"][1]))
            duplicate["source_count"] += 1
            duplicate["source_manifest_sha256"] = (
                decision_case_corpus._manifest_digest(duplicate["sources"])
            )
            with self.assertRaises(decision_case_corpus.CaseCorpusError):
                decision_case_corpus.validate_manifest(directory, duplicate)

            bad = Path(directory) / "logs" / "attempts" / "attempt-c" / "decision-cases.jsonl"
            _write_jsonl(bad, [_case("attempt-c", 4, controller="controller-x")])
            # Two rows in one shard cannot claim different controllers.
            _write_jsonl(bad, [
                _case("attempt-c", 4),
                _case("attempt-c", 5, controller="controller-x"),
            ])
            with self.assertRaises(decision_case_corpus.CaseCorpusError):
                decision_case_corpus.build_manifest(
                    root_cases, [bad], base_manifest=manifest,
                )

    def test_manifest_validation_never_reopens_root_or_attempt_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            root_cases, sources, manifest_path, _manifest = self._bundle(directory)
            original_open = Path.open

            def guarded_open(path, *args, **kwargs):
                candidate = Path(path)
                if candidate == root_cases or candidate in sources:
                    raise AssertionError("runtime JSONL source was reopened")
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=guarded_open):
                cases = decision_case_corpus.load_case_corpus(
                    root_cases, manifest_path
                )
            self.assertEqual(3, len(cases))


if __name__ == "__main__":
    unittest.main()
