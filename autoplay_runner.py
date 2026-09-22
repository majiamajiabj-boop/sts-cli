"""Outer supervisor that lands a post-process controller exit receipt.

``autoplay.py`` can prove what happened up to its terminal write, but it cannot
prove its own final exit code or that stderr stayed empty.  This process owns
that final observation.  A run is selector-eligible only after this supervisor
has waited for the child and appended one exact, clean ``controller_exit``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import campaign_selector
import cohort_report
import cohort_review
import freeze_manifest
import pre_run_binding


ROOT = Path(__file__).resolve().parent
POLICY_VERSION = "fast-policy-v5"
SCHEMA_VERSION = 2
RESULT_NAME = "run-result.json"
CONTEXT_NAME = "run-context.json"
STATE_NAME = "state.json"
SELECTION_NAME = "next-run-selection.json"
HISTORY_NAME = "run-history.jsonl"
COHORT_REPORT_NAME = "cohort-report.json"
COHORT_REVIEW_NAME = "cohort-review.json"
FREEZE_MANIFEST_NAME = "freeze-manifest.json"
LAUNCH_NAME = "launch-latest.json"
ATTEMPT_LOG_ROOT = Path("logs") / "attempts"
RUNNER_LOCK_NAME = ".autoplay-runner.lock"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
BINDING_FIELDS = (
    "policy_version",
    "attempt_id",
    "run_id",
    "seed",
    "character",
    "ascension_level",
    "run_type",
    "decision_hash",
    "controller_hash",
    "selection_id",
    "selection_digest",
    "terminal_state_seq",
)


class RunnerError(RuntimeError):
    """Raised when the outer receipt cannot be landed without ambiguity."""


class RunnerLease:
    """Prevent two supervisors from racing around the child's own lease."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise RunnerError("another autoplay runner owns the lease") from exc
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, traceback):
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _attempt_component(attempt_id):
    raw = str(attempt_id or "").strip()
    if not raw:
        raise RunnerError("attempt_id is missing")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def attempt_directory(root, attempt_id):
    return Path(root) / ATTEMPT_LOG_ROOT / _attempt_component(attempt_id)


