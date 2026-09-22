"""Strict ordered manifests for bounded DecisionCase source archives.

The historical root JSONL is capped at 128 MiB and live attempts write their
own shards.  At an optimization checkpoint each source is streamed once into
an independent deterministic gzip archive.  The manifest stays small; no new
ever-growing JSONL or aggregate payload is created, and replay never reopens
runtime logs.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_MODE = "ordered_bounded_case_archives_v1"
SOURCE_SCHEMA_VERSION = 1
SOURCE_MODE = "exact_jsonl_gzip_archive_v1"
DEFAULT_MANIFEST_NAME = "decision-case-corpus-sources.json"
ARCHIVE_DIRECTORY_NAME = "decision-case-corpus"
MAX_ROOT_BYTES = 128 * 1024 * 1024
MAX_ATTEMPT_SHARD_BYTES = 32 * 1024 * 1024


class CaseCorpusError(ValueError):
    pass


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _object_digest(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class _CorpusHasher:
    def __init__(self):
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self.count = 0

    def update(self, value):
        if self.count:
            self._digest.update(b",")
        self._digest.update(_canonical_bytes(value))
        self.count += 1

    def hexdigest(self):
        digest = self._digest.copy()
        digest.update(b"]")
        return digest.hexdigest()


def case_corpus_digest(cases):
    hasher = _CorpusHasher()
    for case in cases or []:
        hasher.update(case)
    return hasher.hexdigest()


def _valid_sha256(value):
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _file_sha256(path):
    digest = hashlib.sha256()
    size = 0
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise CaseCorpusError("DecisionCase archive is unreadable") from exc
    return size, digest.hexdigest()


def _attempt_from_source_path(label):
    if not isinstance(label, str) or not label or "\\" in label:
        raise CaseCorpusError("DecisionCase shard path is not canonical")
    path = Path(label)
    parts = path.parts
    if (
        path.is_absolute() or path.as_posix() != label or len(parts) != 4
        or parts[0] != "logs" or parts[1] != "attempts"
        or not parts[2] or parts[3] != "decision-cases.jsonl"
    ):
        raise CaseCorpusError(
            "DecisionCase shard is not an exact bounded attempt source"
        )
    return parts[2]


def _archive_label(kind, attempt_id, raw_sha256):
    stem = "root" if kind == "root" else attempt_id
    return (
        f"{ARCHIVE_DIRECTORY_NAME}/{stem}-{raw_sha256[:16]}.jsonl.gz"
    )


def _validate_archive_label(label):
    if not isinstance(label, str) or not label or "\\" in label:
        raise CaseCorpusError("DecisionCase archive path is not canonical")
    path = Path(label)
    if (
        path.is_absolute() or path.as_posix() != label or len(path.parts) != 2
        or path.parts[0] != ARCHIVE_DIRECTORY_NAME
        or not path.name.endswith(".jsonl.gz")
    ):
        raise CaseCorpusError("DecisionCase archive path is invalid")


def _parse_line(raw_line, *, source_name, line_number):
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaseCorpusError(
            f"{source_name} line {line_number} is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise CaseCorpusError(
            f"{source_name} line {line_number} is not an object"
        )
    return value


class _DigestWriter:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, value):
        self.digest.update(value)
        self.size += len(value)
        return self.handle.write(value)

    def flush(self):
        return self.handle.flush()


_SOURCE_FIELDS = {
    "schema_version", "mode", "source_id", "kind", "source_path",
    "archive_path", "attempt_id", "decision_hash", "controller_hash",
    "start_case_index", "raw_byte_length", "raw_sha256",
    "source_scan_pass_count", "case_count", "case_corpus_sha256",
    "archive_byte_length", "archive_sha256",
}
_MANIFEST_FIELDS = {
    "schema_version", "mode", "sources", "source_manifest_sha256",
    "source_count", "total_case_count", "case_corpus_sha256",
}


def _source_identity(source):
    return {key: source.get(key) for key in sorted(_SOURCE_FIELDS - {"source_id"})}


def _source_id(source):
    return "case-source:" + _object_digest(_source_identity(source))[:32]


def _source_contract(source, *, expected_start):
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise CaseCorpusError("DecisionCase source descriptor is invalid")
    kind = source.get("kind")
    if kind not in {"root", "attempt"}:
        raise CaseCorpusError("DecisionCase source kind is invalid")
    if kind == "root":
        if (
            source.get("source_path") != "decision-cases.jsonl"
            or source.get("attempt_id") is not None
            or source.get("decision_hash") is not None
            or source.get("controller_hash") is not None
            or expected_start != 0
        ):
            raise CaseCorpusError("DecisionCase root source identity is invalid")
        limit = MAX_ROOT_BYTES
    else:
        attempt_id = _attempt_from_source_path(source.get("source_path"))
        if (
            source.get("attempt_id") != attempt_id
            or not isinstance(source.get("decision_hash"), str)
            or not source.get("decision_hash")
            or not isinstance(source.get("controller_hash"), str)
            or not source.get("controller_hash")
        ):
            raise CaseCorpusError("DecisionCase attempt source binding is invalid")
        limit = MAX_ATTEMPT_SHARD_BYTES
    _validate_archive_label(source.get("archive_path"))
    if (
        source.get("schema_version") != SOURCE_SCHEMA_VERSION
        or source.get("mode") != SOURCE_MODE
        or source.get("source_id") != _source_id(source)
        or source.get("start_case_index") != expected_start
        or source.get("source_scan_pass_count") != 1
        or type(source.get("raw_byte_length")) is not int
        or not 0 < source["raw_byte_length"] <= limit
        or not _valid_sha256(source.get("raw_sha256"))
        or type(source.get("case_count")) is not int
        or source["case_count"] <= 0
        or not _valid_sha256(source.get("case_corpus_sha256"))
        or type(source.get("archive_byte_length")) is not int
        or source["archive_byte_length"] <= 0
        or not _valid_sha256(source.get("archive_sha256"))
    ):
        raise CaseCorpusError("DecisionCase source contract is invalid")
    return expected_start + source["case_count"]


def _scan_source_to_archive(root, source_path, *, kind, start_case_index):
    root = Path(root).resolve()
    source_path = Path(source_path).resolve()
    if kind == "root":
        if source_path != root / "decision-cases.jsonl":
            raise CaseCorpusError("DecisionCase root path is invalid")
        source_label = "decision-cases.jsonl"
        attempt_id = None
        limit = MAX_ROOT_BYTES
    else:
        try:
            source_label = source_path.relative_to(root).as_posix()
        except ValueError as exc:
            raise CaseCorpusError("DecisionCase shard escaped the repository") from exc
        attempt_id = _attempt_from_source_path(source_label)
        limit = MAX_ATTEMPT_SHARD_BYTES
        if source_path.with_name("decision-cases-limit.json").exists():
            raise CaseCorpusError("DecisionCase shard hit its fail-closed limit")

    archive_dir = root / ARCHIVE_DIRECTORY_NAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    temporary = archive_dir / f".{kind}-{attempt_id or 'root'}-{os.getpid()}.tmp"
    raw_digest = hashlib.sha256()
    corpus_digest = _CorpusHasher()
    raw_size = 0
    cases = []
    decision_hashes = set()
    controller_hashes = set()
    compressed_writer = None
    try:
        with source_path.open("rb") as source, temporary.open("wb") as output:
            compressed_writer = _DigestWriter(output)
            with gzip.GzipFile(
                fileobj=compressed_writer, mode="wb", compresslevel=9, mtime=0,
            ) as compressed:
                for line_number, raw_line in enumerate(source, start=1):
                    raw_size += len(raw_line)
                    if raw_size > limit:
                        raise CaseCorpusError("DecisionCase source size is invalid")
                    raw_digest.update(raw_line)
                    compressed.write(raw_line)
                    if not raw_line.strip():
                        continue
                    case = _parse_line(
                        raw_line, source_name=source_label,
                        line_number=line_number,
                    )
                    if kind == "attempt":
                        if case.get("attempt_id") != attempt_id:
                            raise CaseCorpusError(
                                "DecisionCase shard contains a different attempt"
                            )
                        decision_hashes.add(case.get("decision_hash"))
                        controller_hashes.add(case.get("controller_hash"))
                    corpus_digest.update(case)
                    cases.append(case)
            compressed_writer.flush()
        if not cases or raw_size <= 0:
            raise CaseCorpusError("DecisionCase source contains no cases")
        if kind == "attempt" and (
            len(decision_hashes) != 1 or len(controller_hashes) != 1
            or not all(isinstance(value, str) and value for value in decision_hashes)
            or not all(isinstance(value, str) and value for value in controller_hashes)
        ):
            raise CaseCorpusError(
                "DecisionCase shard policy binding is inconsistent"
            )
        raw_sha256 = raw_digest.hexdigest()
        archive_label = _archive_label(kind, attempt_id, raw_sha256)
        archive_path = root / Path(archive_label)
        os.replace(temporary, archive_path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CaseCorpusError("DecisionCase source is unreadable") from exc
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    source = {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "mode": SOURCE_MODE,
        "kind": kind,
        "source_path": source_label,
        "archive_path": archive_label,
        "attempt_id": attempt_id,
        "decision_hash": (
            next(iter(decision_hashes)) if kind == "attempt" else None
        ),
        "controller_hash": (
            next(iter(controller_hashes)) if kind == "attempt" else None
        ),
        "start_case_index": start_case_index,
        "raw_byte_length": raw_size,
        "raw_sha256": raw_sha256,
        "source_scan_pass_count": 1,
        "case_count": len(cases),
        "case_corpus_sha256": corpus_digest.hexdigest(),
        "archive_byte_length": compressed_writer.size,
        "archive_sha256": compressed_writer.digest.hexdigest(),
    }
    source["source_id"] = _source_id(source)
    return source, cases


def _load_archive(root, source):
    root = Path(root).resolve()
    archive_path = (root / Path(source["archive_path"])).resolve()
    try:
        archive_path.relative_to(root / ARCHIVE_DIRECTORY_NAME)
    except ValueError as exc:
        raise CaseCorpusError("DecisionCase archive escaped its directory") from exc
    try:
        compressed = archive_path.read_bytes()
    except OSError as exc:
        raise CaseCorpusError("DecisionCase archive is unreadable") from exc
    if (
        len(compressed) != source["archive_byte_length"]
        or hashlib.sha256(compressed).hexdigest() != source["archive_sha256"]
    ):
        raise CaseCorpusError("DecisionCase archive digest mismatch")

    raw_digest = hashlib.sha256()
    corpus_digest = _CorpusHasher()
    raw_size = 0
    cases = []
    decision_hashes = set()
    controller_hashes = set()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as payload:
            for line_number, raw_line in enumerate(payload, start=1):
                raw_size += len(raw_line)
                raw_digest.update(raw_line)
                if not raw_line.strip():
                    continue
                case = _parse_line(
                    raw_line, source_name=source["archive_path"],
                    line_number=line_number,
                )
                if source["kind"] == "attempt":
                    if case.get("attempt_id") != source["attempt_id"]:
                        raise CaseCorpusError(
                            "DecisionCase archive attempt binding mismatch"
                        )
                    decision_hashes.add(case.get("decision_hash"))
                    controller_hashes.add(case.get("controller_hash"))
                corpus_digest.update(case)
                cases.append(case)
    except (OSError, EOFError) as exc:
        raise CaseCorpusError("DecisionCase archive is not valid gzip") from exc
    if (
        raw_size != source["raw_byte_length"]
        or raw_digest.hexdigest() != source["raw_sha256"]
        or len(cases) != source["case_count"]
        or corpus_digest.hexdigest() != source["case_corpus_sha256"]
        or (
            source["kind"] == "attempt"
            and (
                decision_hashes != {source["decision_hash"]}
                or controller_hashes != {source["controller_hash"]}
            )
        )
    ):
        raise CaseCorpusError("DecisionCase archive content binding mismatch")
    return cases


def _manifest_digest(sources):
    return _object_digest(sources)


def validate_manifest(root, manifest):
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise CaseCorpusError("DecisionCase manifest envelope is invalid")
    sources = manifest.get("sources")
    if (
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or manifest.get("mode") != MANIFEST_MODE
        or not isinstance(sources, list) or not sources
        or manifest.get("source_count") != len(sources)
        or manifest.get("source_manifest_sha256") != _manifest_digest(sources)
    ):
        raise CaseCorpusError("DecisionCase manifest contract is invalid")
    cases = []
    corpus_digest = _CorpusHasher()
    source_ids = set()
    source_paths = set()
    archive_paths = set()
    attempt_ids = set()
    case_keys = set()
    expected_start = 0
    for index, source in enumerate(sources):
        expected_kind = "root" if index == 0 else "attempt"
        if not isinstance(source, dict) or source.get("kind") != expected_kind:
            raise CaseCorpusError("DecisionCase source order is invalid")
        expected_start = _source_contract(source, expected_start=expected_start)
        for identity, seen, label in (
            (source["source_id"], source_ids, "source identity"),
            (source["source_path"], source_paths, "source path"),
            (source["archive_path"], archive_paths, "archive path"),
        ):
            if identity in seen:
                raise CaseCorpusError(f"duplicate DecisionCase {label}")
            seen.add(identity)
        if source["kind"] == "attempt":
            if source["attempt_id"] in attempt_ids:
                raise CaseCorpusError("duplicate DecisionCase attempt source")
            attempt_ids.add(source["attempt_id"])
        source_cases = _load_archive(root, source)
        for case in source_cases:
            key = (case.get("attempt_id"), case.get("before_seq"), case.get("phase"))
            if key in case_keys:
                raise CaseCorpusError("duplicate DecisionCase semantic key")
            case_keys.add(key)
            corpus_digest.update(case)
        cases.extend(source_cases)
    if (
        manifest.get("total_case_count") != len(cases)
        or manifest.get("case_corpus_sha256") != corpus_digest.hexdigest()
    ):
        raise CaseCorpusError("DecisionCase manifest aggregate mismatch")
    return cases


def load_manifest(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseCorpusError("DecisionCase manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise CaseCorpusError("DecisionCase manifest is not an object")
    return value


def _manifest_file_digest(path):
    size, digest = _file_sha256(path)
    if size <= 0:
        raise CaseCorpusError("DecisionCase manifest is empty")
    return digest


def load_case_corpus(root_cases_path, manifest_path, *, with_evidence=False):
    root_cases_path = Path(root_cases_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    if root_cases_path.name != "decision-cases.jsonl":
        raise CaseCorpusError("DecisionCase root filename is invalid")
    if not manifest_path.exists():
        raise CaseCorpusError("DecisionCase source manifest is required")
    manifest = load_manifest(manifest_path)
    cases = validate_manifest(root_cases_path.parent, manifest)
    evidence = {
        "mode": MANIFEST_MODE,
        "manifest_sha256": _manifest_file_digest(manifest_path),
        "source_manifest_sha256": manifest["source_manifest_sha256"],
        "source_count": manifest["source_count"],
        "case_count": manifest["total_case_count"],
        "case_corpus_sha256": manifest["case_corpus_sha256"],
    }
    return (cases, evidence) if with_evidence else cases


def build_manifest(root_cases_path, attempt_source_paths, *, base_manifest=None):
    root_cases_path = Path(root_cases_path).resolve()
    root = root_cases_path.parent
    if base_manifest is None:
        root_source, cases = _scan_source_to_archive(
            root, root_cases_path, kind="root", start_case_index=0,
        )
        sources = [root_source]
    else:
        cases = validate_manifest(root, base_manifest)
        sources = json.loads(json.dumps(base_manifest["sources"]))
    existing_attempts = {
        source["attempt_id"] for source in sources if source["kind"] == "attempt"
    }
    existing_paths = {source["source_path"] for source in sources}
    requested = []
    for path in attempt_source_paths:
        resolved = Path(path).resolve()
        try:
            label = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise CaseCorpusError("DecisionCase shard escaped the repository") from exc
        attempt_id = _attempt_from_source_path(label)
        if (
            attempt_id in existing_attempts or label in existing_paths
            or any(row[1] == attempt_id or row[2] == label for row in requested)
        ):
            raise CaseCorpusError("DecisionCase shard is already in the manifest")
        requested.append((resolved, attempt_id, label))
    if not requested and base_manifest is not None:
        raise CaseCorpusError("no new DecisionCase shards were supplied")
    case_keys = {
        (case.get("attempt_id"), case.get("before_seq"), case.get("phase"))
        for case in cases
    }
    corpus_digest = _CorpusHasher()
    for case in cases:
        corpus_digest.update(case)
    for resolved, _attempt_id, _label in requested:
        source, source_cases = _scan_source_to_archive(
            root, resolved, kind="attempt", start_case_index=len(cases),
        )
        for case in source_cases:
            key = (case.get("attempt_id"), case.get("before_seq"), case.get("phase"))
            if key in case_keys:
                raise CaseCorpusError("duplicate DecisionCase semantic key")
            case_keys.add(key)
            corpus_digest.update(case)
        sources.append(source)
        cases.extend(source_cases)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "mode": MANIFEST_MODE,
        "sources": sources,
        "source_manifest_sha256": _manifest_digest(sources),
        "source_count": len(sources),
        "total_case_count": len(cases),
        "case_corpus_sha256": corpus_digest.hexdigest(),
    }


def write_manifest(path, manifest):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-cases", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args(argv)
    base = load_manifest(args.base) if args.base else None
    manifest = build_manifest(
        args.root_cases, args.source, base_manifest=base,
    )
    write_manifest(args.write, manifest)
    print(json.dumps({
        "schema_version": manifest["schema_version"],
        "source_count": manifest["source_count"],
        "total_case_count": manifest["total_case_count"],
        "case_corpus_sha256": manifest["case_corpus_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