def _read_object(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunnerError(f"{label} is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"{label} is not an object")
    return value


def _read_optional_bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _write_once(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "xb"
    data = payload if isinstance(payload, bytes) else bytes(payload)
    try:
        with path.open(mode) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise RunnerError(f"refusing to overwrite existing evidence: {path}") from exc


def _write_json_once(path, payload):
    _write_once(
        path,
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
    )


def _append_history(path, record):
    encoded = (
        json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with Path(path).open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _same_typed_value(actual, expected):
    return type(actual) is type(expected) and actual == expected


def _binding_from_result(result):
    if (
        type(result.get("schema_version")) is not int
        or result.get("schema_version") != SCHEMA_VERSION
    ):
        raise RunnerError("run result schema_version is not 2")
    if result.get("policy_version") != POLICY_VERSION:
        raise RunnerError("run result policy_version is unsupported")
    if not _nonempty_string(result.get("attempt_id")):
        raise RunnerError("run result attempt_id is missing")
    if not _nonempty_string(result.get("run_id")):
        raise RunnerError("run result run_id is missing")
    if not _nonempty_string(result.get("decision_hash")):
        raise RunnerError("run result decision_hash is missing")
    if not _nonempty_string(result.get("controller_hash")):
        raise RunnerError("run result controller_hash is missing")
    if not _nonempty_string(result.get("selection_id")):
        raise RunnerError("run result selection_id is missing")
    if result.get("character") not in campaign_selector.CHARACTERS:
        raise RunnerError("run result character is unsupported")
    if type(result.get("ascension_level")) is not int:
        raise RunnerError("run result ascension_level is not an integer")
    if result.get("ascension_level") != 0 or result.get("run_type") != "standard":
        raise RunnerError("run result is not standard A0")
    if type(result.get("terminal_state_seq")) is not int:
        raise RunnerError("run result terminal_state_seq is not an integer")
    if result.get("run_id") != (
        f"{result.get('character')}:0:{result.get('seed')}"
    ):
        raise RunnerError("run result run_id does not match character/A0/seed")
    return {field: result.get(field) for field in BINDING_FIELDS}


def _validate_bound_artifact(artifact, binding, label):
    for field, expected in binding.items():
        actual = artifact.get(field)
        if not _same_typed_value(actual, expected):
            raise RunnerError(f"{label} {field} binding mismatch")


def _require_schema_version(artifact, label):
    if type(artifact.get("schema_version")) is not int:
        raise RunnerError(f"{label} schema_version is not an integer")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError(f"{label} schema_version is not 2")


def _state_summary(state, binding):
    game = state.get("game_state") if isinstance(state.get("game_state"), dict) else {}
    return {
        **binding,
        "protocol_version": state.get("protocol_version"),
        "state_seq": state.get("state_seq"),
        "screen_type": game.get("screen_type", state.get("screen_type")),
        "phase": state.get("phase"),
        "decision_id": state.get("decision_id"),
        "victory": game.get("victory"),
    }


def _load_evidence(root, result, *, require_terminal_snapshot):
    root = Path(root)
    binding = _binding_from_result(result)
    directory = attempt_directory(root, binding["attempt_id"])
    context = _read_object(root / CONTEXT_NAME, "run context")
    _require_schema_version(context, "run context")
    _validate_bound_artifact(context, binding, "run context")

    attempt_result = _read_object(directory / RESULT_NAME, "attempt run result")
    if attempt_result != result:
        raise RunnerError("attempt run result differs from authoritative run result")
    selection = _read_object(directory / "selection.json", "selection snapshot")
    _require_schema_version(selection, "selection snapshot")
    _validate_bound_artifact(selection, binding, "selection snapshot")
    if selection.get("selection_id") != context.get("selection_id"):
        raise RunnerError("selection snapshot differs from run context")
    context_selection = context.get("selection")
    if not isinstance(context_selection, dict):
        raise RunnerError("run context selection is missing")
    if result.get("selection") != context_selection:
        raise RunnerError("run result selection differs from run context")
    try:
        digest = campaign_selector.selection_digest(context_selection)
        acceptance = pre_run_binding.load_start_acceptance(
            root, context_selection
        )
    except (ValueError, pre_run_binding.PreRunBindingError) as exc:
        raise RunnerError(str(exc)) from exc
    if binding.get("selection_digest") != digest:
        raise RunnerError("selection digest differs from accepted selection")
    for field, expected in context_selection.items():
        if not _same_typed_value(selection.get(field), expected):
            raise RunnerError(
                f"selection snapshot {field} differs from run context"
            )
    audit = _read_object(directory / "run-audit.json", "attempt audit")
    _require_schema_version(audit, "attempt audit")
    _validate_bound_artifact(audit, binding, "attempt audit")

    state_path = directory / "terminal-state.json"
    if not state_path.exists() and not require_terminal_snapshot:
        state_path = root / STATE_NAME
    state = _read_object(state_path, "authoritative state snapshot")
    if type(state.get("protocol_version")) is not int:
        raise RunnerError("authoritative state protocol_version is not an integer")
    if state.get("protocol_version") != 2:
        raise RunnerError("authoritative state protocol_version is not 2")
    _validate_bound_artifact(state, binding, "authoritative state snapshot")
    if state.get("state_seq") != binding["terminal_state_seq"]:
        raise RunnerError("state_seq does not equal terminal_state_seq")
    if require_terminal_snapshot:
        game = state.get("game_state") or {}
        if game.get("screen_type") != "GAME_OVER":
            raise RunnerError("terminal snapshot is not authoritative GAME_OVER")
    return {
        "binding": binding,
        "directory": directory,
        "context": context,
        "selection": selection,
        "state": state,
        "audit": audit,
    }


def _load_failure_binding(root, result):
    """Bind failure evidence without pretending that a terminal was valid."""

    root = Path(root)
    binding = _binding_from_result(result)
    context = _read_object(root / CONTEXT_NAME, "run context")
    _require_schema_version(context, "run context")
    nonterminal_binding = {
        field: value for field, value in binding.items()
        if field != "terminal_state_seq"
    }
    _validate_bound_artifact(context, nonterminal_binding, "run context")
    directory = attempt_directory(root, binding["attempt_id"])

    def validate_state(state, label, *, require_terminal_sequence=True):
        if type(state.get("protocol_version")) is not int:
            raise RunnerError(
                f"{label} protocol_version is not an integer"
            )
        if state.get("protocol_version") != 2:
            raise RunnerError(f"{label} protocol_version is not 2")
        if state.get("in_game") is not True:
            raise RunnerError(f"{label} is not an in-game frame")
        _validate_bound_artifact(state, nonterminal_binding, label)
        if not _same_typed_value(
            state.get("state_seq"), binding["terminal_state_seq"]
        ):
            raise RunnerError(f"{label} sequence is not bound")
        if require_terminal_sequence:
            if not _same_typed_value(
                state.get("terminal_state_seq"),
                binding["terminal_state_seq"],
            ):
                raise RunnerError(
                    f"{label} terminal sequence is not bound"
                )
        elif (
            "terminal_state_seq" in state
            and not _same_typed_value(
                state.get("terminal_state_seq"),
                binding["terminal_state_seq"],
            )
        ):
            raise RunnerError(f"{label} terminal sequence conflicts")
        return state

    def embedded_state():
        if result.get("termination_kind") != "operational_error":
            raise RunnerError(
                "embedded state fallback is only valid for operational_error"
            )
        embedded = result.get("last_authoritative_state")
        if not isinstance(embedded, dict):
            raise RunnerError(
                "bound operational fallback is missing"
            )
        validate_state(
            embedded, "embedded last authoritative state"
        )
        canonical = canonical_operational_state(embedded)
        if not same_typed_tree(embedded, canonical):
            raise RunnerError(
                "embedded last authoritative state is not the complete "
                "canonical projection"
            )
        return embedded

    def same_typed_tree(actual, expected):
        if type(actual) is not type(expected):
            return False
        if isinstance(expected, dict):
            return set(actual) == set(expected) and all(
                same_typed_tree(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return len(actual) == len(expected) and all(
                same_typed_tree(left, right)
                for left, right in zip(actual, expected)
            )
        return actual == expected

    def canonical_operational_state(state):
        """Rebuild the controller's complete bounded state projection."""

        # Keep one authority for the bounded evidence schema.  Importing here
        # avoids loading the controller and planner stack on clean exits.
        import autoplay

        canonical = autoplay.authoritative_state_snapshot(state)
        canonical.update({
            "protocol_version": state.get("protocol_version"),
            "in_game": state.get("in_game"),
            "terminal_state_seq": state.get("state_seq"),
        })
        for field in autoplay.ATTEMPT_BINDING_FIELDS:
            canonical[field] = state.get(field)
        return canonical

    def require_live_embedded_match(live, embedded):
        """Require typed equality with the full canonical live projection.

        Raw live state may contain presentation/map extras, but the embedded
        evidence must contain every field emitted by the controller's bounded
        schema.  Exact typed equality of the two canonical trees prevents a
        truncated snapshot from becoming an outer exit receipt.
        """

        canonical = canonical_operational_state(live)
        if not same_typed_tree(canonical, embedded):
            raise RunnerError(
                "live authoritative state canonical projection differs from "
                "embedded operational state"
            )

    def persist_embedded_fallback(state):
        fallback_path = directory / "last-authoritative-state.json"
        if fallback_path.exists():
            existing = _read_object(
                fallback_path, "attempt last authoritative state"
            )
            if existing != state:
                raise RunnerError(
                    "attempt last authoritative fallback already differs"
                )
        else:
            _write_json_once(fallback_path, state)
        return state

    state = None
    unreadable = []
    fallback_path = directory / "last-authoritative-state.json"
    try:
        candidate = _read_object(
            fallback_path, "attempt last authoritative state"
        )
    except RunnerError as exc:
        unreadable.append(str(exc))
    else:
        # An attempt-scoped fallback is already immutable evidence and must
        # carry the synthetic terminal sequence marker.
        state = validate_state(
            candidate, "attempt last authoritative state"
        )
        if result.get("termination_kind") == "operational_error":
            embedded = embedded_state()
            if candidate != embedded:
                raise RunnerError(
                    "attempt last authoritative state differs from embedded "
                    "operational state"
                )

    if state is None:
        try:
            live = _read_object(root / STATE_NAME, "last authoritative state")
        except RunnerError as exc:
            unreadable.append(str(exc))
        else:
            # The root protocol frame is still in-game and therefore normally
            # lacks terminal_state_seq.  Permit only that single omission,
            # then corroborate the controller's bounded embedded snapshot
            # against every corresponding live fact before persisting it.
            validate_state(
                live,
                "last authoritative state",
                require_terminal_sequence=False,
            )
            if "terminal_state_seq" in live:
                validate_state(live, "last authoritative state")
                if result.get("termination_kind") == "operational_error":
                    embedded = embedded_state()
                    require_live_embedded_match(live, embedded)
                    state = persist_embedded_fallback(embedded)
                else:
                    state = live
            else:
                embedded = embedded_state()
                require_live_embedded_match(live, embedded)
                state = persist_embedded_fallback(embedded)

    if state is None:
        try:
            state = persist_embedded_fallback(embedded_state())
        except RunnerError as exc:
            detail = "; ".join((*unreadable, str(exc))) or "no state source"
            raise RunnerError(
                "last authoritative state is unavailable and the bound "
                f"operational fallback is invalid: {detail}"
            ) from exc
    return {
        "binding": binding,
        "directory": directory,
        "context": context,
        "state": state,
    }


def _history_for_attempt(root, attempt_id):
    records = campaign_selector.load_history(Path(root) / HISTORY_NAME)
    return records, [
        (index, record)
        for index, record in enumerate(records)
        if record.get("attempt_id") == attempt_id
    ]


def append_controller_exit(root, record, *, require_audit=True):
    """Append one receipt after its unique terminal/audit history chain."""

    root = Path(root)
    attempt_id = record.get("attempt_id")
    records, matching = _history_for_attempt(root, attempt_id)
    terminals = [
        (index, item) for index, item in matching
        if item.get("record_type") == campaign_selector.TERMINAL_RECORD_TYPE
    ]
    audits = [
        (index, item) for index, item in matching
        if item.get("record_type") == campaign_selector.AUDIT_RECORD_TYPE
    ]
    exits = [
        (index, item) for index, item in matching
        if item.get("record_type") == campaign_selector.CONTROLLER_EXIT_RECORD_TYPE
    ]
    sidecar_path = attempt_directory(root, attempt_id) / "controller-exit.json"
    if exits:
        if len(exits) != 1:
            raise RunnerError("duplicate controller exits exist in history")
        sidecar = _read_object(sidecar_path, "controller exit sidecar")
        if sidecar != exits[0][1] or sidecar != record:
            raise RunnerError("controller exit sidecar/history mismatch")
        raise RunnerError("controller exit already exists for this attempt")
    sidecar_pending_history = False
    if sidecar_path.exists():
        sidecar = _read_object(sidecar_path, "controller exit sidecar")
        if sidecar != record:
            raise RunnerError("controller exit sidecar differs from new receipt")
        # The sidecar is written and fsynced before the append-only history.
        # A supervisor crash in that narrow interval is recoverable only when
        # the caller supplies the byte-equivalent receipt and every source
        # artifact below still validates.  Never manufacture a different exit.
        sidecar_pending_history = True
    if len(terminals) != 1:
        raise RunnerError("controller exit requires one unique terminal history record")
    if len(audits) > 1 or (require_audit and len(audits) != 1):
        raise RunnerError("controller exit requires one unique audit history record")
    terminal_index, terminal = terminals[0]
    binding = _binding_from_result(terminal)
    _validate_bound_artifact(record, binding, "controller exit")
    if (
        record.get("record_type")
        != campaign_selector.CONTROLLER_EXIT_RECORD_TYPE
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != SCHEMA_VERSION
        or record.get("policy_version") != POLICY_VERSION
    ):
        raise RunnerError("controller exit envelope is invalid")
    if record.get("controller_exit_status") == "clear":
        exit_error = campaign_selector._controller_exit_validation_error(
            record, terminal
        )
        if exit_error is not None:
            raise RunnerError(f"clean controller exit is invalid: {exit_error}")
        freeze_evidence = _validate_current_freeze(root, binding)
        for field, expected in freeze_evidence.items():
            if not _same_typed_value(record.get(field), expected):
                raise RunnerError(
                    f"clean controller exit {field} mismatch"
                )
    elif (
        record.get("controller_exit_status") != "issues"
        or not isinstance(record.get("controller_exit_audit"), dict)
        or record["controller_exit_audit"].get("release_gate_passed") is not False
    ):
        raise RunnerError("failed controller exit has no blocking audit summary")
    if type(record.get("exit_code")) is not int:
        raise RunnerError("controller exit_code is not an integer")
    attempt_dir = attempt_directory(root, attempt_id)
    try:
        stdout = (attempt_dir / "controller.stdout.log").read_bytes()
        stderr = (attempt_dir / "controller.stderr.log").read_bytes()
    except OSError as exc:
        raise RunnerError("controller stream evidence is missing") from exc
    observed_output = _output_summary(
        stdout, stderr, record.get("exit_code")
    )
    for field, expected in observed_output.items():
        if not _same_typed_value(record.get(field), expected):
            raise RunnerError(f"controller stream {field} mismatch")
    if record.get("controller_exit_status") == "clear":
        _validate_bridge_stderr_output(record, root, require_clean=True)
    elif record.get("bridge_stderr_evidence_status") == "clear":
        _validate_bridge_stderr_output(record, root, require_clean=False)
        if (
            record.get("bridge_stderr_error") is not None
            or record.get("bridge_stderr_delta_size") != 0
        ):
            raise RunnerError(
                "failed controller exit bridge stderr clear claim is invalid"
            )
    elif record.get("bridge_stderr_evidence_status") == "issues":
        # Complete non-empty deltas are still independently reproducible.
        # An I/O/provenance exception is retained by the P1 audit instead.
        if record.get("bridge_stderr_error") is None:
            _validate_bridge_stderr_output(record, root, require_clean=False)
        elif not _nonempty_string(record.get("bridge_stderr_error")):
            raise RunnerError("failed bridge stderr evidence has no reason")
    else:
        raise RunnerError(
            "failed controller exit has no bridge stderr evidence"
        )
    authoritative_result = _read_object(
        root / RESULT_NAME, "authoritative run result"
    )
    if record.get("controller_exit_status") == "clear":
        semantic_output = _terminal_stdout_evidence(
            stdout, authoritative_result
        )
        for field, expected in semantic_output.items():
            if not _same_typed_value(record.get(field), expected):
                raise RunnerError(f"controller stream {field} mismatch")
    expected_terminal = {
        **authoritative_result,
        "record_type": campaign_selector.TERMINAL_RECORD_TYPE,
    }
    if terminal != expected_terminal:
        raise RunnerError("terminal history differs from authoritative run result")
    if audits:
        attempt_audit_path = (
            attempt_directory(root, attempt_id) / "run-audit.json"
        )
        artifact_audit = _read_object(
            attempt_audit_path, "attempt audit artifact"
        )
        for field, expected in audits[0][1].items():
            if field == "record_type":
                continue
            if not _same_typed_value(artifact_audit.get(field), expected):
                raise RunnerError(
                    f"audit history {field} differs from attempt audit"
                )
    if audits and audits[0][0] <= terminal_index:
        raise RunnerError("attempt audit does not follow its terminal")
    last_bound_index = audits[0][0] if audits else terminal_index
    if any(
        index > terminal_index
        and item.get("record_type") == campaign_selector.TERMINAL_RECORD_TYPE
        for index, item in enumerate(records)
    ):
        raise RunnerError("a later terminal prevents attaching controller exit")
    if last_bound_index != max(index for index, _ in matching):
        raise RunnerError("unexpected attempt history follows the audit")
    if not sidecar_pending_history:
        _write_json_once(sidecar_path, record)
    _append_history(root / HISTORY_NAME, record)
    return record


def _validate_current_freeze(root, binding):
    """Re-prove the launch freeze after the child has stopped."""

    root = Path(root)
    path = root / FREEZE_MANIFEST_NAME
    manifest = _read_object(path, "freeze manifest")
    try:
        freeze_manifest.validate_manifest(
            manifest,
            root,
            binding["decision_hash"],
            binding["controller_hash"],
        )
        raw = path.read_bytes()
    except (freeze_manifest.FreezeManifestError, OSError) as exc:
        raise RunnerError(f"post-run freeze validation failed: {exc}") from exc
    launch = manifest.get("launch_evidence")
    if not isinstance(launch, dict):
        raise RunnerError("post-run freeze has no launch evidence")
    return {
        "freeze_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "freeze_source_digest": manifest.get("source_digest"),
        "freeze_generated_at": manifest.get("generated_at"),
        "freeze_launch_record_sha256": launch.get(
            "launch_record_sha256"
        ),
        "freeze_launch_start_record_path": launch.get(
            "launch_start_record_path"
        ),
        "freeze_launch_start_record_sha256": launch.get(
            "launch_start_record_sha256"
        ),
        "freeze_launch_id": launch.get("launch_id"),
        "freeze_launcher_pid": launch.get("launcher_pid"),
        "freeze_java_pid": launch.get("java_pid"),
        "freeze_bridge_instance_sha256": launch.get(
            "bridge_instance_sha256"
        ),
        "freeze_bridge_instance_token": launch.get(
            "bridge_instance_token"
        ),
        "freeze_bridge_pid": launch.get("bridge_pid"),
        "freeze_bridge_launch_id": launch.get("bridge_launch_id"),
        "freeze_bridge_sha256": launch.get("bridge_sha256"),
        "freeze_bridge_started_at": launch.get("bridge_started_at"),
        "freeze_root_communication_mod_jar_sha256": launch.get(
            "root_communication_mod_jar_sha256"
        ),
        "freeze_installed_communication_mod_jar_sha256": launch.get(
            "installed_communication_mod_jar_sha256"
        ),
    }


def _optional_artifact_digest(path):
    try:
        value = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(value).hexdigest()


def _ensure_attempt_blocking_review(root, result, controller_exit):
    """Persist the immediate non-cohort Act4/Heart review for a failed gate.

    Cohort reports intentionally contain only fully eligible triples.  That
    must not make an Act4 run disappear merely because its audit or process
    exit is non-clear.  This sidecar is bound directly to the terminal and is
    permanently ineligible; it preserves the evidence needed for repair.
    """

    observed_max_act = result.get("observed_max_act")
    heart_candidate = result.get("heart_defeated") is True
    hidden_entry = type(observed_max_act) is int and observed_max_act >= 4
    if not hidden_entry and not heart_candidate:
        return None
    if controller_exit.get("controller_exit_status") == "clear":
        return None

    root = Path(root)
    binding = _binding_from_result(result)
    directory = attempt_directory(root, binding["attempt_id"])
    audit_path = directory / "run-audit.json"
    audit = None
    evidence_errors = []
    if audit_path.exists():
        try:
            audit = _read_object(audit_path, "attempt audit")
            _require_schema_version(audit, "attempt audit")
            _validate_bound_artifact(audit, binding, "attempt audit")
        except RunnerError as exc:
            audit = None
            evidence_errors.append(str(exc))

    replay_path = directory / "death-replay.json"
    replay = None
    if replay_path.exists():
        try:
            replay = _read_object(replay_path, "death replay")
            _validate_bound_artifact(replay, binding, "death replay")
        except RunnerError as exc:
            replay = None
            evidence_errors.append(str(exc))

    artifact_paths = {
        "trace": directory / "autoplay.log",
        "context": directory / "run-context.json",
        "result": directory / "run-result.json",
        "terminal_state": directory / "terminal-state.json",
        "selection": directory / "selection.json",
        "audit": audit_path,
        "death_replay": replay_path,
        "controller_exit": directory / "controller-exit.json",
        "controller_stdout": directory / "controller.stdout.log",
        "controller_stderr": directory / "controller.stderr.log",
    }
    digests = {
        name: _optional_artifact_digest(path)
        for name, path in artifact_paths.items()
    }
    missing = sorted(name for name, value in digests.items() if value is None)
    audit_issue_count = audit.get("issue_count") if isinstance(audit, dict) else None
    audit_unknown = (
        audit.get("eligible_unknown_count", audit.get("eligible_unknown"))
        if isinstance(audit, dict) else None
    )
    oracle = audit.get("independent_oracle") if isinstance(audit, dict) else None
    replay_summary = {
        "status": replay.get("status") if isinstance(replay, dict) else "missing",
        "issue_count": replay.get("issue_count") if isinstance(replay, dict) else None,
        "eligible_unknown_count": (
            replay.get("eligible_unknown_count")
            if isinstance(replay, dict) else None
        ),
    }
    findings = [{
        "severity": "P1",
        "kind": (
            "noneligible_heart_candidate_requires_repair"
            if heart_candidate
            else "noneligible_act4_attempt_requires_repair"
        ),
        "attempt_id": binding["attempt_id"],
        "terminal_state_seq": binding["terminal_state_seq"],
        "controller_exit_status": controller_exit.get(
            "controller_exit_status"
        ),
        "audit_status": audit.get("audit_status") if isinstance(audit, dict) else "missing",
    }]
    if missing:
        findings.append({
            "severity": "P1",
            "kind": "blocking_review_evidence_missing",
            "artifacts": missing,
        })
    if evidence_errors:
        findings.append({
            "severity": "P1",
            "kind": "blocking_review_evidence_binding_invalid",
            "errors": evidence_errors,
        })
    base = {
        "record_type": "attempt_blocking_review",
        "schema_version": SCHEMA_VERSION,
        **binding,
        "review_kind": (
            "heart_candidate_noneligible"
            if heart_candidate else "act4_failure_noneligible"
        ),
        "review_status": "issues",
        "release_gate_passed": False,
        "eligible_for_cohort": False,
        "termination_kind": result.get("termination_kind"),
        "authoritative_game_over": result.get("authoritative_game_over"),
        "screen_type": result.get("screen_type"),
        "observed_max_act": observed_max_act,
        "hidden_entry": hidden_entry,
        "victory": result.get("victory"),
        "heart_defeated": result.get("heart_defeated"),
        "audit": {
            "status": audit.get("audit_status") if isinstance(audit, dict) else "missing",
            "release_gate_passed": (
                audit.get("release_gate_passed")
                if isinstance(audit, dict) else False
            ),
            "issue_count": audit_issue_count,
            "review_finding_count": (
                audit.get("review_finding_count")
                if isinstance(audit, dict) else None
            ),
            "eligible_unknown_count": audit_unknown,
            "oracle": oracle,
        },
        "death_replay": replay_summary,
        "controller_exit": {
            "status": controller_exit.get("controller_exit_status"),
            "exit_code": controller_exit.get("exit_code"),
            "stdout_sha256": controller_exit.get("stdout_sha256"),
            "stderr_sha256": controller_exit.get("stderr_sha256"),
        },
        "artifact_sha256": digests,
        "issue_count": max(
            1,
            audit_issue_count
            if type(audit_issue_count) is int and audit_issue_count >= 0
            else 1,
        ) + len(missing) + len(evidence_errors),
        "review_finding_count": len(findings),
        "eligible_unknown_count": (
            audit_unknown
            if type(audit_unknown) is int and audit_unknown >= 0
            else 1
        ),
        "oracle_disagreement_count": (
            oracle.get("disagreement_count")
            if isinstance(oracle, dict)
            and type(oracle.get("disagreement_count")) is int
            else 1
        ),
        "findings": findings,
    }
    path = directory / "attempt-blocking-review.json"
    if path.exists():
        existing = _read_object(path, "attempt blocking review")
        for field, expected in base.items():
            if not _same_typed_value(existing.get(field), expected):
                raise RunnerError(
                    f"attempt blocking review {field} mismatch"
                )
        return existing
    review = {**base, "generated_at": time.time()}
    _write_json_once(path, review)
    return review


def _refresh_cohort(root, decision_hash):
    root = Path(root)
    report = cohort_report.build_cohort_report(
        campaign_selector.load_history(root / HISTORY_NAME), decision_hash
    )
    cohort_report.write_report(root / COHORT_REPORT_NAME, report)
    requirement = cohort_review.review_requirement(report)
    if requirement is None:
        return report

    review_path = root / COHORT_REVIEW_NAME
    attempt_root = root / ATTEMPT_LOG_ROOT
    existing_review = None
    if review_path.exists():
        try:
            existing_review = _read_object(
                review_path, "cohort review"
            )
        except RunnerError:
            existing_review = None
    if existing_review is not None and cohort_review.validate_review_binding(
        existing_review, report, attempt_root
    ):
        if cohort_review.validate_review_gate(
            existing_review, report, attempt_root
        ):
            return report
        raise RunnerError(
            f"mandatory {requirement} cohort review is not clear"
        )

    try:
        review = cohort_review.build_review(
            report, attempt_root, requirement
        )
    except cohort_review.CohortReviewError as exc:
        blocking = cohort_review.build_blocking_review(
            report, requirement, exc
        )
        cohort_review.write_review(review_path, blocking)
        raise RunnerError(
            f"mandatory {requirement} cohort review is inconclusive: {exc}"
        ) from exc
    cohort_review.write_review(review_path, review)
    if not cohort_review.validate_review_gate(review, report, attempt_root):
        raise RunnerError(
            f"mandatory {requirement} cohort review is not clear"
        )
    return report


def _reconcile_completed_receipt(root):
    """Finish post-exit gates without ever rerunning a completed attempt."""

    root = Path(root)
    # A pending one-use selection proves that campaign_attempt has already
    # committed START for a newer run but the child has not yet created its
    # context and consumed that selection.  The root result/context pair still
    # names the preceding attempt in this short interval, so reconciling it
    # would strand the newly-started run without a controller.
    if (root / SELECTION_NAME).exists():
        return None
    if not (root / RESULT_NAME).exists() or not (root / CONTEXT_NAME).exists():
        return None
    try:
        result = _read_object(root / RESULT_NAME, "authoritative run result")
        binding = _binding_from_result(result)
        context = _read_object(root / CONTEXT_NAME, "run context")
    except RunnerError:
        return None
    # A stale result from the previous attempt is normal after a new run has
    # started.  Only reconcile when the current context is exactly that result.
    if context.get("attempt_id") != binding["attempt_id"]:
        return None
    _require_schema_version(context, "run context")
    _validate_bound_artifact(context, binding, "run context")

    attempt_id = binding["attempt_id"]
    directory = attempt_directory(root, attempt_id)
    sidecar_path = directory / "controller-exit.json"
    records, matching = _history_for_attempt(root, attempt_id)
    terminals = [
        (index, item) for index, item in matching
        if item.get("record_type") == campaign_selector.TERMINAL_RECORD_TYPE
    ]
    audits = [
        (index, item) for index, item in matching
        if item.get("record_type") == campaign_selector.AUDIT_RECORD_TYPE
    ]
    exits = [
        (index, item) for index, item in matching
        if item.get("record_type") == campaign_selector.CONTROLLER_EXIT_RECORD_TYPE
    ]
    if not sidecar_path.exists() and not exits:
        if terminals or audits or (directory / RESULT_NAME).exists():
            raise RunnerError(
                "completed attempt is missing its controller exit receipt"
            )
        return None
    if sidecar_path.exists() and not exits:
        # Resume the only recoverable outer-receipt crash point: the immutable
        # sidecar landed, but its identical append-only history record did not.
        orphan = _read_object(sidecar_path, "controller exit sidecar")
        append_controller_exit(
            root,
            orphan,
            require_audit=(
                orphan.get("controller_exit_status") == "clear"
            ),
        )
        records, matching = _history_for_attempt(root, attempt_id)
        terminals = [
            (index, item) for index, item in matching
            if item.get("record_type")
            == campaign_selector.TERMINAL_RECORD_TYPE
        ]
        audits = [
            (index, item) for index, item in matching
            if item.get("record_type")
            == campaign_selector.AUDIT_RECORD_TYPE
        ]
        exits = [
            (index, item) for index, item in matching
            if item.get("record_type")
            == campaign_selector.CONTROLLER_EXIT_RECORD_TYPE
        ]
    if len(terminals) != 1 or len(audits) != 1 or len(exits) != 1:
        raise RunnerError(
            "completed attempt has an incomplete or duplicate exit chain"
        )
    sidecar = _read_object(sidecar_path, "controller exit sidecar")
    if sidecar != exits[0][1]:
        raise RunnerError("controller exit sidecar/history mismatch")
    _validate_bound_artifact(sidecar, binding, "controller exit")
    if sidecar.get("controller_exit_status") != "clear":
        _ensure_attempt_blocking_review(root, result, sidecar)
        raise RunnerError("completed attempt has a non-clear controller exit")

    evidence = _load_evidence(
        root, result, require_terminal_snapshot=True
    )
    expected_terminal = {
        **result,
        "record_type": campaign_selector.TERMINAL_RECORD_TYPE,
    }
    expected_audit = {
        **{
            field: evidence["audit"].get(field)
            for field in cohort_review.HISTORY_AUDIT_FIELDS
        },
        "record_type": campaign_selector.AUDIT_RECORD_TYPE,
    }
    if terminals[0][1] != expected_terminal:
        raise RunnerError("terminal history differs from authoritative run result")
    if audits[0][1] != expected_audit:
        raise RunnerError("audit history differs from attempt audit")
    if not (terminals[0][0] < audits[0][0] < exits[0][0]):
        raise RunnerError("completed attempt exit chain is out of order")
    if exits[0][0] != max(index for index, _ in matching):
        raise RunnerError("unexpected attempt history follows controller exit")
    exit_error = campaign_selector._controller_exit_validation_error(
        sidecar, result
    )
    if exit_error is not None:
        raise RunnerError(f"existing controller exit is invalid: {exit_error}")
    try:
        stdout = (directory / "controller.stdout.log").read_bytes()
        stderr = (directory / "controller.stderr.log").read_bytes()
    except OSError as exc:
        raise RunnerError("controller stream evidence is missing") from exc
    observed_output = _output_summary(stdout, stderr, sidecar["exit_code"])
    for field, expected in observed_output.items():
        if not _same_typed_value(sidecar.get(field), expected):
            raise RunnerError(f"controller stream {field} mismatch")
    semantic_output = _terminal_stdout_evidence(stdout, result)
    for field, expected in semantic_output.items():
        if not _same_typed_value(sidecar.get(field), expected):
            raise RunnerError(f"controller stream {field} mismatch")
    _validate_bridge_stderr_output(sidecar, root, require_clean=True)
    freeze_evidence = _validate_current_freeze(root, binding)
    for field, expected in freeze_evidence.items():
        if not _same_typed_value(sidecar.get(field), expected):
            raise RunnerError(f"controller exit {field} mismatch")

    # Rebuild/verify the cohort and any mandatory offline review.  A current
    # clear review is reused byte-for-byte; a current blocking review raises.
    _refresh_cohort(root, binding["decision_hash"])
    return sidecar


def _output_summary(stdout, stderr, exit_code):
    return {
        "exit_code": int(exit_code),
        "stdout_size": len(stdout),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_size": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
    }


def _bridge_stderr_start_snapshot(root):
    launch = _read_object(Path(root) / LAUNCH_NAME, "runtime launch")
    if (
        launch.get("schema_version") != 2
        or not _nonempty_string(launch.get("launch_id"))
        or "finished_at" in launch
        or "exit_code" in launch
    ):
        raise RunnerError("runtime launch is not active schema-v2 evidence")
    game_dir = launch.get("game_dir")
    path_value = launch.get("bridge_stderr_path")
    if (
        not _nonempty_string(game_dir)
        or not _nonempty_string(path_value)
    ):
        raise RunnerError("runtime bridge stderr path is missing")
    path = Path(path_value)
    if (
        not path.is_absolute()
        or path.resolve()
        != (Path(game_dir) / "communication_mod_errors.log").resolve()
    ):
        raise RunnerError("runtime bridge stderr path is not canonical")
    if not path.is_file():
        raise RunnerError("runtime bridge stderr evidence file is missing")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise RunnerError("runtime bridge stderr is unreadable") from exc
    return {
        "launch_id": launch["launch_id"],
        "bridge_stderr_path": str(path.resolve()),
        "bridge_stderr_start_size": size,
        "bridge_stderr_start_sha256": digest.hexdigest(),
    }


def _bridge_stderr_end_snapshot(start, *, end_size=None):
    path = Path(start["bridge_stderr_path"])
    start_size = start["bridge_stderr_start_size"]
    full = hashlib.sha256()
    prefix = hashlib.sha256()
    delta = hashlib.sha256()
    total = 0
    if not path.is_file():
        raise RunnerError("runtime bridge stderr evidence file disappeared")
    try:
        with path.open("rb") as handle:
            while True:
                if end_size is not None:
                    remaining = end_size - total
                    if remaining <= 0:
                        break
                    block = handle.read(min(1024 * 1024, remaining))
                else:
                    block = handle.read(1024 * 1024)
                if not block:
                    break
                block_start = total
                block_end = total + len(block)
                full.update(block)
                if block_start < start_size:
                    prefix.update(block[:max(
                        0, min(len(block), start_size - block_start)
                    )])
                if block_end > start_size:
                    delta.update(block[max(0, start_size - block_start):])
                total = block_end
    except OSError as exc:
        raise RunnerError("runtime bridge stderr is unreadable at exit") from exc
    if end_size is not None and total != end_size:
        raise RunnerError("controller exit bridge stderr evidence was truncated")
    if total < start_size:
        raise RunnerError("runtime bridge stderr was truncated during attempt")
    if prefix.hexdigest() != start["bridge_stderr_start_sha256"]:
        raise RunnerError("runtime bridge stderr prefix changed during attempt")
    return {
        **start,
        "bridge_stderr_evidence_status": (
            "clear" if total == start_size else "issues"
        ),
        "bridge_stderr_error": None,
        "bridge_stderr_end_size": total,
        "bridge_stderr_end_sha256": full.hexdigest(),
        "bridge_stderr_delta_size": total - start_size,
        "bridge_stderr_delta_sha256": delta.hexdigest(),
    }


def _validate_bridge_stderr_output(record, root, *, require_clean):
    fields = (
        "launch_id",
        "bridge_stderr_path",
        "bridge_stderr_start_size",
        "bridge_stderr_start_sha256",
        "bridge_stderr_end_size",
        "bridge_stderr_end_sha256",
        "bridge_stderr_delta_size",
        "bridge_stderr_delta_sha256",
    )
    if any(field not in record for field in fields):
        raise RunnerError("controller exit bridge stderr evidence is missing")
    if record.get("bridge_stderr_evidence_status") not in {
        "clear", "issues"
    }:
        raise RunnerError("controller exit bridge stderr status is invalid")
    start = {
        field: record[field]
        for field in (
            "launch_id",
            "bridge_stderr_path",
            "bridge_stderr_start_size",
            "bridge_stderr_start_sha256",
        )
    }
    if (
        not _nonempty_string(start["launch_id"])
        or not _nonempty_string(start["bridge_stderr_path"])
        or type(start["bridge_stderr_start_size"]) is not int
        or start["bridge_stderr_start_size"] < 0
    ):
        raise RunnerError("controller exit bridge stderr start is invalid")
    for field in (
        "bridge_stderr_start_sha256",
        "bridge_stderr_end_sha256",
        "bridge_stderr_delta_sha256",
    ):
        value = record.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise RunnerError(f"controller exit {field} is invalid")
    observed_end = record["bridge_stderr_end_size"]
    if type(observed_end) is not int or observed_end < start["bridge_stderr_start_size"]:
        raise RunnerError("controller exit bridge stderr end is invalid")
    # At append/reconciliation time no later attempt is allowed to exist for
    # this current root result.  Requiring the exact current end closes the
    # snapshot-to-receipt race; cohort review may later validate the bounded
    # historical prefix after subsequent attempts append to the same log.
    expected = _bridge_stderr_end_snapshot(start)
    if expected["bridge_stderr_end_size"] != observed_end:
        raise RunnerError(
            "controller exit bridge stderr changed after end snapshot"
        )
    for field in fields:
        if not _same_typed_value(record.get(field), expected.get(field)):
            raise RunnerError(f"controller exit {field} mismatch")
    if require_clean and record["bridge_stderr_delta_size"] != 0:
        raise RunnerError("controller exit bridge stderr delta is not empty")
    if require_clean and (
        record.get("bridge_stderr_evidence_status") != "clear"
        or record.get("bridge_stderr_error") is not None
    ):
        raise RunnerError("controller exit bridge stderr evidence is not clear")
    if require_clean:
        launch = _read_object(Path(root) / LAUNCH_NAME, "runtime launch")
        if (
            launch.get("schema_version") != 2
            or launch.get("launch_id") != record.get("launch_id")
            or launch.get("bridge_stderr_path")
            != record.get("bridge_stderr_path")
            or "finished_at" in launch
            or "exit_code" in launch
        ):
            raise RunnerError(
                "controller exit bridge stderr launch binding mismatch"
            )
    return record


def _terminal_stdout_evidence(stdout, result):
    """Require stdout to be exactly one JSON line equal to run-result."""

    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunnerError("controller stdout is not valid UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise RunnerError("controller stdout is not exactly one terminal JSON line")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RunnerError("controller stdout terminal line is not JSON") from exc
    if not isinstance(value, dict):
        raise RunnerError("controller stdout terminal summary differs from run result")
    canonical = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected_canonical = json.dumps(
        result, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if canonical != expected_canonical:
        raise RunnerError("controller stdout terminal summary differs from run result")
    return {
        "stdout_line_count": 1,
        "stdout_semantic_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _build_exit_record(
    binding, output, *, status, audit_summary=None, freeze_evidence=None
):
    record = {
        "record_type": campaign_selector.CONTROLLER_EXIT_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        **binding,
        **output,
        "controller_exit_status": status,
        "observed_at": time.time(),
    }
    if audit_summary is not None:
        record["controller_exit_audit"] = audit_summary
    if freeze_evidence is not None:
        record.update(freeze_evidence)
    return record


def _failure_audit(evidence, output, reason):
    binding = evidence["binding"]
    path = evidence["directory"] / "controller-exit-audit.json"
    report = {
        "record_type": "controller_exit_audit",
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        **binding,
        "audit_status": "issues",
        "release_gate_passed": False,
        "protocol_correctness": {"status": "inconclusive"},
        "mechanics_coverage": {"status": "inconclusive"},
        "strategy_quality": {"status": "inconclusive"},
        "issue_count": 1,
        "review_finding_count": 1,
        "issues": [{
            "kind": "controller_exit_failure",
            "severity": "P1",
            "reason": reason,
            **output,
        }],
        "last_authoritative_state": _state_summary(
            evidence["state"], binding
        ),
        "generated_at": time.time(),
    }
    _write_json_once(path, report)
    return report, {
        "path": "controller-exit-audit.json",
        "audit_status": "issues",
        "release_gate_passed": False,
        "issue_count": 1,
        "review_finding_count": 1,
        "issue_kind": "controller_exit_failure",
        "severity": "P1",
        "reason": reason,
    }


def _blocked_launch_incident(root, reason, *, stdout=b"", stderr=b"", error=None):
    root = Path(root)
    incident_id = f"{int(time.time() * 1000)}-{os.getpid()}-{uuid.uuid4().hex}"
    directory = root / "logs" / "blocked-launches" / incident_id
    directory.mkdir(parents=True, exist_ok=False)
    _write_once(directory / "controller.stdout.log", stdout)
    _write_once(directory / "controller.stderr.log", stderr)
    report = {
        "record_type": "blocked_launch_incident",
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "incident_id": incident_id,
        "binding_status": "unverified",
        "reason": reason,
        "error_class": type(error).__name__ if error is not None else None,
        **_output_summary(stdout, stderr, -1),
        "generated_at": time.time(),
    }
    _write_json_once(directory / "blocked-launch-incident.json", report)
    return report


def fingerprint_sources(root=ROOT):
    root = Path(root)
    names = (
        "autoplay.py",
        "autoplay_runner.py",
        "campaign_attempt.py",
        "campaign_selector.py",
        "cohort_report.py",
        "cohort_review.py",
        "strategy_audit.py",
        "independent_oracle.py",
        "death_replay.py",
        "deepseek_macro.py",
        "macro_policy.py",
        "macro_wire.py",
        "prompt_assembler.py",
        "decision_cases.py",
        "pre_run_binding.py",
        "decision_case_replay.py",
        "decision_case_resolution.py",
        "policy_contracts.py",
        "freeze_manifest.py",
        "launch_game.py",
        "bridge.py",
        "stsctl.py",
        "run_context.py",
        "decision-case-resolutions.json",
        "decision-case-trace-evidence.json",
        "test_fixtures/decision-cases-v2.jsonl",
        "test_fixtures/decision-case-resolution-invariants-v1.json",
        "CommunicationMod.jar",
    )
    directories = (
        (
            "src/CommunicationMod-1.2.1/src/main/java/communicationmod",
            {".java"},
        ),
        ("src/spirecomm-master/spirecomm", {".py"}),
        ("prompts", {".json", ".txt"}),
        ("knowledge", {".json", ".txt"}),
        ("test_fixtures", {".json", ".jsonl", ".txt"}),
    )
    paths = []
    for name in names:
        path = root / name
        if not path.is_file():
            raise RunnerError(f"runner fingerprint source is missing: {name}")
        paths.append(path.resolve())
    for name, suffixes in directories:
        directory = root / name
        if not directory.is_dir():
            raise RunnerError(
                f"runner fingerprint source directory is missing: {name}"
            )
        children = sorted(
            (
                item.resolve() for item in directory.rglob("*")
                if item.is_file()
                and item.suffix.casefold() in suffixes
                and "__pycache__" not in item.parts
            ),
            key=lambda item: item.relative_to(root.resolve()).as_posix(),
        )
        if not children:
            raise RunnerError(
                f"runner fingerprint source directory is empty: {name}"
            )
        paths.extend(children)
    return tuple(sorted(
        set(paths),
        key=lambda item: item.relative_to(root.resolve()).as_posix(),
    ))


def runner_fingerprint(root=ROOT):
    """Hash the supervisor and every post-exit gate it relies on."""

    digest = hashlib.sha256()
    for path in fingerprint_sources(root):
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError as exc:
            raise RunnerError("runner fingerprint source escaped root") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_controller(
    root=ROOT,
    *,
    max_actions=5000,
    python_executable=sys.executable,
    subprocess_run=subprocess.run,
):
    """Run one child, persist raw streams, and append its outer receipt."""

    root = Path(root)
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    result_path = root / RESULT_NAME
    with RunnerLease(root / RUNNER_LOCK_NAME):
        reconciled = _reconcile_completed_receipt(root)
        if reconciled is not None:
            return reconciled
        try:
            pending_selection = _read_object(
                root / SELECTION_NAME, "pending accepted selection"
            )
            pre_run_binding.load_start_acceptance(
                root, pending_selection
            )
        except (RunnerError, pre_run_binding.PreRunBindingError) as exc:
            _blocked_launch_incident(
                root, "start_acceptance_invalid", error=exc
            )
            if isinstance(exc, RunnerError):
                raise
            raise RunnerError(str(exc)) from exc
        bridge_start = None
        bridge_stderr_error = None
        try:
            bridge_start = _bridge_stderr_start_snapshot(root)
        except RunnerError as exc:
            # START has already committed before this supervisor is called.
            # Keep controlling the run, but make a clean outer receipt
            # impossible and retain the provenance failure in its P1 audit.
            bridge_stderr_error = str(exc)
        before_result = _read_optional_bytes(result_path)
        command = [
            str(python_executable),
            str(root / "autoplay.py"),
            "--max-actions",
            str(max_actions),
        ]
        try:
            # Final consumer-side check: no mutable history, source,
            # selection, acceptance, launch, or bridge evidence may change in
            # the START-acceptance-to-child window.
            pre_run_binding.validate_accepted_start_checkpoint(
                root, expected_selection=pending_selection
            )
        except pre_run_binding.PreRunBindingError as exc:
            _blocked_launch_incident(
                root, "accepted_start_checkpoint_invalid", error=exc
            )
            raise RunnerError(str(exc)) from exc
        try:
            completed = subprocess_run(
                command,
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except Exception as exc:
            _blocked_launch_incident(
                root, "controller_subprocess_exception", error=exc
            )
            raise RunnerError("autoplay subprocess could not be observed") from exc

        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        output = _output_summary(stdout, stderr, completed.returncode)
        bridge_output = {}
        if bridge_start is not None:
            try:
                bridge_output = _bridge_stderr_end_snapshot(bridge_start)
            except RunnerError as exc:
                bridge_stderr_error = str(exc)
        if bridge_stderr_error is not None:
            bridge_output = {
                **(bridge_start or {}),
                **bridge_output,
                "bridge_stderr_evidence_status": "issues",
                "bridge_stderr_error": bridge_stderr_error,
            }
        output.update(bridge_output)
        after_result = _read_optional_bytes(result_path)
        if after_result is None or after_result == before_result:
            _blocked_launch_incident(
                root,
                "controller_did_not_land_a_new_run_result",
                stdout=stdout,
                stderr=stderr,
            )
            raise RunnerError("controller did not land a new run result")
        try:
            result = _read_object(result_path, "authoritative run result")
            binding = _binding_from_result(result)
        except RunnerError as exc:
            _blocked_launch_incident(
                root,
                "new_run_result_is_not_uniquely_bindable",
                stdout=stdout,
                stderr=stderr,
                error=exc,
            )
            raise

        directory = attempt_directory(root, binding["attempt_id"])
        try:
            _write_once(directory / "controller.stdout.log", stdout)
            _write_once(directory / "controller.stderr.log", stderr)
        except RunnerError as exc:
            _blocked_launch_incident(
                root,
                "attempt_output_evidence_already_exists",
                stdout=stdout,
                stderr=stderr,
                error=exc,
            )
            raise

        process_clean = completed.returncode == 0 and not stderr
        bridge_stderr_clean = (
            bridge_output.get("bridge_stderr_evidence_status") == "clear"
            and bridge_output.get("bridge_stderr_error") is None
            and bridge_output.get("bridge_stderr_delta_size") == 0
        )
        evidence = None
        evidence_error = None
        freeze_evidence = None
        try:
            evidence = _load_evidence(
                root, result, require_terminal_snapshot=process_clean
            )
            terminal_error = campaign_selector._terminal_validation_error(
                {
                    **result,
                    "record_type": campaign_selector.TERMINAL_RECORD_TYPE,
                },
                binding["decision_hash"],
            )
            if terminal_error is not None:
                raise RunnerError(f"terminal evidence failed: {terminal_error}")
            audit_error = campaign_selector._audit_validation_error(
                {
                    **evidence["audit"],
                    "record_type": campaign_selector.AUDIT_RECORD_TYPE,
                },
                result,
            )
            if audit_error is not None:
                raise RunnerError(f"audit evidence failed: {audit_error}")
            if process_clean:
                output.update(_terminal_stdout_evidence(stdout, result))
                freeze_evidence = _validate_current_freeze(root, binding)
                if (
                    bridge_output.get("launch_id")
                    != freeze_evidence.get("freeze_launch_id")
                ):
                    raise RunnerError(
                        "attempt bridge stderr launch differs from freeze"
                    )
        except RunnerError as exc:
            evidence_error = exc

        if process_clean and bridge_stderr_clean and evidence_error is None:
            exit_record = _build_exit_record(
                binding,
                output,
                status="clear",
                freeze_evidence=freeze_evidence,
            )
            append_controller_exit(root, exit_record, require_audit=True)
            _refresh_cohort(root, binding["decision_hash"])
            return exit_record

        failure_reason = (
            "controller_process_exit_not_clean"
            if not process_clean
            else (
                "communication_mod_error_delta_nonempty"
                if bridge_output.get("bridge_stderr_delta_size", 0) > 0
                else "communication_mod_error_evidence_invalid:"
                + str(bridge_stderr_error)
            )
            if not bridge_stderr_clean
            else f"controller_exit_evidence_invalid:{evidence_error}"
        )
        if evidence is None:
            try:
                evidence = _load_failure_binding(root, result)
            except RunnerError as exc:
                _blocked_launch_incident(
                    root,
                    "controller_failure_could_not_be_uniquely_bound",
                    stdout=stdout,
                    stderr=stderr,
                    error=exc,
                )
                raise RunnerError(
                    "controller failure could not be uniquely bound"
                ) from exc
        # A clean controller process with a non-clear audit is still a
        # structurally bindable cohort attempt.  Preserve the exact terminal
        # stdout and frozen-launch evidence on the issues receipt so the
        # selector can downgrade the ordinary audit finding without treating
        # missing receipt fields as a second (false) P0.
        if process_clean or (
            completed.returncode == 1
            and not stderr
            and evidence is not None
        ):
            try:
                output.update(_terminal_stdout_evidence(stdout, result))
                freeze_evidence = _validate_current_freeze(root, binding)
            except RunnerError as exc:
                evidence_error = evidence_error or exc
        _, audit_summary = _failure_audit(
            evidence, output, failure_reason
        )
        exit_record = _build_exit_record(
            binding,
            output,
            status="issues",
            audit_summary=audit_summary,
            freeze_evidence=freeze_evidence,
        )
        append_controller_exit(root, exit_record, require_audit=False)
        _ensure_attempt_blocking_review(root, result, exit_record)
        _refresh_cohort(root, binding["decision_hash"])
        return exit_record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-actions", type=int, default=5000)
    args = parser.parse_args()
    try:
        receipt = run_controller(max_actions=args.max_actions)
    except (RunnerError, campaign_selector.HistoryValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt.get("controller_exit_status") == "clear" else 1


if __name__ == "__main__":
    raise SystemExit(main())
