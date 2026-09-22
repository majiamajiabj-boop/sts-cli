"""State-versioned safety bridge for CommunicationMod.

CommunicationMod itself speaks newline-delimited raw commands.  Controllers
write one structured command to command.json.  This bridge binds that command
to the exact state/decision/target visible in state.json, rejects stale or
mismatched requests locally, and only then emits the raw command to the mod.
"""

import hashlib
import json
import os
import math
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

import freeze_manifest
import pre_run_binding


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
META_PATH = ROOT / "state-meta.json"
COMMAND_PATH = ROOT / "command.json"
RECEIPT_PATH = ROOT / "action-receipt.json"
INSTANCE_PATH = ROOT / "bridge-instance.json"
RUN_CONTEXT_PATH = ROOT / "run-context.json"
PENDING_SELECTION_PATH = ROOT / "next-run-selection.json"
RUN_HISTORY_PATH = ROOT / "run-history.jsonl"
FREEZE_MANIFEST_PATH = ROOT / "freeze-manifest.json"
COHORT_REVIEW_PATH = ROOT / "cohort-review.json"
PROTOCOL_VERSION = 2
REQUIRED_POLICY_VERSION = "fast-policy-v5"
STATE_COMMAND_WAKE_INTERVAL_SECONDS = 0.03
ATTEMPT_BINDING_FIELDS = (
    "attempt_id",
    "run_id",
    "seed",
    "character",
    "ascension_level",
    "run_type",
    "decision_hash",
    "controller_hash",
    "policy_version",
    "selection_id",
    "selection_digest",
)
EVENT_SETTLE_MIN_FRAMES = 2
EVENT_SETTLE_TIMEOUT_FRAMES = 240
EVENT_UNCHANGED_SETTLE_FRAMES = 30
# Gremlin Wheel keeps exposing its singleton ``spin`` option while the wheel
# animation/result is being resolved.  A short unchanged window therefore
# acknowledges the click too early and lets the controller submit Spin again.
# Keep the receipt pending for the full event settlement window instead.
WHEEL_SETTLE_TIMEOUT_FRAMES = 720
EVENT_REWARD_FALLBACK_FRAMES = 120
MATCH_UNKNOWN_PAIR_SETTLE_FRAMES = 120
# A card play can advance the serialized decision surface before the game's
# card/action queue has fully drained.  In that narrow window a following
# PLAY is accepted by CommunicationMod but never enters the queue (notably
# after a Power triggers Mummified Hand).  Keep one read-only frame between a
# normal same-turn play acknowledgement and the next write; this is a
# transport barrier, not a replay or a relaxed no-op acknowledgement.
COMBAT_PLAY_SETTLE_FRAMES = 1
# Bound waiting for the exact selected instance. Once selection is observed,
# its receipt completes; readiness for subsequent gameplay is gated separately.
HAND_SELECT_CHOOSE_SETTLE_TIMEOUT_FRAMES = 120
# A HAND_SELECT frame can expose the post-selection hand before the Java
# HandCardSelectScreen has committed the same list for the next command.  Do
# not dispatch the following CHOOSE from the first frame we observe: require
# two identical, command-ready frames while the exact card instance remains
# visible.  This is a transport barrier; it never changes the selected card.
HAND_SELECT_PRE_DISPATCH_STABLE_FRAMES = 2
HAND_SELECT_PRE_DISPATCH_TIMEOUT_FRAMES = 120
# Confirming a multi-card HAND_SELECT can leave the screen visible for a few
# frames while the selected cards are exhausted/discarded.  The confirm has
# already been accepted in that window, so keep its receipt pending long
# enough to observe the subsequent combat frame instead of classifying the
# valid action as a no-op.
HAND_SELECT_PROCEED_SETTLE_TIMEOUT_FRAMES = 120
# Campfire options can spend several render frames in their VFX/action queue
# (notably RECALL) before the room leaves REST.  Keep the accepted command
# pending until the room phase or room type actually advances.
REST_SETTLE_TIMEOUT_FRAMES = 120
# The Heart victory screen can keep the dungeon process alive while the base
# game runs its victory/unlock transition.  Death screens normally settle in
# the shorter budget, but a legitimate victory needs extra time before
# ``startOver`` exposes the main menu to CommunicationMod.
PROCEED_SETTLE_TIMEOUT_FRAMES = 120
PROCEED_VICTORY_SETTLE_TIMEOUT_FRAMES = 360
PROGRESSION_WIN_FLAGS = (
    "silent_third_act_win",
    "defect_third_act_win",
    "ironclad_third_act_win",
)
BRIDGE_INSTANCE_FIELDS = {
    "schema_version", "protocol_version", "instance_token",
    "bridge_pid", "parent_java_pid", "launch_id", "bridge_sha256",
    "runtime_source_digest", "runtime_source_file_count", "started_at",
}
PRE_RUNTIME_SOURCE_BRIDGE_INSTANCE_FIELDS = (
    BRIDGE_INSTANCE_FIELDS
    - {"runtime_source_digest", "runtime_source_file_count"}
)
LEGACY_SCHEMA2_BRIDGE_INSTANCE_FIELDS = (
    PRE_RUNTIME_SOURCE_BRIDGE_INSTANCE_FIELDS - {"launch_id"}
)
BRIDGE_CLAIM_LOCK_STALE_SECONDS = 30.0
BRIDGE_CLAIM_OWNER_FILE = "owner.json"
BRIDGE_CLAIM_RECOVERY_FILE = "recovery.json"
BRIDGE_CLAIM_OWNER_FIELDS = {
    "schema_version", "owner_token", "owner_pid", "created_at", "launch_id",
}
NEOW_REWARD_KINDS = {
    "RANDOM_COLORLESS_2", "THREE_CARDS", "ONE_RANDOM_RARE_CARD",
    "REMOVE_CARD", "UPGRADE_CARD", "RANDOM_COLORLESS", "TRANSFORM_CARD",
    "THREE_SMALL_POTIONS", "RANDOM_COMMON_RELIC", "TEN_PERCENT_HP_BONUS",
    "HUNDRED_GOLD", "THREE_ENEMY_KILL", "REMOVE_TWO",
    "TRANSFORM_TWO_CARDS", "ONE_RARE_RELIC", "THREE_RARE_CARDS",
    "TWO_FIFTY_GOLD", "TWENTY_PERCENT_HP_BONUS", "BOSS_RELIC",
}
NEOW_DRAWBACK_KINDS = {
    "NONE", "TEN_PERCENT_HP_LOSS", "NO_GOLD", "CURSE", "PERCENT_DAMAGE",
}
WORLD_OF_GOOP_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.exordium.GoopPuddle"
)
CLERIC_EVENT_CLASS = "com.megacrit.cardcrawl.events.exordium.Cleric"
DESIGNER_EVENT_CLASS = "com.megacrit.cardcrawl.events.shrines.Designer"
CURSED_TOME_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.city.CursedTome"
)
MAUSOLEUM_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.city.TheMausoleum"
)
KNOWING_SKULL_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.city.KnowingSkull"
)
DEAD_ADVENTURER_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.exordium.DeadAdventurer"
)
SCRAP_OOZE_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.exordium.ScrapOoze"
)
FACE_TRADER_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.shrines.FaceTrader"
)
DUPLICATOR_EVENT_CLASS = (
    "com.megacrit.cardcrawl.events.shrines.Duplicator"
)
BONFIRE_EVENT_CLASS = "com.megacrit.cardcrawl.events.shrines.Bonfire"
FACE_TRADER_RELIC_IDS = (
    "CultistMask", "FaceOfCleric", "GremlinMask", "NlothsMask",
    "SsserpentHead",
)
CURSED_TOME_BOOK_IDS = (
    "Necronomicon", "Enchiridion", "Nilry's Codex",
)


class CommandRejected(ValueError):
    pass


def atomic_write(path, content):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    for attempt in range(3000):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError:
            if attempt == 2999:
                raise
            time.sleep(0.02)


def atomic_write_json(path, payload):
    atomic_write(path, json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def _state_command_wakeup(
    command_path, *, pending_ref, stop_event, output_lock,
    interval=STATE_COMMAND_WAKE_INTERVAL_SECONDS,
):
    """Wake the game when a timed-out action leaves a read-only STATE pending.

    The bridge normally polls ``command.json`` between incoming game frames.
    If an accepted gameplay command stops producing frames, however, the
    controller's recovery STATE request would never reach CommunicationMod.
    This narrow watcher only emits STATE, never consumes the file, and leaves
    normal command validation/receipt ownership to the main bridge loop.
    """

    emitted_request_id = None
    while not stop_event.wait(interval):
        if not pending_ref.get("active"):
            continue
        try:
            payload = json.loads(command_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("action") or "").lower() != "state":
            continue
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            continue
        if request_id == emitted_request_id:
            continue
        with output_lock:
            print("STATE", flush=True)
        emitted_request_id = request_id


def _emit_command(command, output_lock):
    with output_lock:
        print(command, flush=True)


def _sha256_file(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise RuntimeError(f"bridge source is unreadable: {path}") from exc
    return digest.hexdigest()


def _read_existing_bridge_instance():
    try:
        value = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing bridge instance is unreadable") from exc
    if not isinstance(value, dict):
        raise RuntimeError("existing bridge instance is malformed")
    return value


def _existing_bridge_pids(record):
    if (
        set(record) == BRIDGE_INSTANCE_FIELDS
        and record.get("schema_version") == 2
        and record.get("protocol_version") == PROTOCOL_VERSION
    ):
        bridge_pid = record.get("bridge_pid")
        parent_pid = record.get("parent_java_pid")
    elif (
        set(record) == PRE_RUNTIME_SOURCE_BRIDGE_INSTANCE_FIELDS
        and record.get("schema_version") == 2
        and record.get("protocol_version") == PROTOCOL_VERSION
    ):
        bridge_pid = record.get("bridge_pid")
        parent_pid = record.get("parent_java_pid")
    elif (
        set(record) == LEGACY_SCHEMA2_BRIDGE_INSTANCE_FIELDS
        and record.get("schema_version") == 2
        and record.get("protocol_version") == PROTOCOL_VERSION
    ):
        bridge_pid = record.get("bridge_pid")
        parent_pid = record.get("parent_java_pid")
    elif set(record) == {"token", "pid", "parent_pid", "claimed_at"}:
        # One-time safe migration from the pre-schema2 bridge lease.  A live
        # legacy owner is still an owner and must never be overwritten.
        bridge_pid = record.get("pid")
        parent_pid = record.get("parent_pid")
    else:
        raise RuntimeError("existing bridge instance is malformed")
    if (
        type(bridge_pid) is not int or bridge_pid <= 0
        or type(parent_pid) is not int or parent_pid <= 0
    ):
        raise RuntimeError("existing bridge instance pids are invalid")
    return bridge_pid, parent_pid


def _claim_owner_payload(*, token, pid, created_at, launch_id):
    return {
        "schema_version": 1,
        "owner_token": token,
        "owner_pid": pid,
        "created_at": float(created_at),
        "launch_id": launch_id,
    }


def _validate_claim_owner(value, *, label):
    if not isinstance(value, dict) or set(value) != BRIDGE_CLAIM_OWNER_FIELDS:
        raise RuntimeError(f"bridge claim {label} is malformed")
    if value.get("schema_version") != 1:
        raise RuntimeError(f"bridge claim {label} is malformed")
    token = value.get("owner_token")
    pid = value.get("owner_pid")
    created_at = value.get("created_at")
    launch_id = value.get("launch_id")
    if (
        not isinstance(token, str) or not token or len(token) > 512
        or type(pid) is not int or pid <= 0
        or type(created_at) not in (int, float)
        or not math.isfinite(float(created_at)) or float(created_at) < 0
        or not isinstance(launch_id, str) or not launch_id
        or any(character in launch_id for character in "\\/:")
    ):
        raise RuntimeError(f"bridge claim {label} is malformed")
    return value


def _read_claim_owner(path, *, label):
    try:
        owner_stat = os.lstat(path)
        if not stat.S_ISREG(owner_stat.st_mode) or stat.S_ISLNK(owner_stat.st_mode):
            raise RuntimeError(f"bridge claim {label} is malformed")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"bridge claim {label} is unreadable") from exc
    return _validate_claim_owner(value, label=label), raw


def _write_exclusive_claim_owner(path, payload):
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return serialized


def _claim_process_alive(process_alive_fn, pid, *, label):
    try:
        return bool(process_alive_fn(pid))
    except Exception as exc:
        raise RuntimeError(
            f"bridge claim {label} process state cannot be verified"
        ) from exc


def _unlink_claim_owner_if_exact(path, expected_raw, *, label):
    try:
        current_raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(f"bridge claim {label} is unreadable") from exc
    if current_raw != expected_raw:
        raise RuntimeError(f"bridge claim {label} changed during recovery")
    path.unlink()
    return True


def _bridge_instance_has_live_owner(process_alive_fn):
    existing = _read_existing_bridge_instance()
    if existing is None:
        return False
    bridge_pid, _parent_pid = _existing_bridge_pids(existing)
    return _claim_process_alive(
        process_alive_fn, bridge_pid, label="instance owner"
    )


def _recover_bridge_claim_lock(
    claim_lock, *, process_alive_fn, current_pid, clock, token_factory,
    launch_id, stale_seconds,
):
    """Recover only a lock whose prior owner is provably gone.

    A fixed-name recovery record is the compare-and-swap guard.  Every cleanup
    re-reads its exact serialized token, so a failed recovery cannot remove a
    later claimant's record.
    """

    try:
        lock_stat = os.lstat(claim_lock)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RuntimeError("bridge claim lock cannot be inspected") from exc
    if not stat.S_ISDIR(lock_stat.st_mode) or stat.S_ISLNK(lock_stat.st_mode):
        raise RuntimeError("bridge claim lock is malformed")

    try:
        children = {child.name: child for child in claim_lock.iterdir()}
    except OSError as exc:
        raise RuntimeError("bridge claim lock cannot be inspected") from exc
    allowed = {BRIDGE_CLAIM_OWNER_FILE, BRIDGE_CLAIM_RECOVERY_FILE}
    if not set(children).issubset(allowed):
        raise RuntimeError("bridge claim lock contains unexpected content")

    recovery_path = claim_lock / BRIDGE_CLAIM_RECOVERY_FILE
    if BRIDGE_CLAIM_RECOVERY_FILE in children:
        recovery, recovery_raw = _read_claim_owner(
            recovery_path, label="recovery owner"
        )
        if _claim_process_alive(
            process_alive_fn, recovery["owner_pid"], label="recovery owner"
        ):
            raise RuntimeError("another bridge claim recovery is in progress")
        _unlink_claim_owner_if_exact(
            recovery_path, recovery_raw, label="recovery owner"
        )
        # Deleting an abandoned recovery record changes directory timestamps.
        # Re-inspect from scratch; an ownerless directory must age again.
        return False

    owner_path = claim_lock / BRIDGE_CLAIM_OWNER_FILE
    owner = owner_raw = None
    if BRIDGE_CLAIM_OWNER_FILE in children:
        owner, owner_raw = _read_claim_owner(owner_path, label="owner")
        if _claim_process_alive(
            process_alive_fn, owner["owner_pid"], label="owner"
        ):
            raise RuntimeError("another bridge claim is in progress")
    else:
        age = float(clock()) - float(lock_stat.st_mtime)
        if not math.isfinite(age) or age < float(stale_seconds):
            raise RuntimeError("another bridge claim is in progress")
        if _bridge_instance_has_live_owner(process_alive_fn):
            raise RuntimeError(
                "ownerless bridge claim lock has a live bridge instance"
            )

    recovery_token = f"{current_pid}:{token_factory()}"
    recovery = _claim_owner_payload(
        token=recovery_token,
        pid=current_pid,
        created_at=clock(),
        launch_id=launch_id,
    )
    try:
        recovery_raw = _write_exclusive_claim_owner(recovery_path, recovery)
    except FileExistsError as exc:
        raise RuntimeError("another bridge claim recovery is in progress") from exc

    try:
        names = {child.name for child in claim_lock.iterdir()}
        expected_names = {BRIDGE_CLAIM_RECOVERY_FILE}
        if owner is not None:
            expected_names.add(BRIDGE_CLAIM_OWNER_FILE)
        if names != expected_names:
            raise RuntimeError("bridge claim lock changed during recovery")
        if owner is not None:
            current_owner, current_owner_raw = _read_claim_owner(
                owner_path, label="owner"
            )
            if current_owner != owner or current_owner_raw != owner_raw:
                raise RuntimeError("bridge claim owner changed during recovery")
            if _claim_process_alive(
                process_alive_fn, owner["owner_pid"], label="owner"
            ):
                raise RuntimeError("bridge claim owner revived during recovery")
        elif _bridge_instance_has_live_owner(process_alive_fn):
            raise RuntimeError(
                "ownerless bridge claim lock has a live bridge instance"
            )

        if owner is not None:
            _unlink_claim_owner_if_exact(owner_path, owner_raw, label="owner")
        _unlink_claim_owner_if_exact(
            recovery_path, recovery_raw, label="recovery owner"
        )
        try:
            claim_lock.rmdir()
        except OSError as exc:
            raise RuntimeError("bridge claim lock changed during recovery") from exc
        return True
    except Exception:
        # Release only our own guard.  A concurrently written owner is retained.
        try:
            _unlink_claim_owner_if_exact(
                recovery_path, recovery_raw, label="recovery owner"
            )
        except (FileNotFoundError, RuntimeError, OSError):
            pass
        raise


def _acquire_bridge_claim_lock(
    claim_lock, *, process_alive_fn, current_pid, clock, token_factory,
    launch_id, stale_seconds,
):
    for _attempt in range(4):
        try:
            claim_lock.mkdir()
        except FileExistsError:
            recovered = _recover_bridge_claim_lock(
                claim_lock,
                process_alive_fn=process_alive_fn,
                current_pid=current_pid,
                clock=clock,
                token_factory=token_factory,
                launch_id=launch_id,
                stale_seconds=stale_seconds,
            )
            if not recovered:
                continue
            continue

        owner_token = f"{current_pid}:{token_factory()}"
        owner = _claim_owner_payload(
            token=owner_token,
            pid=current_pid,
            created_at=clock(),
            launch_id=launch_id,
        )
        owner_path = claim_lock / BRIDGE_CLAIM_OWNER_FILE
        try:
            owner_raw = _write_exclusive_claim_owner(owner_path, owner)
        except Exception:
            try:
                claim_lock.rmdir()
            except OSError:
                pass
            raise
        return owner, owner_raw
    raise RuntimeError("bridge claim lock recovery did not converge")


def _release_bridge_claim_lock(claim_lock, owner, owner_raw):
    owner_path = claim_lock / BRIDGE_CLAIM_OWNER_FILE
    try:
        current, current_raw = _read_claim_owner(owner_path, label="owner")
    except FileNotFoundError:
        return
    if current != owner or current_raw != owner_raw:
        return
    try:
        _unlink_claim_owner_if_exact(owner_path, owner_raw, label="owner")
        claim_lock.rmdir()
    except (FileNotFoundError, OSError, RuntimeError):
        pass


def claim_bridge_instance(
    *, process_alive_fn=None,
    current_pid_fn=os.getpid, parent_pid_fn=os.getppid,
    clock=time.time, token_factory=uuid.uuid4, launch_id=None,
    source_snapshot_fn=None,
    claim_lock_stale_seconds=BRIDGE_CLAIM_LOCK_STALE_SECONDS,
):
    """Make this process the sole command-file owner for the current game."""

    # ``parent_process_is_alive`` is defined just below this function, so bind
    # the production default at call time rather than in the signature.
    process_alive_fn = process_alive_fn or parent_process_is_alive
    launch_id = str(launch_id or os.environ.get("STS_LAUNCH_ID") or "")
    if (
        not launch_id
        or any(character in launch_id for character in "\\/:")
    ):
        raise RuntimeError("bridge launch_id environment binding is missing")
    claim_lock = INSTANCE_PATH.with_name(
        INSTANCE_PATH.name + ".claim-lock"
    )
    bridge_pid = current_pid_fn()
    if type(bridge_pid) is not int or bridge_pid <= 0:
        raise RuntimeError("new bridge process identity is invalid")
    lock_owner, lock_owner_raw = _acquire_bridge_claim_lock(
        claim_lock,
        process_alive_fn=process_alive_fn,
        current_pid=bridge_pid,
        clock=clock,
        token_factory=token_factory,
        launch_id=launch_id,
        stale_seconds=claim_lock_stale_seconds,
    )
    try:
        existing = _read_existing_bridge_instance()
        if existing is not None:
            existing_bridge_pid, existing_parent_pid = _existing_bridge_pids(
                existing
            )
            try:
                bridge_alive = bool(process_alive_fn(existing_bridge_pid))
                parent_alive = bool(process_alive_fn(existing_parent_pid))
            except Exception as exc:
                raise RuntimeError(
                    "existing bridge process state cannot be verified"
                ) from exc
            if bridge_alive:
                qualifier = " and parent is alive" if parent_alive else ""
                raise RuntimeError(
                    "existing bridge instance is still alive" + qualifier
                )
        parent_pid = parent_pid_fn()
        if (
            type(bridge_pid) is not int or bridge_pid <= 0
            or type(parent_pid) is not int or parent_pid <= 0
            or bridge_pid == parent_pid
        ):
            raise RuntimeError("new bridge process identity is invalid")
        snapshot_fn = source_snapshot_fn or freeze_manifest.source_snapshot
        try:
            runtime_sources = snapshot_fn(ROOT)
        except Exception as exc:
            raise RuntimeError(
                "bridge runtime source snapshot cannot be captured"
            ) from exc
        if not isinstance(runtime_sources, dict) or not runtime_sources:
            raise RuntimeError("bridge runtime source snapshot is empty")
        runtime_source_digest = freeze_manifest.snapshot_digest(
            runtime_sources
        )
        token = lock_owner["owner_token"]
        atomic_write_json(
            INSTANCE_PATH,
            {
                "schema_version": 2,
                "protocol_version": PROTOCOL_VERSION,
                "instance_token": token,
                "bridge_pid": bridge_pid,
                "parent_java_pid": parent_pid,
                "launch_id": launch_id,
                "bridge_sha256": _sha256_file(Path(__file__).resolve()),
                "runtime_source_digest": runtime_source_digest,
                "runtime_source_file_count": len(runtime_sources),
                "started_at": float(clock()),
            },
        )
        return token
    finally:
        _release_bridge_claim_lock(claim_lock, lock_owner, lock_owner_raw)


def bridge_instance_is_current(token):
    try:
        record = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        return (
            isinstance(record, dict)
            and set(record) == BRIDGE_INSTANCE_FIELDS
            and record.get("schema_version") == 2
            and record.get("protocol_version") == PROTOCOL_VERSION
            and record.get("instance_token") == token
            and record.get("bridge_pid") == os.getpid()
            and record.get("parent_java_pid") == os.getppid()
            and isinstance(record.get("launch_id"), str)
            and bool(record["launch_id"])
            and isinstance(record.get("runtime_source_digest"), str)
            and len(record["runtime_source_digest"]) == 64
            and type(record.get("runtime_source_file_count")) is int
            and record["runtime_source_file_count"] > 0
        )
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, AttributeError):
        return False


def release_bridge_instance(token):
    if not bridge_instance_is_current(token):
        return
    try:
        INSTANCE_PATH.unlink()
    except FileNotFoundError:
        pass


def parent_process_is_alive(parent_pid):
    """Check the game process without signalling or otherwise mutating it."""

    if type(parent_pid) is not int or parent_pid <= 0:
        return False
    if os.name == "nt":
        import _winapi

        try:
            handle = _winapi.OpenProcess(
                _winapi.SYNCHRONIZE, False, parent_pid
            )
        except OSError as exc:
            return getattr(exc, "winerror", None) == 5
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(parent_pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def stable_id(prefix, value):
    digest = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def active_attempt_binding(state, context_path=None):
    """Return a frozen controller binding only for this exact active run."""

    context_path = RUN_CONTEXT_PATH if context_path is None else context_path
    if state.get("in_game") is not True:
        return None
    game = state.get("game_state") or {}
    try:
        context = json.loads(Path(context_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(context, dict):
        return None
    seed = game.get("seed")
    character = game.get("class")
    ascension = game.get("ascension_level")
    run_id = f"{character}:{ascension}:{seed}"
    selection = context.get("selection")
    screen_type = str(game.get("screen_type") or "").upper()
    terminal_state_seq = context.get("terminal_state_seq")
    required_strings = (
        "attempt_id", "run_id", "character", "decision_hash",
        "controller_hash", "policy_version", "selection_id",
        "selection_digest",
    )
    if any(
        not isinstance(context.get(field), str)
        or not context.get(field).strip()
        for field in required_strings
    ):
        return None
    if (
        context.get("schema_version") != 2
        or context.get("policy_version") != REQUIRED_POLICY_VERSION
        or context.get("run_id") != run_id
        or type(context.get("seed")) is not type(seed)
        or context.get("seed") != seed
        or context.get("character") != character
        or type(context.get("ascension_level")) is not int
        or context.get("ascension_level") != ascension
        or ascension != 0
        or context.get("run_type") != "standard"
        or game.get("is_standard_run") is not True
        or context.get("goal_mode") != "HEART"
        or not isinstance(selection, dict)
        or selection.get("selection_id") != context.get("selection_id")
        or selection.get("decision_hash") != context.get("decision_hash")
        or selection.get("controller_hash") != context.get("controller_hash")
        or selection.get("policy_version") != context.get("policy_version")
        or selection.get("character") != context.get("character")
        or type(selection.get("ascension_level")) is not int
        or selection.get("ascension_level") != context.get("ascension_level")
        or selection.get("run_type") != context.get("run_type")
        or selection.get("goal_mode") != "HEART"
        or selection.get("algorithm") != "beta-thompson-v1"
        or context.get("selection_digest")
        != pre_run_binding.selection_digest(selection)
        or (
            screen_type != "GAME_OVER"
            and terminal_state_seq is not None
        )
        or (
            screen_type == "GAME_OVER"
            and terminal_state_seq is not None
            and (
                type(terminal_state_seq) is not int
                or terminal_state_seq != state.get("state_seq")
            )
        )
    ):
        return None
    binding = {
        "attempt_id": context["attempt_id"],
        "run_id": context["run_id"],
        "seed": context["seed"],
        "character": context["character"],
        "ascension_level": context["ascension_level"],
        "run_type": context["run_type"],
        "decision_hash": context["decision_hash"],
        "controller_hash": context["controller_hash"],
        "policy_version": context["policy_version"],
        "selection_id": context["selection_id"],
        "selection_digest": context["selection_digest"],
    }
    if screen_type == "GAME_OVER":
        binding["terminal_state_seq"] = state.get("state_seq")
    return binding


def derive_phase(raw):
    if "error" in raw:
        return "ERROR"
    if not raw.get("in_game"):
        return "MAIN_MENU"
    game = raw.get("game_state") or {}
    screen = str(game.get("screen_type") or "NONE").upper()
    screen_state = game.get("screen_state") or {}
    if (
        screen == "EVENT"
        and normalized_game_id(screen_state.get("event_id"))
        in {"neow", "neowevent"}
    ):
        return "NEOW"
    if screen == "COMBAT_REWARD" and any(
        isinstance(reward, dict)
        and str(reward.get("reward_type") or "").upper()
        == "SAPPHIRE_KEY"
        for reward in (screen_state.get("rewards") or [])
    ):
        return "SAPPHIRE_KEY"
    if screen != "NONE":
        return screen
    if game.get("is_screen_up"):
        screen_name = str(game.get("screen_name") or "UNKNOWN").upper()
        return f"OVERLAY_{screen_name}"
    if game.get("room_phase") == "COMBAT":
        turn = ((game.get("combat_state") or {}).get("turn"))
        return f"COMBAT_TURN_{turn}"
    return str(game.get("room_phase") or game.get("action_phase") or "IN_GAME").upper()


def is_match_game_event(event_id):
    token = "".join(character for character in str(event_id or "").lower() if character.isalnum())
    return token in {"matchandkeep", "gremlinmatchgame", "matchgame"} or (
        "match" in token and ("keep" in token or "gremlin" in token)
    )


def normalized_game_id(value):
    return "".join(
        character
        for character in str(value or "").lower()
        if character.isalnum()
    )


def has_uninitialized_combat_intent(raw):
    """Whether a nominally ready combat frame is still missing real intents.

    Slay the Spire briefly exposes ``DEBUG`` intents while a combat is being
    created or restored from an autosave. Treating that frame as a decision
    point makes every controller believe the enemies deal zero damage.
    """

    if not raw.get("in_game"):
        return False
    game = raw.get("game_state") or {}
    # A death/victory screen can retain the last combat monster snapshot
    # while the mod is still settling the final damage action. GAME_OVER is
    # already an authoritative terminal state, so never classify that frame
    # as a combat-initialization stall.
    if str(game.get("screen_type") or "").upper() == "GAME_OVER":
        return False
    if game.get("room_phase") != "COMBAT":
        return False
    combat = game.get("combat_state") or {}
    return any(
        str(monster.get("intent") or "").upper() == "DEBUG"
        for monster in combat.get("monsters") or []
        if int(monster.get("current_hp") or 0) > 0
        and not monster.get("half_dead")
        and not monster.get("is_gone")
    )


def annotate_cards(value):
    if isinstance(value, list):
        for item in value:
            annotate_cards(item)
    elif isinstance(value, dict):
        if "uuid" in value and "id" in value:
            value["card_instance_id"] = str(value["uuid"])
        for child in value.values():
            annotate_cards(child)


def annotate_instances(raw):
    annotate_cards(raw)
    game = raw.get("game_state") or {}
    for slot, potion in enumerate(game.get("potions") or []):
        potion["potion_instance_id"] = stable_id(
            "potion", {"slot": slot, "id": potion.get("id"), "name": potion.get("name")}
        )
        potion["slot"] = slot
    combat = game.get("combat_state") or {}
    occurrences = {}
    for monster in combat.get("monsters") or []:
        key = str(monster.get("id") or monster.get("name") or "monster")
        occurrence = occurrences.get(key, 0)
        occurrences[key] = occurrence + 1
        monster["enemy_instance_id"] = stable_id(
            "enemy",
            {"floor": game.get("floor"), "id": key, "occurrence": occurrence, "max_hp": monster.get("max_hp")},
        )


def normalize_main_menu_progression(raw):
    """Project a loaded profile's final-act availability before START.

    The game can emit a main-menu frame before ``Settings.isFinalActAvailable``
    has been refreshed, despite all three loaded profile victory markers being
    true.  A new standard run refreshes that setting, so treating the transient
    menu value as false makes the audited Heart selection choose a character
    for an unlock run and fail closed immediately after START.  Normalize only
    this non-game projection; in-game final-act facts still come directly from
    the game's authoritative state.
    """

    if raw.get("in_game"):
        return
    if all(raw.get(flag) is True for flag in PROGRESSION_WIN_FLAGS):
        raw["key_system_unlocked"] = True


def canonical_neow_contract(value):
    if not isinstance(value, dict) or value.get("contract_version") != 1:
        return None
    kind = value.get("contract_kind")
    if kind == "NEOW_DIALOG_ADVANCE":
        if set(value) != {
            "contract_version", "contract_kind", "screen_num", "resource_effect",
        }:
            return None
        screen_num = value.get("screen_num")
        if (
            type(screen_num) is not int or screen_num == 3
            or value.get("resource_effect") != "NONE"
        ):
            return None
        return json.loads(canonical(value))
    if kind != "NEOW_REWARD" or set(value) != {
        "contract_version", "contract_kind", "reward_kind", "drawback_kind",
        "parameters",
    }:
        return None
    reward_kind = value.get("reward_kind")
    drawback_kind = value.get("drawback_kind")
    parameters = value.get("parameters")
    parameter_keys = set(parameters) if isinstance(parameters, dict) else set()
    # CommunicationMod's Gson intentionally omits null map values.  Java
    # represents a NONE drawback with ``drawback_def_kind = null``, so the
    # wire object contains only hp_bonus/cursed.  Requiring the omitted key
    # made every free Neow choice unbound while the one costly choice (whose
    # drawback enum is non-null) remained selectable.  The public NONE enum
    # is authoritative enough to restore exactly this one null; real
    # drawbacks must still carry and match their private definition.
    expected_parameter_keys = {"hp_bonus", "cursed", "drawback_def_kind"}
    gson_omitted_none = (
        drawback_kind == "NONE"
        and parameter_keys == {"hp_bonus", "cursed"}
    )
    if (
        reward_kind not in NEOW_REWARD_KINDS
        or drawback_kind not in NEOW_DRAWBACK_KINDS
        or not isinstance(parameters, dict)
        or (
            parameter_keys != expected_parameter_keys
            and not gson_omitted_none
        )
        or type(parameters.get("hp_bonus")) is not int
        or type(parameters.get("cursed")) is not bool
    ):
        return None
    drawback_def_kind = parameters.get("drawback_def_kind")
    if drawback_def_kind is not None and drawback_def_kind not in NEOW_DRAWBACK_KINDS:
        return None
    if drawback_def_kind is not None and drawback_def_kind != drawback_kind:
        return None
    normalized = json.loads(canonical(value))
    normalized["parameters"]["drawback_def_kind"] = drawback_def_kind
    return normalized


def _canonical_goop_event_contract(value):
    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "original_button_index", "option_kind", "parameters",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("contract_version") != 1
        or value.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or value.get("event_id") != "World of Goop"
        or value.get("event_class") != WORLD_OF_GOOP_EVENT_CLASS
        or type(value.get("original_button_index")) is not int
        or not isinstance(value.get("parameters"), dict)
    ):
        return None
    original_index = value["original_button_index"]
    option_kind = value.get("option_kind")
    parameters = value["parameters"]
    expected_parameters = {
        (0, "GATHER"): {"gold_gain", "hp_damage"},
        (1, "LEAVE"): {"gold_loss"},
        (0, "CONTINUE"): set(),
    }.get((original_index, option_kind))
    if expected_parameters is None or set(parameters) != expected_parameters:
        return None
    if any(type(item) is not int or item < 0 for item in parameters.values()):
        return None
    return json.loads(canonical(value))


def _exact_typed_value(value, expected):
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _exact_typed_value(value[key], expected[key])
            for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _exact_typed_value(item, expected_item)
            for item, expected_item in zip(value, expected)
        )
    return value == expected


def _canonical_cleric_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != CLERIC_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {
            "heal_amount", "heal_gold_cost", "purify_cost",
        }
        or type(instance.get("heal_amount")) is not int
        or instance["heal_amount"] < 0
        or instance.get("heal_gold_cost") != 35
        or instance.get("purify_cost") not in {50, 75}
        or not isinstance(parameters, dict)
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key == ("MAIN", 0, "HEAL"):
        expected = {
            "gold_cost": 35,
            "heal_amount": instance["heal_amount"],
        }
    elif key == ("MAIN", 1, "PURIFY"):
        expected = {
            "gold_cost_if_purgeable": instance["purify_cost"],
            "purge_select_count": 1,
            "selection_mode": "PLAYER_SELECT",
        }
    elif key in {
        ("MAIN", 2, "LEAVE"), ("RESULT", 0, "CONTINUE"),
    }:
        expected = {}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_designer_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != DESIGNER_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {
            "adjustment_upgrades_one", "clean_up_removes_cards",
            "adjust_cost", "clean_up_cost", "full_service_cost",
            "hp_loss",
        }
        or type(instance.get("adjustment_upgrades_one")) is not bool
        or type(instance.get("clean_up_removes_cards")) is not bool
        or not isinstance(parameters, dict)
    ):
        return None
    costs = (
        instance.get("adjust_cost"), instance.get("clean_up_cost"),
        instance.get("full_service_cost"), instance.get("hp_loss"),
    )
    if costs not in {(40, 60, 90, 3), (50, 75, 110, 5)}:
        return None
    stage = value.get("event_stage")
    index = value.get("original_button_index")
    kind = value.get("option_kind")
    if (stage, index, kind) in {
        ("INTRO", 0, "OPEN_SERVICES"),
        ("DONE", 0, "CONTINUE"),
    }:
        expected = {}
    elif stage == "MAIN" and index == 0:
        one = instance["adjustment_upgrades_one"]
        expected_kind = (
            "ADJUSTMENT_GRID_UPGRADE"
            if one else "ADJUSTMENT_RANDOM_UPGRADE"
        )
        if kind != expected_kind:
            return None
        expected = {"gold_cost": instance["adjust_cost"]}
        if one:
            expected.update({
                "upgrade_select_count": 1,
                "selection_mode": "PLAYER_SELECT",
            })
        else:
            expected.update({
                "upgrade_max_count": 2,
                "selection_mode": "RANDOM_UP_TO_AVAILABLE",
            })
    elif stage == "MAIN" and index == 1:
        removes = instance["clean_up_removes_cards"]
        expected_kind = (
            "CLEAN_UP_GRID_PURGE"
            if removes else "CLEAN_UP_GRID_TRANSFORM"
        )
        if kind != expected_kind:
            return None
        expected = {
            "gold_cost": instance["clean_up_cost"],
            "selection_mode": "PLAYER_SELECT",
        }
        if removes:
            expected["purge_select_count"] = 1
        else:
            expected.update({
                "transform_select_count": 2,
                "transform_result": "RANDOM",
            })
    elif (stage, index, kind) == ("MAIN", 2, "FULL_SERVICE"):
        expected = {
            "gold_cost": instance["full_service_cost"],
            "purge_select_count": 1,
            "random_upgrade_max_count": 1,
            "selection_mode": "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE",
        }
    elif (stage, index, kind) == ("MAIN", 3, "PUNCH_AND_LEAVE"):
        expected = {"hp_loss": instance["hp_loss"]}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _exact_cursed_tome_pool(pool):
    if not isinstance(pool, list) or not pool:
        return False
    if pool == ["Circlet"]:
        return True
    if any(type(item) is not str for item in pool):
        return False
    indexes = []
    for item in pool:
        try:
            indexes.append(CURSED_TOME_BOOK_IDS.index(item))
        except ValueError:
            return False
    return indexes == sorted(set(indexes))


def _expected_cursed_tome_pool(game):
    if not isinstance(game, dict) or not isinstance(game.get("relics"), list):
        return None
    owned = set()
    for relic in game["relics"]:
        if not isinstance(relic, dict) or type(relic.get("id")) is not str:
            return None
        if relic["id"] in CURSED_TOME_BOOK_IDS:
            owned.add(relic["id"])
    missing = [item for item in CURSED_TOME_BOOK_IDS if item not in owned]
    return missing or ["Circlet"]


def _canonical_cursed_tome_event_contract(value, game):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != CURSED_TOME_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {
            "final_hp_loss", "damage_taken", "random_relic_pool",
        }
        or instance.get("final_hp_loss") not in {10, 15}
        or type(instance.get("damage_taken")) is not int
        or not _exact_cursed_tome_pool(instance.get("random_relic_pool"))
        or not isinstance(parameters, dict)
    ):
        return None
    if game is not None:
        expected_pool = _expected_cursed_tome_pool(game)
        if expected_pool is None or instance["random_relic_pool"] != expected_pool:
            return None
    stage = value.get("event_stage")
    damage_taken = instance["damage_taken"]
    final_hp_loss = instance["final_hp_loss"]
    expected_damage = {
        "INTRO": {0}, "PAGE_1": {0}, "PAGE_2": {1},
        "PAGE_3": {3}, "LAST_PAGE": {6},
        "END": {0, 9, 6 + final_hp_loss},
    }.get(stage)
    if expected_damage is None or damage_taken not in expected_damage:
        return None
    key = (
        stage, value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key == ("INTRO", 0, "ENTER_RANDOM_BOOK_CHAIN"):
        expected = {
            "future_hp_loss_to_complete": 6 + final_hp_loss,
            "random_relic_count": 1,
            "reward_surface": "COMBAT_REWARD",
            "selection_mode": "UNIFORM_MISC_RNG",
        }
    elif key == ("INTRO", 1, "LEAVE"):
        expected = {}
    elif key == ("PAGE_1", 0, "READ_PAGE_1"):
        expected = {"hp_loss": 1}
    elif key == ("PAGE_2", 0, "READ_PAGE_2"):
        expected = {"hp_loss": 2}
    elif key == ("PAGE_3", 0, "READ_PAGE_3"):
        expected = {"hp_loss": 3}
    elif key == ("LAST_PAGE", 0, "COMPLETE_RANDOM_BOOK"):
        expected = {
            "hp_loss": final_hp_loss,
            "random_relic_count": 1,
            "reward_surface": "COMBAT_REWARD",
            "selection_mode": "UNIFORM_MISC_RNG",
        }
    elif key == ("LAST_PAGE", 1, "STOP"):
        expected = {"hp_loss": 3}
    elif key == ("END", 0, "PROCEED"):
        expected = {}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_mausoleum_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != MAUSOLEUM_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {"curse_probability_percent"}
        or instance.get("curse_probability_percent") not in {50, 100}
        or not isinstance(parameters, dict)
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key == ("INTRO", 0, "OPEN"):
        expected = {
            "random_relic_count": 1,
            "relic_selection_mode": "RANDOM_TIER_THEN_SCREENLESS_RELIC",
            "curse_card_id": "Writhe",
            "curse_probability_percent": instance[
                "curse_probability_percent"
            ],
        }
    elif key in {
        ("INTRO", 1, "LEAVE"), ("RESULT", 0, "CONTINUE"),
    }:
        expected = {}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_knowing_skull_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != KNOWING_SKULL_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {
            "potion_cost", "card_cost", "gold_cost", "leave_cost",
            "gold_reward",
        }
        or any(
            type(instance.get(name)) is not int
            or instance[name] <= 0
            for name in (
                "potion_cost", "card_cost", "gold_cost", "leave_cost",
            )
        )
        or instance.get("gold_reward") != 90
        or not isinstance(parameters, dict)
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key in {
        ("INTRO_1", 0, "OPEN_QUESTIONS"),
        ("COMPLETE", 0, "CONTINUE"),
    }:
        expected = {}
    elif key == ("ASK", 0, "TAKE_POTION"):
        expected = {
            "hp_loss": instance["potion_cost"],
            "reward_count": 1,
            "reward_kind": "RANDOM_POTION",
        }
    elif key == ("ASK", 1, "TAKE_GOLD"):
        expected = {
            "hp_loss": instance["gold_cost"],
            "gold_gain": 90,
        }
    elif key == ("ASK", 2, "TAKE_CARD"):
        expected = {
            "hp_loss": instance["card_cost"],
            "reward_count": 1,
            "reward_color": "COLORLESS",
            "reward_rarity": "UNCOMMON",
            "selection_mode": "RANDOM",
        }
    elif key == ("ASK", 3, "LEAVE"):
        expected = {"hp_loss": instance["leave_cost"]}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_dead_adventurer_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != DEAD_ADVENTURER_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {
            "num_rewards", "encounter_chance_percent",
            "remaining_rewards", "enemy_index", "encounter_id",
        }
        or type(instance.get("num_rewards")) is not int
        or type(instance.get("encounter_chance_percent")) is not int
        or type(instance.get("enemy_index")) is not int
        or not isinstance(instance.get("remaining_rewards"), list)
        or not isinstance(parameters, dict)
    ):
        return None
    num_rewards = instance["num_rewards"]
    chance = instance["encounter_chance_percent"]
    rewards = instance["remaining_rewards"]
    enemy = instance["enemy_index"]
    encounter_ids = ("3 Sentries", "Gremlin Nob", "Lagavulin Event")
    if (
        num_rewards not in {0, 1, 2, 3}
        or chance not in {
            25 + 25 * num_rewards, 35 + 25 * num_rewards,
        }
        or enemy not in {0, 1, 2}
        or instance.get("encounter_id") != encounter_ids[enemy]
        or len(rewards) != 3 - num_rewards
        or any(
            type(item) is not str
            or item not in ("GOLD", "NOTHING", "RELIC")
            for item in rewards
        )
        or len(set(rewards)) != len(rewards)
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key == ("INTRO", 0, "SEARCH") and rewards:
        reward = rewards[0]
        expected = {
            "roll_min": 0,
            "roll_max_inclusive": 99,
            "encounter_roll_lt": chance,
            "encounter_id": encounter_ids[enemy],
            "encounter_reward_gold_min": 25,
            "encounter_reward_gold_max": 35,
            "success_reward_kind": reward,
            "success_gold_gain": 30 if reward == "GOLD" else 0,
            "success_random_relic_count": 1 if reward == "RELIC" else 0,
            "success_relic_selection_mode": (
                "RANDOM_TIER_THEN_SCREENLESS_RELIC"
                if reward == "RELIC" else "NONE"
            ),
        }
    elif key == ("INTRO", 1, "LEAVE") and rewards:
        expected = {}
    elif key == ("FAIL", 0, "FIGHT"):
        expected = {
            "encounter_id": encounter_ids[enemy],
            "combat_reward_gold_min": 25,
            "combat_reward_gold_max": 35,
        }
    elif key in {
        ("SUCCESS", 0, "CONTINUE"),
        ("ESCAPE", 0, "CONTINUE"),
    }:
        expected = {}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_scrap_ooze_event_contract(value, game):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != SCRAP_OOZE_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {
            "relic_chance_percent_displayed", "damage",
            "total_damage_dealt", "screen_num",
        }
        or any(type(instance.get(name)) is not int for name in instance)
        or not isinstance(parameters, dict)
    ):
        return None
    chance = instance["relic_chance_percent_displayed"]
    damage = instance["damage"]
    total = instance["total_damage_dealt"]
    screen_num = instance["screen_num"]
    if chance < 25 or chance > 105 or (chance - 25) % 10:
        return None
    attempts = (chance - 25) // 10
    ascension = game.get("ascension_level") if isinstance(game, dict) else None
    base_damage = 5 if type(ascension) is int and ascension >= 15 else 3
    failed_attempt_damage = attempts * (2 * base_damage + attempts - 1) // 2
    possible_result_damage = {
        failed_attempt_damage,
        failed_attempt_damage + damage,
    }
    if (
        type(ascension) is not int
        or damage != base_damage + attempts
        or screen_num not in {0, 1}
        or (
            screen_num == 0
            and total != failed_attempt_damage
        )
        or (
            screen_num == 1
            and total not in possible_result_damage
        )
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key == ("MAIN", 0, "REACH_INSIDE") and screen_num == 0:
        expected = {
            "hp_loss": damage,
            "roll_min": 0,
            "roll_max_inclusive": 99,
            "success_roll_min_inclusive": 99 - chance,
            "success_random_relic_count": 1,
            "success_relic_selection_mode": (
                "RANDOM_TIER_THEN_SCREENLESS_RELIC"
            ),
        }
    elif key == ("MAIN", 1, "LEAVE") and screen_num == 0:
        expected = {}
    elif key == ("RESULT", 0, "CONTINUE") and screen_num == 1:
        expected = {}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _expected_face_trader_pool(game):
    if not isinstance(game, dict) or not isinstance(game.get("relics"), list):
        return None
    owned = set()
    for relic in game["relics"]:
        if not isinstance(relic, dict) or type(relic.get("id")) is not str:
            return None
        owned.add(relic["id"])
    pool = [item for item in FACE_TRADER_RELIC_IDS if item not in owned]
    return pool or ["Circlet"]


def _canonical_face_trader_event_contract(value, game):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != FACE_TRADER_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {"gold_reward", "damage", "random_face_pool"}
        or type(instance.get("gold_reward")) is not int
        or type(instance.get("damage")) is not int
        or not isinstance(parameters, dict)
    ):
        return None
    ascension = game.get("ascension_level") if isinstance(game, dict) else None
    max_hp = game.get("max_hp") if isinstance(game, dict) else None
    pool = _expected_face_trader_pool(game)
    if (
        type(ascension) is not int or type(max_hp) is not int
        or instance["gold_reward"] != (50 if ascension >= 15 else 75)
        or instance["damage"] != max(1, max_hp // 10)
        or pool is None or instance.get("random_face_pool") != pool
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key in {
        ("INTRO", 0, "OPEN"),
        ("MAIN", 2, "LEAVE"),
        ("RESULT", 0, "CONTINUE"),
    }:
        expected = {}
    elif key == ("MAIN", 0, "TOUCH"):
        expected = {
            "hp_loss": instance["damage"],
            "gold_gain": instance["gold_reward"],
        }
    elif key == ("MAIN", 1, "TRADE"):
        expected = {
            "random_relic_pool": pool,
            "random_relic_count": 1,
            "selection_mode": "UNIFORM_MISC_RNG_SHUFFLE_FIRST",
        }
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_duplicator_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != DUPLICATOR_EVENT_CLASS
        or not isinstance(instance, dict)
        or set(instance) != {"screen_num"}
        or instance.get("screen_num") not in {0, 2}
        or not isinstance(parameters, dict)
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key == ("MAIN", 0, "DUPLICATE") and instance["screen_num"] == 0:
        expected = {
            "duplicate_select_count": 1,
            "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
        }
    elif key == ("MAIN", 1, "LEAVE") and instance["screen_num"] == 0:
        expected = {}
    elif key == ("RESULT", 0, "CONTINUE") and instance["screen_num"] == 2:
        expected = {}
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def _canonical_bonfire_event_contract(value):
    instance = value.get("instance_parameters")
    parameters = value.get("parameters")
    if (
        value.get("event_class") != BONFIRE_EVENT_CLASS
        or instance != {"card_select": False}
        or not isinstance(parameters, dict)
    ):
        return None
    key = (
        value.get("event_stage"), value.get("original_button_index"),
        value.get("option_kind"),
    )
    if key in {
        ("INTRO", 0, "CONTINUE"),
        ("COMPLETE", 0, "CONTINUE"),
    }:
        expected = {}
    elif key == ("CHOOSE", 0, "OFFER_CARD"):
        expected = {
            "offer_select_count": 1,
            "selection_mode": (
                "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
            ),
        }
    else:
        return None
    return value if _exact_typed_value(parameters, expected) else None


def canonical_event_contract(value, game=None):
    """Validate authoritative, reflection-backed base-game event evidence."""

    if not isinstance(value, dict):
        return None
    if value.get("event_id") == "World of Goop":
        return _canonical_goop_event_contract(value)
    required = {
        "contract_version", "contract_kind", "event_id", "event_class",
        "event_stage", "original_button_index", "option_kind",
        "instance_parameters", "parameters",
    }
    if (
        set(value) != required
        or value.get("contract_version") != 1
        or value.get("contract_kind") != "BASE_GAME_EVENT_OPTION"
        or type(value.get("event_stage")) is not str
        or type(value.get("original_button_index")) is not int
    ):
        return None
    event_id = value.get("event_id")
    if event_id == "The Cleric":
        checked = _canonical_cleric_event_contract(value)
    elif event_id == "Designer":
        checked = _canonical_designer_event_contract(value)
    elif event_id == "Cursed Tome":
        checked = _canonical_cursed_tome_event_contract(value, game)
    elif event_id == "The Mausoleum":
        checked = _canonical_mausoleum_event_contract(value)
    elif event_id == "Knowing Skull":
        checked = _canonical_knowing_skull_event_contract(value)
    elif event_id == "Dead Adventurer":
        checked = _canonical_dead_adventurer_event_contract(value)
    elif event_id == "Scrap Ooze":
        checked = _canonical_scrap_ooze_event_contract(value, game)
    elif event_id == "Face Trader":
        checked = _canonical_face_trader_event_contract(value, game)
    elif event_id == "Duplicator":
        checked = _canonical_duplicator_event_contract(value)
    elif event_id == "Bonfire Elementals":
        checked = _canonical_bonfire_event_contract(value)
    else:
        return None
    return json.loads(canonical(checked)) if checked is not None else None


_NEOW_GRID_FOLLOWUPS = {
    "REMOVE_CARD": ("remove", 1),
    "REMOVE_TWO": ("remove", 2),
    "UPGRADE_CARD": ("upgrade", 1),
    "TRANSFORM_CARD": ("transform", 1),
    "TRANSFORM_TWO_CARDS": ("transform", 2),
}


def accepted_grid_followup_context(payload, state):
    """Bind a typed multistage choice to its subsequent ambiguous GRID."""

    phase = str(state.get("phase") or "").upper()
    if str(payload.get("action") or "").lower() != "choose":
        return None
    matches = [
        option for option in state.get("options") or []
        if isinstance(option, dict)
        and option.get("option_id") == payload.get("option_id")
    ]
    if len(matches) != 1:
        return None
    option = matches[0]
    target = option.get("target")
    target = target if isinstance(target, dict) else {}
    if phase == "BOSS_REWARD":
        relic = target.get("relic")
        relic = relic if isinstance(relic, dict) else {}
        if (
            target.get("kind") != "relic"
            or normalized_game_id(relic.get("id") or relic.get("name"))
            != "astrolabe"
        ):
            return None
        return {
            "authority": "accepted_protocol_choice",
            "parent_phase": "BOSS_REWARD",
            "source_option_id": option.get("option_id"),
            "source_choice_index": option.get("choice_index"),
            "relic_id": relic.get("id"),
            "operation": "transform",
            "select_count": 3,
        }
    if phase == "COMBAT_REWARD":
        reward = target.get("reward")
        reward = reward if isinstance(reward, dict) else {}
        relic = reward.get("relic")
        relic = relic if isinstance(relic, dict) else {}
        relic_id = normalized_game_id(relic.get("id") or relic.get("name"))
        bottle = {
            "bottledflame": ("bottle_attack", "ATTACK"),
            "bottledlightning": ("bottle_skill", "SKILL"),
            "bottledtornado": ("bottle_power", "POWER"),
        }.get(relic_id)
        if not (
            str(reward.get("reward_type") or "").upper() == "RELIC"
            and bottle is not None
        ):
            return None
        operation, card_type = bottle
        return {
            "authority": "accepted_protocol_choice",
            "parent_phase": "COMBAT_REWARD",
            "source_option_id": option.get("option_id"),
            "source_choice_index": option.get("choice_index"),
            "relic_id": relic.get("id"),
            "operation": operation,
            "card_type": card_type,
            "select_count": 1,
        }
    if phase == "EVENT":
        game = state.get("game_state")
        game = game if isinstance(game, dict) else {}
        screen = game.get("screen_state")
        screen = screen if isinstance(screen, dict) else {}
        event_id = normalized_game_id(target.get("event_id"))
        if (
            event_id == "thelibrary"
            and target.get("original_button_index") == 0
            and screen.get("event_class")
            == "com.megacrit.cardcrawl.events.city.TheLibrary"
        ):
            visible = screen.get("options")
            visible = visible if isinstance(visible, list) else []
            original_indexes = sorted(
                entry.get("original_button_index")
                for entry in visible if isinstance(entry, dict)
                and type(entry.get("original_button_index")) is int
            )
            if original_indexes != [0, 1]:
                return None
            mechanism = {
                "event_id": "The Library",
                "event_class": screen["event_class"],
                "original_button_index": 0,
                "operation": "gain",
                "select_count": 1,
                "selection_domain": "library_card_offering",
            }
            return {
                "authority": "accepted_protocol_choice",
                "parent_phase": "EVENT",
                "source_option_id": option.get("option_id"),
                "source_choice_index": option.get("choice_index"),
                "mechanism_id": stable_id("event-grid-mechanism", mechanism),
                **mechanism,
            }
        if (
            event_id in {"noteforyourself", "anoteforyourself"}
            and target.get("original_button_index") == 0
            and screen.get("event_class")
            == "com.megacrit.cardcrawl.events.shrines.NoteForYourself"
        ):
            offered_card = target.get("card")
            offered_card = (
                dict(offered_card) if isinstance(offered_card, dict) else None
            )
            visible = screen.get("options")
            visible = visible if isinstance(visible, list) else []
            original_indexes = sorted(
                entry.get("original_button_index")
                for entry in visible if isinstance(entry, dict)
                and type(entry.get("original_button_index")) is int
            )
            if (
                original_indexes != [0, 1]
                or not offered_card
                or not offered_card.get("id")
                or not offered_card.get("card_instance_id")
            ):
                return None
            mechanism = {
                "event_id": "NoteForYourself",
                "event_class": screen["event_class"],
                "original_button_index": 0,
                "operation": "note_exchange",
                "select_count": 1,
                "offered_card": offered_card,
            }
            return {
                "authority": "accepted_protocol_choice",
                "parent_phase": "EVENT",
                "source_option_id": option.get("option_id"),
                "source_choice_index": option.get("choice_index"),
                "mechanism_id": stable_id("event-grid-mechanism", mechanism),
                **mechanism,
            }
        if (
            event_id == "drugdealer"
            and target.get("original_button_index") == 1
            and screen.get("event_class")
            == "com.megacrit.cardcrawl.events.city.DrugDealer"
        ):
            visible = screen.get("options")
            visible = visible if isinstance(visible, list) else []
            original_indexes = sorted(
                entry.get("original_button_index")
                for entry in visible if isinstance(entry, dict)
                and type(entry.get("original_button_index")) is int
            )
            if original_indexes != [0, 1, 2]:
                return None
            mechanism = {
                "event_id": "Drug Dealer",
                "event_class": screen["event_class"],
                "original_button_index": 1,
                "operation": "transform",
                "select_count": 2,
            }
            return {
                "authority": "accepted_protocol_choice",
                "parent_phase": "EVENT",
                "source_option_id": option.get("option_id"),
                "source_choice_index": option.get("choice_index"),
                "mechanism_id": stable_id("event-grid-mechanism", mechanism),
                **mechanism,
            }
        contract = canonical_event_contract(
            target.get("event_contract"), game=game
        )
        if not isinstance(contract, dict):
            return None
        if (
            contract.get("event_id") == "Duplicator"
            and contract.get("event_stage") == "MAIN"
            and contract.get("option_kind") == "DUPLICATE"
            and contract.get("parameters") == {
                "duplicate_select_count": 1,
                "selection_mode": "PLAYER_SELECT_CURRENT_DECK",
            }
        ):
            operation = "duplicate"
        elif (
            contract.get("event_id") == "Bonfire Elementals"
            and contract.get("event_stage") == "CHOOSE"
            and contract.get("option_kind") == "OFFER_CARD"
            and contract.get("parameters") == {
                "offer_select_count": 1,
                "selection_mode": (
                    "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK"
                ),
            }
        ):
            operation = "remove"
        else:
            return None
        mechanism_id = stable_id("event-mechanism", contract)
        if target.get("mechanism_id") != mechanism_id:
            return None
        return {
            "authority": "accepted_protocol_choice",
            "parent_phase": "EVENT",
            "source_option_id": option.get("option_id"),
            "source_choice_index": option.get("choice_index"),
            "mechanism_id": mechanism_id,
            "event_contract": contract,
            "operation": operation,
            "select_count": 1,
        }
    if phase != "NEOW":
        return None
    contract = canonical_neow_contract(target.get("neow_contract"))
    if not isinstance(contract, dict):
        return None
    followup = _NEOW_GRID_FOLLOWUPS.get(contract.get("reward_kind"))
    if followup is None:
        return None
    operation, select_count = followup
    mechanism_id = stable_id("neow-mechanism", contract)
    if target.get("mechanism_id") != mechanism_id:
        return None
    return {
        "authority": "accepted_protocol_choice",
        "parent_phase": "NEOW",
        "source_option_id": option.get("option_id"),
        "source_choice_index": option.get("choice_index"),
        "mechanism_id": mechanism_id,
        "neow_contract": contract,
        "operation": operation,
        "select_count": select_count,
    }


def _unbound_combat_reward(index, status, reward=None):
    return {
        "kind": "reward_unbound",
        "choice_index": index,
        "binding_status": status,
        "reward": reward if isinstance(reward, dict) else None,
    }


def _combat_reward_descriptor(index, screen):
    rewards = screen.get("rewards") or []
    if not isinstance(rewards, list):
        return _unbound_combat_reward(index, "rewards_not_a_list")
    explicit_indexes = any(
        isinstance(reward, dict) and "choice_index" in reward
        for reward in rewards
    )
    if explicit_indexes:
        matches = [
            reward for reward in rewards
            if isinstance(reward, dict)
            and type(reward.get("choice_index")) is int
            and reward["choice_index"] == index
        ]
        if len(matches) != 1:
            return _unbound_combat_reward(
                index, "missing_or_duplicate_choice_index"
            )
        reward = matches[0]
    elif index < len(rewards):
        # Compatibility for historical protocol frames.  Current Java emits
        # explicit choice_index on every reward, so live binding never relies
        # on mutable container order.
        reward = rewards[index]
    else:
        return _unbound_combat_reward(index, "missing_reward")
    if not isinstance(reward, dict):
        return _unbound_combat_reward(index, "reward_not_an_object")
    reward_type = str(reward.get("reward_type") or "").upper()
    if not reward_type:
        return _unbound_combat_reward(
            index, "missing_reward_type", reward
        )
    if reward_type == "SAPPHIRE_KEY":
        key_count = sum(
            1 for item in rewards
            if isinstance(item, dict)
            and str(item.get("reward_type") or "").upper()
            == "SAPPHIRE_KEY"
        )
        if key_count != 1:
            return _unbound_combat_reward(
                index, "duplicate_sapphire_key_reward", reward
            )
        return {
            "kind": "sapphire_key",
            "reward": reward,
            "link": reward.get("link"),
        }
    return {"kind": "reward", "reward": reward}


def _unbound_event_option(index, event_id, status):
    return {
        "kind": "event_option_unbound",
        "event_id": event_id,
        "choice_index": index,
        "binding_status": status,
    }


_TYPED_EVENT_IDS = {
    "worldofgoop", "thecleric", "designer", "cursedtome",
    "themausoleum", "knowingskull", "deadadventurer", "scrapooze",
    "facetrader", "duplicator", "bonfireelementals",
}

_PROGRESS_EVENT_CLASSES = {
    "liarsgame": (
        "com.megacrit.cardcrawl.events.exordium.Sssserpent", "event_stage",
    ),
    "vampires": (
        "com.megacrit.cardcrawl.events.city.Vampires", "screen_num",
    ),
    "goldenshrine": (
        "com.megacrit.cardcrawl.events.shrines.GoldShrine", "event_stage",
    ),
    "shininglight": (
        "com.megacrit.cardcrawl.events.exordium.ShiningLight", "event_stage",
    ),
    "mushrooms": (
        "com.megacrit.cardcrawl.events.exordium.Mushrooms", "screen_num",
    ),
    "maskedbandits": (
        "com.megacrit.cardcrawl.events.city.MaskedBandits", "event_stage",
    ),
    "colosseum": (
        "com.megacrit.cardcrawl.events.city.Colosseum", "event_stage",
    ),
    "windinghalls": (
        "com.megacrit.cardcrawl.events.beyond.WindingHalls", "screen_num",
    ),
    "sensorystone": (
        "com.megacrit.cardcrawl.events.beyond.SensoryStone", "event_stage",
    ),
    "accursedblacksmith": (
        "com.megacrit.cardcrawl.events.shrines.AccursedBlacksmith", "screen_num",
    ),
    "mysterioussphere": (
        "com.megacrit.cardcrawl.events.beyond.MysteriousSphere", "event_stage",
    ),
    "spireheart": (
        "com.megacrit.cardcrawl.events.beyond.SpireHeart", "event_stage",
    ),
    "bonfireelementals": (BONFIRE_EVENT_CLASS, "event_stage"),
}


def _validated_event_progress(screen):
    """Return exact class/stage evidence emitted by the instrumented bridge."""

    event_id = normalized_game_id((screen or {}).get("event_id"))
    expected = _PROGRESS_EVENT_CLASSES.get(event_id)
    if expected is None:
        return {}
    expected_class, progress_field = expected
    event_class = (screen or {}).get("event_class")
    progress = (screen or {}).get(progress_field)
    if event_class != expected_class:
        return {}
    if progress_field == "event_stage":
        if not isinstance(progress, str) or not progress:
            return {}
    elif type(progress) is not int or progress < 0:
        return {}
    return {"event_class": event_class, progress_field: progress}


def _canonical_event_surface_contracts(game, screen):
    event_token = normalized_game_id(screen.get("event_id"))
    if event_token not in _TYPED_EVENT_IDS:
        return None, None
    options = screen.get("options")
    if not isinstance(options, list):
        return None, "event_contract_options_not_a_list"
    by_original = {}
    for item in options:
        if not isinstance(item, dict):
            return None, "event_contract_option_not_an_object"
        original = item.get("original_button_index")
        contract = canonical_event_contract(
            item.get("event_contract"), game=game
        )
        if (
            contract is None
            or contract.get("original_button_index") != original
            or normalized_game_id(contract.get("event_id")) != event_token
            or original in by_original
        ):
            return None, "missing_invalid_or_mismatched_event_contract"
        by_original[original] = contract

    if event_token == "worldofgoop":
        shape = {
            index: contract.get("option_kind")
            for index, contract in by_original.items()
        }
        if shape not in (
            {0: "GATHER", 1: "LEAVE"},
            {0: "CONTINUE"},
        ):
            return None, "world_of_goop_contract_surface_mismatch"
        return by_original, None

    stages = {contract.get("event_stage") for contract in by_original.values()}
    instances = {
        canonical(contract.get("instance_parameters"))
        for contract in by_original.values()
    }
    if len(stages) != 1 or len(instances) != 1:
        return None, "event_contract_stage_or_instance_mismatch"
    stage = next(iter(stages))
    expected_originals = {
        ("thecleric", "MAIN"): {0, 1, 2},
        ("thecleric", "RESULT"): {0},
        ("designer", "INTRO"): {0},
        ("designer", "MAIN"): {0, 1, 2, 3},
        ("designer", "DONE"): {0},
        ("cursedtome", "INTRO"): {0, 1},
        ("cursedtome", "PAGE_1"): {0},
        ("cursedtome", "PAGE_2"): {0},
        ("cursedtome", "PAGE_3"): {0},
        ("cursedtome", "LAST_PAGE"): {0, 1},
        ("cursedtome", "END"): {0},
        ("themausoleum", "INTRO"): {0, 1},
        ("themausoleum", "RESULT"): {0},
        ("knowingskull", "INTRO_1"): {0},
        ("knowingskull", "ASK"): {0, 1, 2, 3},
        ("knowingskull", "COMPLETE"): {0},
        ("deadadventurer", "INTRO"): {0, 1},
        ("deadadventurer", "FAIL"): {0},
        ("deadadventurer", "SUCCESS"): {0},
        ("deadadventurer", "ESCAPE"): {0},
        ("scrapooze", "MAIN"): {0, 1},
        ("scrapooze", "RESULT"): {0},
        ("facetrader", "INTRO"): {0},
        ("facetrader", "MAIN"): {0, 1, 2},
        ("facetrader", "RESULT"): {0},
        ("duplicator", "MAIN"): {0, 1},
        ("duplicator", "RESULT"): {0},
        ("bonfireelementals", "INTRO"): {0},
        ("bonfireelementals", "CHOOSE"): {0},
        ("bonfireelementals", "COMPLETE"): {0},
    }.get((event_token, stage))
    if expected_originals is None or set(by_original) != expected_originals:
        return None, "event_contract_stage_surface_mismatch"
    if event_token == "themausoleum" and stage == "INTRO":
        open_items = [
            item for item in options
            if item.get("original_button_index") == 0
        ]
        if len(open_items) != 1:
            return None, "mausoleum_writhe_preview_missing"
        preview = open_items[0].get("card")
        if (
            not isinstance(preview, dict)
            or preview.get("id") != "Writhe"
            or preview.get("type") != "CURSE"
            or preview.get("rarity") != "CURSE"
        ):
            return None, "mausoleum_writhe_preview_mismatch"
    return by_original, None


def _bound_event_surface_item(index, game, screen):
    """Bind compressed CHOOSE index to an unfiltered event button exactly."""

    options = screen.get("options")
    event_id = screen.get("event_id")
    if not isinstance(options, list):
        return None, _unbound_event_option(
            index, event_id, "options_not_a_list"
        )
    choices = game.get("choice_list")
    if not isinstance(choices, list):
        return None, _unbound_event_option(
            index, event_id, "choice_list_not_a_list"
        )

    original_indexes = []
    enabled_choice_indexes = []
    for item in options:
        if not isinstance(item, dict):
            return None, _unbound_event_option(
                index, event_id, "option_not_an_object"
            )
        original = item.get("original_button_index")
        if type(original) is not int or original < 0:
            return None, _unbound_event_option(
                index, event_id, "missing_or_invalid_original_button_index"
            )
        original_indexes.append(original)
        disabled = item.get("disabled")
        if type(disabled) is not bool:
            return None, _unbound_event_option(
                index, event_id, "invalid_disabled_flag"
            )
        if disabled:
            if "choice_index" in item:
                return None, _unbound_event_option(
                    index, event_id, "disabled_option_has_choice_index"
                )
            continue
        choice_index = item.get("choice_index")
        if type(choice_index) is not int or choice_index < 0:
            return None, _unbound_event_option(
                index, event_id, "missing_or_invalid_choice_index"
            )
        enabled_choice_indexes.append(choice_index)

    if sorted(original_indexes) != list(range(len(options))):
        return None, _unbound_event_option(
            index, event_id,
            "missing_duplicate_or_noncontiguous_original_button_index",
        )
    if (
        sorted(enabled_choice_indexes)
        != list(range(len(enabled_choice_indexes)))
        or len(enabled_choice_indexes) != len(choices)
    ):
        return None, _unbound_event_option(
            index, event_id, "invalid_compressed_choice_index_surface"
        )
    _contracts, contract_error = _canonical_event_surface_contracts(
        game, screen
    )
    if contract_error is not None:
        return None, _unbound_event_option(
            index, event_id, contract_error
        )
    matches = [
        item for item in options
        if item.get("disabled") is False
        and item.get("choice_index") == index
    ]
    if len(matches) != 1:
        return None, _unbound_event_option(
            index, event_id, "missing_or_duplicate_choice_index"
        )
    return matches[0], None


def option_descriptor(screen_type, index, label, game, screen):
    stype = screen_type.upper()
    if stype == "CARD_REWARD":
        cards = screen.get("cards") or []
        if index < len(cards):
            card = cards[index]
            return {"kind": "card", "card_instance_id": card.get("card_instance_id"), "card": card}
        return {"kind": "bowl", "label": label}
    if stype == "BOSS_REWARD":
        relics = screen.get("relics") or []
        if index < len(relics):
            return {"kind": "relic", "relic": relics[index]}
    if stype in {"COMBAT_REWARD", "SAPPHIRE_KEY"}:
        return _combat_reward_descriptor(index, screen)
    if stype == "MAP":
        if screen.get("boss_available"):
            return {"kind": "map_boss", "act": game.get("act")}
        nodes = screen.get("next_nodes") or []
        if index < len(nodes):
            node = nodes[index]
            return {"kind": "map_node", "x": node.get("x"), "y": node.get("y"), "symbol": node.get("symbol")}
    if stype == "EVENT":
        item, unbound = _bound_event_surface_item(index, game, screen)
        if item is not None:
            original_index = item["original_button_index"]
            surface_contracts, _contract_error = (
                _canonical_event_surface_contracts(game, screen)
            )
            event_contract = (
                surface_contracts.get(original_index)
                if isinstance(surface_contracts, dict)
                else canonical_event_contract(
                    item.get("event_contract"), game=game
                )
            )
            if "event_contract" in item and event_contract is None:
                return _unbound_event_option(
                    index, screen.get("event_id"),
                    "invalid_event_contract",
                )
            if event_contract is not None:
                if (
                    event_contract["original_button_index"]
                    != original_index
                    or normalized_game_id(screen.get("event_id"))
                    != normalized_game_id(event_contract.get("event_id"))
                ):
                    return _unbound_event_option(
                        index, screen.get("event_id"),
                        "event_contract_target_mismatch",
                    )
                mechanism_id = stable_id(
                    "event-mechanism", event_contract
                )
                return {
                    "kind": "event_option",
                    "event_id": screen.get("event_id"),
                    "original_button_index": original_index,
                    "event_contract": event_contract,
                    "mechanism_id": mechanism_id,
                }
            if normalized_game_id(screen.get("event_id")) in _TYPED_EVENT_IDS:
                return _unbound_event_option(
                    index, screen.get("event_id"),
                    "missing_or_invalid_event_contract",
                )
            neow_contract = canonical_neow_contract(item.get("neow_contract"))
            if neow_contract is not None:
                mechanism_id = stable_id("neow-mechanism", neow_contract)
                return {
                    "kind": "event_option",
                    "event_id": screen.get("event_id"),
                    "original_button_index": original_index,
                    "neow_contract": neow_contract,
                    "mechanism_id": mechanism_id,
                }
            if normalized_game_id(screen.get("event_id")) in {
                "neow", "neowevent",
            }:
                return {
                    "kind": "event_option_unbound",
                    "event_id": screen.get("event_id"),
                    "choice_index": index,
                    "binding_status": "missing_or_invalid_neow_contract",
                }
            return {
                "kind": "event_option",
                "event_id": screen.get("event_id"),
                "original_button_index": original_index,
                "label": item.get("label"),
                "text": item.get("text"),
                "card": item.get("card"),
                **_validated_event_progress(screen),
            }
        return unbound
    if stype in {"GRID", "HAND_SELECT"}:
        cards = screen.get("cards") or screen.get("hand") or []
        if index < len(cards):
            card = cards[index]
            return {"kind": "card", "card_instance_id": card.get("card_instance_id"), "card": card}
    if stype == "REST":
        rest_options = screen.get("rest_options") or []
        if index < len(rest_options):
            return {
                "kind": "rest",
                "rest_option": str(rest_options[index]).upper(),
            }
    if stype == "SHOP_SCREEN":
        items = []
        gold = int(game.get("gold") or 0)
        if screen.get("purge_available") and int(screen.get("purge_cost") or 10**9) <= gold:
            items.append({"kind": "purge", "price": screen.get("purge_cost")})
        for kind in ("cards", "relics", "potions"):
            for item in screen.get(kind) or []:
                if int(item.get("price") or 0) <= gold:
                    items.append({"kind": kind[:-1], "item": item})
        if index < len(items):
            return items[index]
    return {"kind": stype.lower(), "label": label}


def build_options(raw):
    game = raw.get("game_state") or {}
    screen_type = str(game.get("screen_type") or "NONE")
    screen = game.get("screen_state") or {}
    choices = game.get("choice_list") or []
    options = []
    used = {}
    for index, label in enumerate(choices):
        descriptor = option_descriptor(screen_type, index, label, game, screen)
        base = stable_id("option", descriptor)
        occurrence = used.get(base, 0)
        used[base] = occurrence + 1
        option_id = base if occurrence == 0 else f"{base}:{occurrence}"
        options.append(
            {"option_id": option_id, "choice_index": index, "label": label, "target": descriptor}
        )
    return options


def enrich_state(raw_state, sequence, parent_choice_context=None):
    state = json.loads(canonical(raw_state))
    normalize_main_menu_progression(state)
    annotate_instances(state)
    initializing_combat = has_uninitialized_combat_intent(state)
    if initializing_combat:
        # Keep the transport responsive, but expose no gameplay command until
        # the monster move roll and intent calculation have actually finished.
        state["available_commands"] = [
            command
            for command in state.get("available_commands") or []
            if str(command).lower() in {"wait", "state"}
        ]
        state["ready_for_command"] = False
        state["unstable_reason"] = "uninitialized_monster_intent"
    phase = "COMBAT_INITIALIZING" if initializing_combat else derive_phase(state)
    if phase == "GRID" and isinstance(parent_choice_context, dict):
        game = state.get("game_state") or {}
        screen = game.get("screen_state") or {}
        screen["parent_choice_context"] = json.loads(
            canonical(parent_choice_context)
        )
        game["screen_state"] = screen
        state["game_state"] = game
    options = build_options(state)
    game = state.get("game_state") or {}
    screen = game.get("screen_state") or {}
    combat = game.get("combat_state") or {}
    decision_material = {
        "phase": phase,
        "screen_name": game.get("screen_name"),
        "is_screen_up": bool(game.get("is_screen_up")),
        "available_commands": state.get("available_commands") or [],
        "options": [item["option_id"] for item in options],
        "hand": [item.get("card_instance_id") for item in combat.get("hand") or []],
        "enemies": [item.get("enemy_instance_id") for item in combat.get("monsters") or []],
        "potions": [item.get("potion_instance_id") for item in game.get("potions") or []],
    }
    if phase in {"EVENT", "NEOW"}:
        # Multi-page events often reuse the exact same Continue button.  The
        # option ID therefore stays stable even though a successful click has
        # advanced the event dialogue.  Bind the authoritative full body text
        # into the decision surface so that page progression is observable
        # and cannot be mistaken for a failed click/replayed command.
        decision_material["event_context"] = {
            "event_id": screen.get("event_id"),
            "body_text": screen.get("body_text"),
        }
    if phase == "GRID" and isinstance(parent_choice_context, dict):
        decision_material["parent_choice_context"] = parent_choice_context
    decision_id = stable_id("decision", decision_material)
    state["protocol_version"] = PROTOCOL_VERSION
    state["state_seq"] = sequence
    state["phase"] = phase
    state["decision_id"] = decision_id
    state["legal_actions"] = list(state.get("available_commands") or [])
    state["options"] = options
    binding = active_attempt_binding(state)
    if binding is not None:
        state.update(binding)
    return state


def _required_binding(payload, state):
    try:
        expected_seq = int(payload["expected_seq"])
        decision_id = str(payload["decision_id"])
        phase = str(payload["phase"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandRejected(f"missing or invalid state binding: {exc}") from exc
    if expected_seq != state["state_seq"]:
        raise CommandRejected(f"stale state: expected {expected_seq}, current {state['state_seq']}")
    if decision_id != state["decision_id"]:
        raise CommandRejected("decision_id mismatch")
    if phase != state["phase"]:
        raise CommandRejected(f"phase mismatch: expected {phase}, current {state['phase']}")

    # MAIN_MENU start/resume commands necessarily precede an authoritative
    # run identity.  Every command issued once a run is active, however, must
    # carry the controller's complete frozen attempt identity.  Derive it
    # again at acceptance time so a state frame produced just before
    # run-context.json was created cannot create an unbound first action.
    if state.get("in_game") is not True:
        return {}
    projected_attempt = (
        {
            field: state[field]
            for field in ATTEMPT_BINDING_FIELDS
        }
        if all(field in state for field in ATTEMPT_BINDING_FIELDS)
        else None
    )
    derived_attempt = active_attempt_binding(state)
    if projected_attempt is not None and derived_attempt is not None:
        if any(
            type(projected_attempt[field]) is not type(derived_attempt[field])
            or projected_attempt[field] != derived_attempt[field]
            for field in ATTEMPT_BINDING_FIELDS
        ):
            raise CommandRejected("active state attempt binding is inconsistent")
    expected_attempt = projected_attempt or derived_attempt
    if not isinstance(expected_attempt, dict):
        raise CommandRejected("active attempt binding is missing or invalid")
    for field in ATTEMPT_BINDING_FIELDS:
        if field not in payload:
            raise CommandRejected(f"missing attempt binding: {field}")
        expected = expected_attempt.get(field)
        observed = payload.get(field)
        if type(observed) is not type(expected) or observed != expected:
            raise CommandRejected(f"attempt binding mismatch: {field}")
    return {
        field: expected_attempt[field]
        for field in ATTEMPT_BINDING_FIELDS
    }


def _rejected_attempt_binding(payload, state):
    """Preserve the requested binding on a rejected in-run receipt."""

    if state.get("in_game") is not True:
        return {}
    return {
        field: payload.get(field)
        for field in ATTEMPT_BINDING_FIELDS
    }


def _require_command(state, name):
    aliases = {
        "proceed": {"proceed", "confirm"},
        "return": {"skip", "cancel", "return", "leave"},
    }
    available = {str(item).lower() for item in state.get("legal_actions") or []}
    accepted = aliases.get(name.lower(), {name.lower()})
    if not (available & accepted):
        raise CommandRejected(f"command is not legal in current state: {name}")


def resolve_command(payload, state):
    """Validate one structured payload and return (raw_command, receipt fields)."""
    action = str(payload.get("action") or "").lower()
    if action != "state" and payload.get("policy_version") != REQUIRED_POLICY_VERSION:
        raise CommandRejected(
            f"policy_version must be {REQUIRED_POLICY_VERSION}"
        )
    if action == "state":
        # STATE is the transport-level recovery command.  It must remain
        # available after a mod error frame, whose payload intentionally has
        # no gameplay command list or decision binding.
        return "STATE", {"requested_target_id": "action:state", "resolved_target_id": "action:state"}

    if (state.get("phase") == "HAND_SELECT"
            and state.get("ready_for_command") is False
            and action != "wait"):
        raise CommandRejected("HAND_SELECT is not ready for another gameplay command")

    attempt_binding = _required_binding(payload, state)

    def receipt_fields(fields):
        return {**fields, **attempt_binding}

    if action == "choose":
        _require_command(state, "choose")
        requested = str(payload.get("option_id") or "")
        matches = [item for item in state.get("options") or [] if item["option_id"] == requested]
        if len(matches) != 1:
            raise CommandRejected("option_id does not resolve uniquely in current state")
        option = matches[0]
        target = option.get("target")
        target = target if isinstance(target, dict) else {}
        if (
            str(target.get("kind") or "").endswith("_unbound")
            or target.get("binding_status")
        ):
            raise CommandRejected(
                "bound option has no authoritative target binding"
            )
        supplied_index = payload.get("choice_index")
        if supplied_index is not None and int(supplied_index) != option["choice_index"]:
            raise CommandRejected("choice index does not match bound option_id")
        return f"CHOOSE {option['choice_index']}", receipt_fields({
            "requested_target_id": requested,
            "resolved_target_id": option["option_id"],
        })
    if action == "play":
        _require_command(state, "play")
        requested = str(payload.get("card_instance_id") or "")
        game = state.get("game_state") or {}
        combat = game.get("combat_state") or {}
        hand = combat.get("hand") or []
        matches = [(index, card) for index, card in enumerate(hand, 1) if card.get("card_instance_id") == requested]
        if len(matches) != 1:
            raise CommandRejected("card_instance_id does not resolve uniquely in current hand")
        index, card = matches[0]
        if not card.get("is_playable"):
            raise CommandRejected("bound card is not playable")
        command = f"PLAY {index}"
        fields = {"requested_target_id": requested, "resolved_target_id": card["card_instance_id"]}
        if card.get("has_target"):
            enemy_id = str(payload.get("enemy_instance_id") or "")
            enemies = combat.get("monsters") or []
            enemy_matches = [(i, enemy) for i, enemy in enumerate(enemies) if enemy.get("enemy_instance_id") == enemy_id]
            if len(enemy_matches) != 1:
                raise CommandRejected("enemy_instance_id does not resolve uniquely")
            enemy_index, enemy = enemy_matches[0]
            if enemy.get("is_gone") or enemy.get("half_dead"):
                raise CommandRejected("bound enemy is not a legal target")
            command += f" {enemy_index}"
            fields.update({"requested_enemy_id": enemy_id, "resolved_enemy_id": enemy["enemy_instance_id"]})
        elif payload.get("enemy_instance_id"):
            raise CommandRejected("non-targeted card must not include enemy_instance_id")
        return command, receipt_fields(fields)
    if action == "potion":
        _require_command(state, "potion")
        operation = str(payload.get("operation") or "").lower()
        if operation not in {"use", "discard"}:
            raise CommandRejected("potion operation must be use or discard")
        requested = str(payload.get("potion_instance_id") or "")
        potions = (state.get("game_state") or {}).get("potions") or []
        matches = [item for item in potions if item.get("potion_instance_id") == requested]
        if len(matches) != 1 or matches[0].get("id") == "Potion Slot":
            raise CommandRejected("potion_instance_id does not resolve uniquely")
        potion = matches[0]
        command = f"POTION {operation} {potion['slot']}"
        fields = {"requested_target_id": requested, "resolved_target_id": potion["potion_instance_id"]}
        if operation == "use" and potion.get("requires_target"):
            enemy_id = str(payload.get("enemy_instance_id") or "")
            enemies = (((state.get("game_state") or {}).get("combat_state") or {}).get("monsters") or [])
            enemy_matches = [(i, enemy) for i, enemy in enumerate(enemies) if enemy.get("enemy_instance_id") == enemy_id]
            if len(enemy_matches) != 1:
                raise CommandRejected("enemy_instance_id does not resolve uniquely")
            enemy_index, enemy = enemy_matches[0]
            if enemy.get("is_gone") or enemy.get("half_dead"):
                raise CommandRejected("bound enemy is not a legal target")
            command += f" {enemy_index}"
            fields.update({"requested_enemy_id": enemy_id, "resolved_enemy_id": enemy["enemy_instance_id"]})
        elif payload.get("enemy_instance_id"):
            raise CommandRejected(
                "non-targeted potion operation must not include enemy_instance_id"
            )
        return command, receipt_fields(fields)
    if action == "start":
        try:
            pre_run_binding.load_and_validate_start_payload(
                payload,
                state,
                selection_path=PENDING_SELECTION_PATH,
                history_path=RUN_HISTORY_PATH,
                manifest_path=FREEZE_MANIFEST_PATH,
                cohort_review_path=COHORT_REVIEW_PATH,
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CommandRejected(f"START selection binding rejected: {exc}") from exc
        _require_command(state, "start")
        player_class = str(payload.get("player_class") or "").upper()
        aliases = {"SILENT": "THE_SILENT", "THE_SILENT": "THE_SILENT", "IRONCLAD": "IRONCLAD", "DEFECT": "DEFECT"}
        if player_class not in aliases:
            raise CommandRejected("unsupported player_class")
        ascension = int(payload.get("ascension_level", 0))
        if not 0 <= ascension <= 20:
            raise CommandRejected("ascension_level out of bounds")
        requested = f"run:{aliases[player_class]}:a{ascension}:standard"
        if payload.get("target_id") != requested:
            raise CommandRejected("start target_id mismatch")
        return f"START {aliases[player_class]} {ascension}", receipt_fields({
            "requested_target_id": requested,
            "resolved_target_id": requested,
            "selection_digest": payload.get("selection_digest"),
        })
    if action == "resume":
        try:
            pre_run_binding.load_and_validate_resume_payload(
                payload,
                state,
                context_path=RUN_CONTEXT_PATH,
                manifest_path=FREEZE_MANIFEST_PATH,
            )
        except pre_run_binding.PreRunBindingError as exc:
            raise CommandRejected(f"RESUME attempt binding rejected: {exc}") from exc
        _require_command(state, "resume")
        player_class = str(payload.get("player_class") or "").upper()
        aliases = {"SILENT": "THE_SILENT", "THE_SILENT": "THE_SILENT", "IRONCLAD": "IRONCLAD", "DEFECT": "DEFECT"}
        if player_class not in aliases:
            raise CommandRejected("unsupported player_class")
        resolved_class = aliases[player_class]
        autosave_field = {
            "IRONCLAD": "ironclad_autosave_exists",
            "THE_SILENT": "silent_autosave_exists",
            "DEFECT": "defect_autosave_exists",
        }[resolved_class]
        if not state.get(autosave_field, False):
            raise CommandRejected(f"no authoritative autosave marker for {resolved_class}")
        requested = f"resume:{resolved_class}:autosave"
        if payload.get("target_id") != requested:
            raise CommandRejected("resume target_id mismatch")
        return f"RESUME {resolved_class}", receipt_fields({"requested_target_id": requested, "resolved_target_id": requested})
    if action in {"end", "proceed", "return"}:
        _require_command(state, action)
        target = f"action:{action}"
        if payload.get("target_id") != target:
            raise CommandRejected(f"{action} target_id mismatch")
        raw_action = action
        if action == "return":
            # ``return`` is the protocol's bound cancel/leave abstraction.
            # Emit the concrete command advertised by the current frame.
            # In particular, CARD_REWARD exposes SKIP; sending RETURN there
            # only reopens the same CARD reward from COMBAT_REWARD.
            available = {
                str(item).lower()
                for item in state.get("legal_actions") or []
            }
            raw_action = next(
                candidate
                for candidate in ("skip", "cancel", "return", "leave")
                if candidate in available
            )
        return raw_action.upper(), receipt_fields({"requested_target_id": target, "resolved_target_id": target})
    if action == "key":
        _require_command(state, "key")
        key_name = str(payload.get("key") or "").upper()
        # Keyboard input is deliberately limited to the one recovery key the
        # controller needs.  Arbitrary keys would bypass the target-specific
        # bindings used by normal gameplay actions.
        if key_name != "CANCEL":
            raise CommandRejected("only the CANCEL recovery key is supported")
        target = "key:CANCEL"
        if payload.get("target_id") != target:
            raise CommandRejected("key target_id mismatch")
        return "KEY CANCEL", receipt_fields({
            "requested_target_id": target,
            "resolved_target_id": target,
        })
    if action == "wait":
        _require_command(state, "wait")
        frames = int(payload.get("frames", 1))
        if not 0 <= frames <= 10000:
            raise CommandRejected("wait frames out of bounds")
        return f"WAIT {frames}", receipt_fields({"requested_target_id": "action:wait", "resolved_target_id": "action:wait"})
    raise CommandRejected(f"unsupported action: {action}")


def rejection_receipt(payload, state, error):
    return {
        "request_id": payload.get("id"),
        "success": False,
        "status": "rejected",
        "error": str(error),
        "requested_target_id": payload.get("option_id") or payload.get("card_instance_id") or payload.get("potion_instance_id") or payload.get("target_id"),
        "resolved_target_id": None,
        "accepted_state_seq": None,
        "result_state_seq": state.get("state_seq"),
        **_rejected_attempt_binding(payload, state),
    }


def combat_card_reward_count(state):
    """Return the authoritative number of CARD items on CombatRewardScreen.

    ``None`` means the frame does not expose the parent reward list.  Keeping
    that distinct from an empty list matters: zero CARD rewards is the exact
    completion condition for an ordinary reward, while missing data must not
    acknowledge anything.
    """

    game = state.get("game_state") or {}
    screen = game.get("screen_state") or {}
    if "rewards" not in screen:
        return None
    return sum(
        1
        for reward in screen.get("rewards") or []
        if str(reward.get("reward_type") or "").upper() == "CARD"
    )


def opened_card_reward_parent_count(payload, state):
    """Capture the parent CARD count when a bound reward is opened."""

    if str(payload.get("action") or "").lower() != "choose":
        return None
    if state.get("phase") != "COMBAT_REWARD":
        return None
    requested = str(payload.get("option_id") or "")
    selected = next(
        (
            option
            for option in state.get("options") or []
            if option.get("option_id") == requested
        ),
        None,
    )
    target = (selected or {}).get("target") or {}
    reward = target.get("reward") or {}
    if target.get("kind") != "reward" or str(reward.get("reward_type") or "").upper() != "CARD":
        return None
    count = combat_card_reward_count(state)
    return count if count is not None and count > 0 else None


def selected_bound_option(payload, state):
    """Return the exact option already validated for a choose payload."""

    if str(payload.get("action") or "").lower() != "choose":
        return None
    requested = str(payload.get("option_id") or "")
    return next(
        (
            option
            for option in state.get("options") or []
            if option.get("option_id") == requested
        ),
        None,
    )


def event_option_label(option):
    """Return the display label for a serialized event option."""

    if not isinstance(option, dict):
        return ""
    target = option.get("target") or {}
    if not isinstance(target, dict):
        target = {}
    return str(
        option.get("label")
        or option.get("text")
        or target.get("label")
        or ""
    ).strip()


def wheel_event_option_labels(state):
    """Return visible Wheel options, or None for a non-Wheel frame."""

    if state.get("phase") != "EVENT":
        return None
    game = state.get("game_state") or {}
    screen = game.get("screen_state") or {}
    if normalized_game_id(screen.get("event_id")) != "wheelofchange":
        return None
    return [
        normalized_game_id(event_option_label(option))
        for option in state.get("options") or []
        if event_option_label(option)
    ]


def event_choice_reward_card_id(payload, state):
    """Return the concrete card preview bound to an event reward button."""

    option = selected_bound_option(payload, state)
    target = (option or {}).get("target") or {}
    card = target.get("card") or {}
    return normalized_game_id(card.get("id"))


def deck_card_count(game, card_id):
    deck = game.get("deck")
    if not isinstance(deck, list):
        return None
    card_ids = card_id if isinstance(card_id, (list, tuple, set)) else (card_id,)
    wanted = {normalized_game_id(value) for value in card_ids}
    return sum(
        1
        for card in deck
        if normalized_game_id(card.get("id")) in wanted
    )


def event_effect_signature(state):
    """Stable macro state used to prove same-decision event progress."""

    game = state.get("game_state") or {}
    deck = game.get("deck") or []
    return (
        game.get("current_hp"),
        game.get("max_hp"),
        game.get("gold"),
        tuple(
            (card.get("id"), card.get("upgrades"), card.get("uuid"))
            for card in deck
        ),
        tuple(relic.get("id") for relic in game.get("relics") or []),
        tuple(potion.get("id") for potion in game.get("potions") or []),
    )


def event_choice_postcondition(payload, state):
    """Capture exact delayed rewards for event choices that need proof.

    Event buttons can advance to a Leave decision while visual obtain effects
    are still adding rewards to the master deck.  Bind the expected mutation
    at command acceptance so a later screen transition cannot masquerade as
    successful settlement.
    """

    if state.get("phase") != "EVENT":
        return None
    option = selected_bound_option(payload, state)
    target = (option or {}).get("target") or {}
    event_id = target.get("event_id") or (
        ((state.get("game_state") or {}).get("screen_state") or {}).get("event_id")
    )
    choice_index = (option or {}).get("choice_index")
    game = state.get("game_state") or {}
    deck = game.get("deck")
    if not isinstance(deck, list):
        # Never install an exact delta from a transient event frame that omits
        # the master deck. A reward-specific bounded fallback below will wait
        # for the delayed effect without inventing a zero-card baseline.
        return None
    normalized_event = normalized_game_id(event_id)
    reward_card_id = event_choice_reward_card_id(payload, state)
    if (
        normalized_event == "ghosts"
        and choice_index == 0
        and reward_card_id in {"ghostly", "apparition"}
    ):
        ascension_level = int(game.get("ascension_level", 0) or 0)
        return {
            "kind": "deck_card_gain",
            "event_id": event_id,
            # Slay the Spire's internal card ID is Ghostly even though the
            # displayed English name is Apparition. Accept both for fixtures
            # and mod variants, but bind live proof to the actual ID.
            "card_id": ("Ghostly", "Apparition"),
            "expected_delta": 3 if ascension_level >= 15 else 5,
            "deck_size_before": len(deck) if isinstance(deck, list) else None,
            "card_count_before": deck_card_count(
                game, ("Ghostly", "Apparition")
            ),
        }
    if (
        normalized_event == "vampires"
        and choice_index in {0, 1}
        and reward_card_id == "bite"
    ):
        strike_count = deck_card_count(
            game, ("Strike_R", "Strike_G", "Strike_B")
        )
        return {
            "kind": "deck_card_gain",
            "event_id": event_id,
            "card_id": "Bite",
            "expected_delta": 5,
            "expected_deck_delta": (
                None if strike_count is None else 5 - strike_count
            ),
            "deck_size_before": len(deck) if isinstance(deck, list) else None,
            "card_count_before": deck_card_count(game, "Bite"),
        }
    return None


def is_hidden_match_card_label(label):
    token = normalized_game_id(label)
    return token.startswith("card") and token[4:].isdigit()


def match_game_choice_context(payload, state, first_flip_card_id=None):
    """Describe a Match and Keep board click without breaking flip receipts.

    Pickable cards are serialized in board order.  A first flip temporarily
    removes one card from that list, so an odd option count authoritatively
    identifies the following click as the second flip.  Known cards use their
    card ID as the label; unrevealed cards use ``cardN`` placeholders.
    """

    game = state.get("game_state") or {}
    screen = game.get("screen_state") or {}
    if (
        state.get("phase") != "EVENT"
        or not is_match_game_event(screen.get("event_id"))
    ):
        return None
    options = state.get("options") or []
    # Setup/continue/leave surfaces have a single dialog option.  The board
    # itself starts with twelve cards and remains a multi-option surface.
    if len(options) <= 1:
        return None
    selected = selected_bound_option(payload, state)
    if selected is None:
        return None
    target = selected.get("target") or {}
    label = target.get("label") or selected.get("label")
    chosen_card_id = None if is_hidden_match_card_label(label) else label
    second_flip = len(options) % 2 == 1
    known_match = (
        second_flip
        and chosen_card_id is not None
        and first_flip_card_id is not None
        and normalized_game_id(chosen_card_id)
        == normalized_game_id(first_flip_card_id)
    )
    deck = game.get("deck")
    return {
        "second_flip": second_flip,
        "chosen_card_id": chosen_card_id,
        "known_match": known_match,
        "deck_size_before": len(deck) if isinstance(deck, list) else None,
    }


def match_game_reward_postcondition(context, state):
    if not (context or {}).get("known_match"):
        return None
    card_id = context.get("chosen_card_id")
    game = state.get("game_state") or {}
    return {
        "kind": "deck_card_gain",
        "event_id": (
            ((game.get("screen_state") or {}).get("event_id"))
        ),
        "card_id": card_id,
        "expected_delta": 1,
        "deck_size_before": context.get("deck_size_before"),
        "card_count_before": deck_card_count(game, card_id),
    }


def event_postcondition_status(postcondition, state):
    """Return satisfied, pending, or mismatch for a captured event reward."""

    if (postcondition or {}).get("kind") != "deck_card_gain":
        return "mismatch"
    game = state.get("game_state") or {}
    deck = game.get("deck")
    deck_size_before = postcondition.get("deck_size_before")
    card_count_before = postcondition.get("card_count_before")
    if (
        not isinstance(deck, list)
        or not isinstance(deck_size_before, int)
        or not isinstance(card_count_before, int)
    ):
        # A transient frame may omit a usable deck snapshot.  Keep waiting so
        # absence of evidence can never acknowledge the reward.
        return "pending"

    expected_delta = int(postcondition.get("expected_delta") or 0)
    card_count = deck_card_count(game, postcondition.get("card_id"))
    if card_count is None:
        return "pending"
    deck_delta = len(deck) - deck_size_before
    card_delta = card_count - card_count_before
    expected_deck_delta = postcondition.get(
        "expected_deck_delta", expected_delta
    )
    if deck_delta == expected_deck_delta and card_delta == expected_delta:
        return "satisfied"
    if (
        expected_deck_delta == expected_delta
        and 0 <= deck_delta <= expected_delta
        and deck_delta == card_delta
    ):
        # ShowCardAndObtainEffect can publish each of the five additions on a
        # separate frame.  Every valid prefix remains pending.
        return "pending"
    if (
        expected_deck_delta != expected_delta
        and 0 <= card_delta <= expected_delta
        and expected_deck_delta - expected_delta
        <= deck_delta
        <= expected_delta
    ):
        # Replacement events may remove the old cards before adding the new
        # ones or vice versa.  Both monotonic prefixes are valid; only the
        # exact final card and net deck deltas acknowledge completion.
        return "pending"
    return "mismatch"


def event_choice_settlement_outcome(pending, state):
    """Apply a short generic barrier plus any exact event postcondition."""

    action = str((pending.get("payload") or {}).get("action") or "").lower()
    if action != "choose" or pending.get("accepted_phase") != "EVENT":
        return None
    accepted_event_id = normalized_game_id(pending.get("accepted_event_id"))
    if accepted_event_id == "wheelofchange":
        # The Wheel changes decision_id as soon as the intro button starts the
        # animation, but its singleton "spin" option remains visible until
        # the result/Leave dialog is actually ready. Treat that surface as an
        # in-flight transaction so the controller cannot submit Spin again.
        current_event_id = normalized_game_id(
            (
                ((state.get("game_state") or {}).get("screen_state") or {})
                .get("event_id")
            )
        )
        if state.get("phase") != "EVENT":
            return "complete"
        if current_event_id and current_event_id != "wheelofchange":
            return "complete"
        accepted_signature = pending.get("accepted_event_effect_signature")
        if (
            accepted_signature is not None
            and event_effect_signature(state) != accepted_signature
        ):
            return "complete"
        labels = wheel_event_option_labels(state)
        accepted_label = normalized_game_id(
            pending.get("accepted_event_option_label")
        )
        in_progress = not labels or all(
            label == "spin" or (accepted_label and label == accepted_label)
            for label in labels
        )
        if in_progress:
            if pending.get("settle_frames", 0) < WHEEL_SETTLE_TIMEOUT_FRAMES:
                return "settle"
            return "event_settlement_timeout"
        return "complete"
    match_event = is_match_game_event(pending.get("accepted_event_id"))
    postcondition = pending.get("event_postcondition")
    if postcondition is not None:
        status = event_postcondition_status(postcondition, state)
        if status == "mismatch":
            return "event_postcondition_mismatch"
        if status != "satisfied":
            if pending.get("settle_frames", 0) >= EVENT_SETTLE_TIMEOUT_FRAMES:
                return "event_settlement_timeout"
            return "settle"

    if match_event:
        match_context = pending.get("match_game_context") or {}
        if match_context.get("second_flip"):
            game = state.get("game_state") or {}
            deck = game.get("deck")
            before = match_context.get("deck_size_before")
            if isinstance(deck, list) and isinstance(before, int):
                deck_delta = len(deck) - before
                if deck_delta == 1:
                    if pending.get("settle_frames", 0) < EVENT_SETTLE_MIN_FRAMES:
                        return "settle"
                    return "complete"
                if deck_delta != 0:
                    return "event_postcondition_mismatch"
            # An unknown pair can legitimately be a mismatch, so it cannot
            # demand +1 forever.  Keep every second flip pending for a full
            # window, however: the first ready frame may still show the board
            # before it changes to Leave, and a delayed matched-card obtain
            # must not escape the command that caused it.
            if pending.get("settle_frames", 0) < MATCH_UNKNOWN_PAIR_SETTLE_FRAMES:
                return "settle"
            return "complete"

        # First flips and ordinary non-terminal second flips retain the Match
        # patch's immediate same-decision-ID acknowledgement.  A known pair
        # already installed an exact +1 postcondition above and cannot reach
        # this branch before its reward settles.
        return None

    if (
        postcondition is None
        and (
            (
                normalized_game_id(pending.get("accepted_event_id"))
                == "ghosts"
                and pending.get("accepted_event_reward_card_id")
                in {"ghostly", "apparition"}
            )
            or (
                normalized_game_id(pending.get("accepted_event_id"))
                == "vampires"
                and pending.get("accepted_event_reward_card_id") == "bite"
            )
        )
    ):
        # Some event-transition frames omit the master deck, so an exact
        # before-count cannot be captured safely.  These two multi-card
        # rewards still need a longer transaction barrier than ordinary
        # events.  At the boundary require either a macro-state mutation or a
        # changed decision surface; otherwise fail closed without replaying.
        if pending.get("settle_frames", 0) < EVENT_REWARD_FALLBACK_FRAMES:
            return "settle"
        signature_changed = (
            pending.get("accepted_event_effect_signature") is not None
            and event_effect_signature(state)
            != pending.get("accepted_event_effect_signature")
        )
        decision_changed = (
            state.get("decision_id") != pending.get("accepted_decision_id")
        )
        return (
            "complete"
            if signature_changed or decision_changed
            else "event_settlement_timeout"
        )

    # Even events without a known exact delta receive a couple of real game
    # updates after their first ready frame.  This covers the common transient
    # window where an event has replaced its buttons but queued side effects
    # have not yet reached serialized state.
    if pending.get("settle_frames", 0) < EVENT_SETTLE_MIN_FRAMES:
        return "settle"
    accepted_signature = pending.get("accepted_event_effect_signature")
    signature_changed = (
        accepted_signature is not None
        and event_effect_signature(state) != accepted_signature
    )
    same_decision = (
        state.get("decision_id") == pending.get("accepted_decision_id")
    )
    if same_decision:
        if (
            normalized_game_id(pending.get("accepted_event_id"))
            == "wheelofchange"
        ):
            # The wheel's singleton Spin option remains visible during its
            # animation and its result may not change the macro signature.
            # Never acknowledge that unchanged surface: keep advancing the
            # game one frame at a time until a new decision/effect appears.
            if signature_changed:
                return "complete"
            if pending.get("settle_frames", 0) < WHEEL_SETTLE_TIMEOUT_FRAMES:
                return "settle"
            return "event_settlement_timeout"
        if pending.get("settle_frames", 0) < EVENT_UNCHANGED_SETTLE_FRAMES:
            return "settle"
        if signature_changed:
            return "complete"
        return "event_settlement_timeout"
    if signature_changed:
        return "complete"
    return None


def proceed_settle_timeout_frames(state):
    """Return the terminal settle budget for the current GAME_OVER frame.

    Normal deaths should fail fast when the game never leaves the terminal
    screen.  A Heart victory is different: the base game runs the victory and
    unlock transition before startOver makes the main menu visible, and
    that transition can exceed the historical two-second budget.
    """

    game = (state or {}).get("game_state") or {}
    screen = game.get("screen_state") or {}
    if (
        game.get("run_victory") is True
        or game.get("room_type") == "TrueVictoryRoom"
        or screen.get("victory") is True
    ):
        return PROCEED_VICTORY_SETTLE_TIMEOUT_FRAMES
    return PROCEED_SETTLE_TIMEOUT_FRAMES


def pending_frame_outcome(pending, state, raw):
    """Classify a mod frame received after an accepted command.

    A frame is not an acknowledgement merely because it arrived later.  It
    must expose either a stable changed decision surface or an authoritative,
    target-specific selection effect. Selection completion does not authorize
    the next gameplay action until its own readiness check passes.
    """

    if "error" in raw:
        # CommunicationMod can publish an error from the preceding UI command
        # after the bridge has already accepted a newer command.  Do not bind
        # an explicit "Invalid command: X" frame to pending command Y; request
        # a fresh state and keep waiting for Y's authoritative transition.
        error = str(raw.get("error") or "")
        marker = "invalid command:"
        invalid_verb = None
        if marker in error.lower():
            remainder = error.lower().split(marker, 1)[1].strip()
            invalid_parts = remainder.split(".", 1)[0].split(None, 1)
            if invalid_parts:
                invalid_verb = invalid_parts[0]
        pending_verb = str(pending.get("raw_command") or "").strip()
        pending_parts = pending_verb.split(None, 1)
        pending_verb = pending_parts[0].lower() if pending_parts else ""
        if invalid_verb and pending_verb and invalid_verb != pending_verb:
            return "stale_error"
        return "complete"
    action = str((pending.get("payload") or {}).get("action") or "").lower()
    # A PROCEED accepted from the terminal screen is different from a combat
    # action that caused death. CommunicationMod can publish GAME_OVER after a
    # combat HAND_SELECT -> PROCEED confirmation when the enemy's queued damage
    # resolves. That combat receipt is complete on the authoritative GAME_OVER
    # frame; only a PROCEED that was accepted while already on GAME_OVER must
    # keep waiting for the actual MAIN_MENU transition.
    if str((raw.get("game_state") or {}).get("screen_type") or "").upper() == "GAME_OVER":
        if (
            action == "proceed"
            and pending.get("accepted_phase") == "GAME_OVER"
            and raw.get("in_game") is True
        ):
            timeout_frames = pending.get("proceed_settle_timeout_frames")
            if type(timeout_frames) is not int or timeout_frames <= 0:
                timeout_frames = proceed_settle_timeout_frames(raw)
            if pending.get("settle_frames", 0) >= timeout_frames:
                return "proceed_settlement_timeout"
            return "proceed_settle"
        return "complete"
    if (
        action == "choose"
        and pending.get("accepted_phase") == "MAP"
        and state.get("phase") != "MAP"
        and state.get("decision_id") != pending.get("accepted_decision_id")
    ):
        # A route choice can enter combat while the next room is still being
        # initialized. CommunicationMod then publishes a new, non-ready
        # decision surface (for example a Gambling Chip HAND_SELECT) and
        # stops emitting frames until that surface receives its first input.
        # The changed phase/decision already proves the MAP write took effect;
        # release its receipt before the non-ready gate so the controller can
        # bind and dispatch the new surface instead of deadlocking recovery.
        return "complete"
    if action == "state":
        # STATE is a read-only transport recovery request.  A HAND_SELECT
        # frame can be authoritative while ready_for_command is false (for
        # example, after the exact card was selected and the confirm button
        # is still settling).  Requiring command readiness here deadlocks the
        # recovery request that produced this very frame, so complete it
        # before the gameplay-readiness gate.  The controller still verifies
        # the original state-changing action from its exact postcondition and
        # never replays an ambiguous write.
        return "complete"
    if has_uninitialized_combat_intent(raw):
        return "unstable"
    if action == "choose" and pending.get("accepted_phase") in {"GRID", "HAND_SELECT"}:
        selected_id = pending.get("accepted_card_instance_id")
        screen = (state.get("game_state") or {}).get("screen_state") or {}
        selected_cards = screen.get("selected_cards") or screen.get("selected") or []
        if selected_id and any(
            card.get("card_instance_id") == selected_id
            for card in selected_cards
            if isinstance(card, dict)
        ):
            # Selection is an exact postcondition, independent of whether the
            # next click/confirm is ready. Holding this receipt until readiness
            # deadlocks recovery STATE behind the completed write. The controller
            # must gate its NEXT gameplay action on a fresh ready frame.
            return "complete"
        # A HAND_SELECT CHOOSE can be accepted by CommunicationMod before
        # the selected card is reflected in the next serialized frame.  In
        # particular, the mod may publish several ready frames from the same
        # selection surface while the Java action queue is still applying the
        # click.  Do not apply the generic three-frame "unchanged" failure in
        # that window: the exact target is still visible in the hand, so keep
        # the receipt pending until the selection appears (or the bounded
        # settlement timeout proves that it did not).
        if pending.get("accepted_phase") == "HAND_SELECT" and selected_id:
            hand = screen.get("hand") or []
            target_still_visible = any(
                card.get("card_instance_id") == selected_id
                for card in hand
                if isinstance(card, dict)
            )
            if target_still_visible:
                if pending.get("settle_frames", 0) >= HAND_SELECT_CHOOSE_SETTLE_TIMEOUT_FRAMES:
                    return "hand_select_choose_settlement_timeout"
                return "settle"
    if not raw.get("ready_for_command", False):
        return "wait"
    if action == "wait":
        return "complete"
    if (
        action == "play"
        and pending.get("accepted_phase", "").startswith("COMBAT_TURN_")
        and state.get("phase", "").startswith("COMBAT_TURN_")
        and state.get("decision_id") != pending.get("accepted_decision_id")
    ):
        accepted_turn = pending.get("accepted_turn")
        current_turn = (
            ((state.get("game_state") or {}).get("combat_state") or {}).get("turn")
        )
        same_turn_surface = state.get("phase") == pending.get("accepted_phase")
        backwards_turn_frame = (
            type(accepted_turn) is int
            and type(current_turn) is int
            and current_turn <= accepted_turn
        )
        # The first changed combat frame proves that the card was accepted,
        # but it may still be the tail of the previous card's action queue.
        # Advance one read-only frame before releasing the receipt so a
        # subsequent PLAY cannot race that queue.  Some card resolutions
        # briefly publish a lower COMBAT_TURN_N (for example TURN_3 -> TURN_1)
        # while the same fight is settling; treat that backwards frame as the
        # same transient queue, not as a real combat boundary.
        if same_turn_surface or backwards_turn_frame:
            if pending.get("settle_frames", 0) < COMBAT_PLAY_SETTLE_FRAMES:
                return "settle"
        return "complete"
    parent_count = pending.get("parent_card_reward_count_before")
    if (
        action in {"return", "choose"}
        and pending.get("accepted_phase") == "CARD_REWARD"
        and parent_count is not None
    ):
        # Closing CardRewardScreen can expose CombatRewardScreen before its
        # completed RewardItem is removed.  A fixed frame delay is not proof
        # of consumption, so keep the receipt pending until the authoritative
        # parent CARD count decreases by exactly one.  This also preserves
        # Prayer Wheel semantics: 2 -> 1 succeeds and leaves the second reward
        # available; its later close must independently prove 1 -> 0.
        if state.get("phase") == "CARD_REWARD":
            if pending.get("settle_frames", 0) >= 120:
                return "reward_cleanup_timeout"
            return "settle"
        if state.get("phase") != "COMBAT_REWARD":
            return "reward_count_mismatch"
        current_count = combat_card_reward_count(state)
        expected_count = int(parent_count) - 1
        if current_count == expected_count:
            return "complete"
        if current_count == int(parent_count) or current_count is None:
            if pending.get("settle_frames", 0) >= 120:
                return "reward_cleanup_timeout"
            return "settle"
        return "reward_count_mismatch"
    if action == "choose" and pending.get("accepted_phase") == "REST":
        game = state.get("game_state") or {}
        # A campfire option is an accepted write even while its visual effect
        # is still queued.  A changed phase or completed room is the exact
        # postcondition; unchanged REST frames receive a bounded settle window
        # instead of the generic three-frame failure.
        if state.get("phase") != "REST":
            return "complete"
        accepted_room_phase = pending.get("accepted_room_phase")
        if (
            accepted_room_phase is not None
            and game.get("room_phase") != accepted_room_phase
        ):
            return "complete"
        if pending.get("settle_frames", 0) < REST_SETTLE_TIMEOUT_FRAMES:
            return "settle"
        return "rest_settlement_timeout"
    if action == "end":
        game = state.get("game_state") or {}
        # Well Laid Plans opens its end-of-turn retain prompt before the
        # combat turn counter advances.  That HAND_SELECT frame is an
        # authoritative transition caused by END even when its decision ID is
        # unchanged from the preceding combat frame.
        if (
            pending.get("accepted_room_phase") == "COMBAT"
            and state.get("phase") == "HAND_SELECT"
        ):
            return "complete"
        accepted_turn = pending.get("accepted_turn")
        current_turn = ((game.get("combat_state") or {}).get("turn"))
        if (
            pending.get("accepted_room_phase") == "COMBAT"
            and game.get("room_phase") == "COMBAT"
            and game.get("screen_type") != "GAME_OVER"
            and accepted_turn is not None
            and (current_turn is None or current_turn <= accepted_turn)
        ):
            # A delayed frame from the preceding card can alter the decision
            # surface before END has actually advanced the turn.  Do not
            # attribute that frame to END merely because decision_id changed.
            return "unchanged"
    event_outcome = event_choice_settlement_outcome(pending, state)
    if event_outcome is not None:
        return event_outcome
    if action == "choose" and is_match_game_event(pending.get("accepted_event_id")):
        # A previously revealed Match and Keep card already exposes its ID in
        # choice_list. Flipping it again therefore produces the same decision
        # ID even though the exact click succeeded. The mod publishes a ready
        # frame from its first-flip patch, which is the authoritative receipt.
        return "complete"
    if (
        action == "proceed"
        and pending.get("accepted_phase") == "HAND_SELECT"
        and state.get("phase") == "HAND_SELECT"
    ):
        # A confirm can open another HAND_SELECT surface (for example a
        # chained discard/exhaust effect) without leaving the phase.  The
        # changed decision binding is the authoritative acknowledgement for
        # that accepted PROCEED; waiting for the phase to change would hold
        # the receipt until the controller times out.
        if state.get("decision_id") != pending.get("accepted_decision_id"):
            return "complete"
        if (
            pending.get("settle_frames", 0)
            < HAND_SELECT_PROCEED_SETTLE_TIMEOUT_FRAMES
        ):
            return "settle"
        return "hand_select_proceed_settlement_timeout"
    if state.get("decision_id") != pending.get("accepted_decision_id"):
        return "complete"
    return "unchanged"


def hand_select_dispatch_frame(pending, state):
    """Prepare a HAND_SELECT CHOOSE only after its card list is stable.

    CommunicationMod serializes ``choice_list`` and the hand-select screen at
    a game-update boundary, while the underlying Java hand can still finish a
    prior selection on the following update.  The old bridge emitted the raw
    CHOOSE immediately after accepting the controller payload, so a valid
    bound option could become an out-of-range Java index.  Keep the accepted
    receipt pending, rebind the same card instance against the newest frame,
    and dispatch only after two identical ready frames.

    Returns one of ``dispatch``, ``settle``, ``wait``, or ``error``.  The
    caller owns the bounded timeout and receipt emission.
    """

    if not pending.get("dispatch_pending"):
        return None
    if (
        pending.get("accepted_phase") != "HAND_SELECT"
        or str((pending.get("payload") or {}).get("action") or "").lower()
        != "choose"
    ):
        return "error"

    if state.get("phase") != "HAND_SELECT":
        pending["dispatch_error"] = "HAND_SELECT surface changed before CHOOSE dispatch"
        return "error"
    decision_changed = (
        state.get("decision_id") != pending.get("accepted_decision_id")
    )
    if decision_changed:
        # END/CONFIRM can publish a transient HAND_SELECT frame first and
        # rebuild the same card-choice surface on the next update.  The
        # decision ID is therefore allowed to advance while this bridge-owned
        # dispatch is still pending, but only after the exact card instance
        # remains bound below.  Reset the stability barrier so CHOOSE is never
        # emitted from the first frame of the refreshed surface.
        pending["accepted_decision_id"] = state.get("decision_id")
        pending["dispatch_signature"] = None
        pending["dispatch_stable_frames"] = 0
    if not state.get("ready_for_command", False):
        return "wait"

    payload = pending.get("payload") or {}
    option_id = payload.get("option_id")
    options = state.get("options") or []
    bound_card_id = pending.get("accepted_card_instance_id")
    matches = [
        option for option in options
        if isinstance(option, dict) and option.get("option_id") == option_id
    ]
    if bound_card_id:
        # Option IDs are derived from the serialized choice descriptor and
        # may be regenerated when the Java selection screen refreshes.  The
        # card instance UUID is the stable authority for this pending write;
        # rebind to its refreshed option when necessary and reject a target
        # substitution even if the old option ID happens to be reused.
        bound_matches = [
            option for option in options
            if isinstance(option, dict)
            and (option.get("target") or {}).get("card_instance_id")
            == bound_card_id
        ]
        if len(bound_matches) == 1:
            matches = bound_matches
    if len(matches) != 1:
        hand = ((state.get("game_state") or {}).get("screen_state") or {}).get(
            "hand"
        ) or []
        target_still_visible = bool(
            bound_card_id
            and any(
                isinstance(card, dict)
                and card.get("card_instance_id") == bound_card_id
                for card in hand
            )
        )
        if (
            target_still_visible
            and pending.get("dispatch_frames", 0)
            < HAND_SELECT_PRE_DISPATCH_TIMEOUT_FRAMES
        ):
            # A decision refresh can briefly omit choice_list while the Java
            # screen is rebuilt.  Keep probing instead of failing a valid
            # selection; the bounded timeout below still fails closed when
            # the exact target never returns.
            return "wait"
        pending["dispatch_error"] = (
            "bound HAND_SELECT option disappeared before CHOOSE dispatch"
        )
        return "error"
    option = matches[0]
    target = option.get("target") or {}
    target_card_id = target.get("card_instance_id")
    if not isinstance(target_card_id, str) or not target_card_id:
        pending["dispatch_error"] = (
            "bound HAND_SELECT option has no card instance before CHOOSE dispatch"
        )
        return "error"

    screen = (state.get("game_state") or {}).get("screen_state") or {}
    hand = screen.get("hand") or []
    hand_ids = tuple(
        card.get("card_instance_id")
        for card in hand
        if isinstance(card, dict)
    )
    if target_card_id not in hand_ids:
        pending["dispatch_error"] = (
            "bound HAND_SELECT card is no longer visible before CHOOSE dispatch"
        )
        return "error"
    # The raw mod command indexes the live player hand.  Rebind against the
    # latest serialized hand rather than trusting an older choice_index.
    live_index = hand_ids.index(target_card_id)
    selected = screen.get("selected") or screen.get("selected_cards") or []
    selected_ids = tuple(
        card.get("card_instance_id")
        for card in selected
        if isinstance(card, dict)
    )
    signature = (
        state.get("decision_id"),
        hand_ids,
        selected_ids,
        tuple(state.get("available_commands") or []),
    )
    if signature != pending.get("dispatch_signature"):
        pending["dispatch_signature"] = signature
        pending["dispatch_stable_frames"] = 1
        return "settle"
    pending["dispatch_stable_frames"] = int(
        pending.get("dispatch_stable_frames", 0) or 0
    ) + 1
    if pending["dispatch_stable_frames"] < HAND_SELECT_PRE_DISPATCH_STABLE_FRAMES:
        return "settle"

    pending["dispatch_pending"] = False
    pending["accepted_card_instance_id"] = target_card_id
    pending["raw_command"] = f"CHOOSE {live_index}"
    return "dispatch"


def read_initial_sequence():
    sequences = [0]
    try:
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        value = meta.get("sequence") if isinstance(meta, dict) else None
        if type(value) is int and value >= 0:
            sequences.append(value)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        value = state.get("state_seq") if isinstance(state, dict) else None
        if type(value) is int and value >= 0:
            sequences.append(value)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return max(sequences)


def main():
    instance_token = claim_bridge_instance()
    parent_pid = os.getppid()
    output_lock = threading.Lock()
    pending_ref = {"active": False}
    wake_stop = threading.Event()
    wake_thread = threading.Thread(
        target=_state_command_wakeup,
        args=(COMMAND_PATH,),
        kwargs={
            "pending_ref": pending_ref,
            "stop_event": wake_stop,
            "output_lock": output_lock,
        },
        name="state-command-wakeup",
        daemon=True,
    )
    wake_thread.start()
    try:
        for path in (COMMAND_PATH, RECEIPT_PATH):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        sequence = read_initial_sequence()
        pending = None
        current_state = None
        active_choice_followup_context = None
        active_card_reward_parent_count = None
        match_first_flip_card_id = None
        _emit_command("ready", output_lock)

        for line in sys.stdin:
            if not bridge_instance_is_current(instance_token) or not parent_process_is_alive(parent_pid):
                return
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            sequence += 1
            frame_followup_context = (
                pending.get("accepted_followup_context")
                if isinstance(pending, dict)
                and pending.get("accepted_followup_context") is not None
                else active_choice_followup_context
            )
            current_state = enrich_state(
                raw, sequence,
                parent_choice_context=frame_followup_context,
            )
            current_event_id = (
                (((current_state.get("game_state") or {}).get("screen_state") or {}).get("event_id"))
            )
            if not is_match_game_event(current_event_id):
                match_first_flip_card_id = None
            atomic_write_json(STATE_PATH, current_state)
            atomic_write_json(META_PATH, {"sequence": sequence, "received_at": time.time(), "protocol_version": PROTOCOL_VERSION})

            # A HAND_SELECT CHOOSE is accepted by the bridge before the raw
            # Java command is dispatched.  Hold that write behind a fresh
            # state probe until the exact card instance and live hand order
            # are stable for two ready frames.  This closes the narrow race
            # where CommunicationMod serializes a two-card hand just before
            # the Java hand has finished moving one card, which otherwise
            # turns a valid bound index into an ``Index ... out of bounds``
            # error inside the game.
            if pending is not None and pending.get("dispatch_pending"):
                if "error" in raw:
                    pending["dispatch_frames"] += 1
                    if pending["dispatch_frames"] >= HAND_SELECT_PRE_DISPATCH_TIMEOUT_FRAMES:
                        receipt = dict(pending["receipt"])
                        receipt.update({
                            "success": False,
                            "status": "failed",
                            "error": "HAND_SELECT pre-dispatch state probe remained in error",
                            "result_state_seq": sequence,
                        })
                        atomic_write_json(RECEIPT_PATH, receipt)
                        pending = None
                        pending_ref["active"] = False
                    else:
                        _emit_command("STATE", output_lock)
                    continue
                dispatch_outcome = hand_select_dispatch_frame(
                    pending, current_state
                )
                pending["dispatch_frames"] += 1
                if dispatch_outcome == "dispatch":
                    _emit_command(pending["raw_command"], output_lock)
                    continue
                if dispatch_outcome == "wait":
                    if pending["dispatch_frames"] >= HAND_SELECT_PRE_DISPATCH_TIMEOUT_FRAMES:
                        receipt = dict(pending["receipt"])
                        receipt.update({
                            "success": False,
                            "status": "failed",
                            "error": "HAND_SELECT pre-dispatch frame was not command-ready",
                            "result_state_seq": sequence,
                        })
                        atomic_write_json(RECEIPT_PATH, receipt)
                        pending = None
                        pending_ref["active"] = False
                    else:
                        _emit_command("STATE", output_lock)
                    continue
                if dispatch_outcome == "error":
                    receipt = dict(pending["receipt"])
                    receipt.update({
                        "success": False,
                        "status": "failed",
                        "error": pending.get(
                            "dispatch_error",
                            "HAND_SELECT target changed before CHOOSE dispatch",
                        ),
                        "result_state_seq": sequence,
                    })
                    atomic_write_json(RECEIPT_PATH, receipt)
                    pending = None
                    pending_ref["active"] = False
                    continue
                if pending["dispatch_frames"] >= HAND_SELECT_PRE_DISPATCH_TIMEOUT_FRAMES:
                    receipt = dict(pending["receipt"])
                    receipt.update({
                        "success": False,
                        "status": "failed",
                        "error": "HAND_SELECT pre-dispatch stabilization timed out",
                        "result_state_seq": sequence,
                    })
                    atomic_write_json(RECEIPT_PATH, receipt)
                    pending = None
                    pending_ref["active"] = False
                else:
                    _emit_command("STATE", output_lock)
                continue

            if pending is not None:
                outcome = pending_frame_outcome(pending, current_state, raw)
                if outcome == "stale_error":
                    pending["stale_error_frames"] += 1
                    if pending["stale_error_frames"] >= 3:
                        receipt = dict(pending["receipt"])
                        receipt.update(
                            {
                                "success": False,
                                "status": "failed",
                                "error": (
                                    "three unrelated delayed command errors "
                                    "arrived while awaiting the accepted action"
                                ),
                                "result_state_seq": sequence,
                            }
                        )
                        atomic_write_json(RECEIPT_PATH, receipt)
                        pending = None
                        pending_ref["active"] = False
                    else:
                        _emit_command("STATE", output_lock)
                    continue
                if outcome == "unstable":
                    pending["unstable_frames"] += 1
                    if pending["unstable_frames"] >= 120:
                        receipt = dict(pending["receipt"])
                        receipt.update(
                            {
                                "success": False,
                                "status": "failed",
                                "error": "combat intents did not initialize within 120 frames",
                                "result_state_seq": sequence,
                            }
                        )
                        atomic_write_json(RECEIPT_PATH, receipt)
                        pending = None
                        pending_ref["active"] = False
                    else:
                        # Advance one game frame and keep the original command
                        # receipt pending until a genuine decision state exists.
                        _emit_command("WAIT 1", output_lock)
                    continue
                if outcome == "settle":
                    pending["settle_frames"] += 1
                    _emit_command("WAIT 1", output_lock)
                    continue
                if outcome == "proceed_settle":
                    pending["settle_frames"] += 1
                    # STATE is read-only and is accepted on both the stale
                    # terminal frame and the eventual main-menu frame. It
                    # avoids introducing another gameplay action while the
                    # game is finishing its death transition.
                    _emit_command("STATE", output_lock)
                    continue
                if outcome == "wait":
                    continue
                if outcome in {
                    "reward_cleanup_timeout",
                    "reward_count_mismatch",
                    "event_settlement_timeout",
                    "event_postcondition_mismatch",
                    "rest_settlement_timeout",
                    "hand_select_choose_settlement_timeout",
                    "proceed_settlement_timeout",
                    "hand_select_proceed_settlement_timeout",
                }:
                    receipt = dict(pending["receipt"])
                    failure_messages = {
                        "reward_cleanup_timeout": "parent CARD reward was not removed within 120 frames",
                        "reward_count_mismatch": "parent CARD reward count did not decrease by exactly one",
                        "event_settlement_timeout": (
                            "event reward did not satisfy its exact postcondition "
                            f"within {EVENT_SETTLE_TIMEOUT_FRAMES} frames"
                        ),
                        "event_postcondition_mismatch": (
                            "event reward changed state inconsistently with its exact postcondition"
                        ),
                        "rest_settlement_timeout": (
                            "campfire option did not leave REST within "
                            f"{REST_SETTLE_TIMEOUT_FRAMES} frames"
                        ),
                        "hand_select_choose_settlement_timeout": (
                            "HAND_SELECT CHOOSE did not make the confirm action "
                            "ready within "
                            f"{HAND_SELECT_CHOOSE_SETTLE_TIMEOUT_FRAMES} frames"
                        ),
                        "proceed_settlement_timeout": (
                            "PROCEED did not reach MAIN_MENU within "
                            f"{pending.get('proceed_settle_timeout_frames', PROCEED_SETTLE_TIMEOUT_FRAMES)} frames"
                        ),
                        "hand_select_proceed_settlement_timeout": (
                            "HAND_SELECT PROCEED did not leave the selection "
                            f"screen within {HAND_SELECT_PROCEED_SETTLE_TIMEOUT_FRAMES} frames"
                        ),
                    }
                    receipt.update(
                        {
                            "success": False,
                            "status": "failed",
                            "error": failure_messages[outcome],
                            "result_state_seq": sequence,
                        }
                    )
                elif outcome == "unchanged":
                    pending["unchanged_frames"] += 1
                    if pending["unchanged_frames"] < 3:
                        # Ask the mod for another authoritative frame while the
                        # controller remains blocked on the original receipt.
                        _emit_command("STATE", output_lock)
                        continue
                    receipt = dict(pending["receipt"])
                    receipt.update(
                        {
                            "success": False,
                            "status": "failed",
                            "error": "accepted action produced no changed decision state",
                            "result_state_seq": sequence,
                        }
                    )
                else:
                    receipt = dict(pending["receipt"])
                    receipt.update(
                        {
                            "success": "error" not in raw,
                            "status": "succeeded" if "error" not in raw else "failed",
                            "error": raw.get("error"),
                            "result_state_seq": sequence,
                        }
                    )
                    if (
                        "error" not in raw
                        and pending.get("opened_card_reward_parent_count") is not None
                        and current_state.get("phase") == "CARD_REWARD"
                    ):
                        active_card_reward_parent_count = pending["opened_card_reward_parent_count"]
                atomic_write_json(RECEIPT_PATH, receipt)
                if (
                    receipt.get("success")
                    and pending.get("accepted_followup_context") is not None
                ):
                    active_choice_followup_context = pending.get(
                        "accepted_followup_context"
                    )
                match_context = pending.get("match_game_context")
                if receipt.get("success") and match_context is not None:
                    match_first_flip_card_id = (
                        None
                        if match_context.get("second_flip")
                        else match_context.get("chosen_card_id")
                    )
                if (
                    pending.get("accepted_phase") == "CARD_REWARD"
                    and str((pending.get("payload") or {}).get("action") or "").lower() in {"return", "choose"}
                ):
                    active_card_reward_parent_count = None
                pending = None
                pending_ref["active"] = False
            if (
                active_choice_followup_context is not None
                and current_state.get("phase")
                not in {"NEOW", "BOSS_REWARD", "GRID"}
            ):
                active_choice_followup_context = None

            while True:
                if not bridge_instance_is_current(instance_token) or not parent_process_is_alive(parent_pid):
                    return
                try:
                    payload = json.loads(COMMAND_PATH.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise CommandRejected("command payload must be an object")
                    COMMAND_PATH.unlink(missing_ok=True)
                except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                    time.sleep(0.03)
                    continue

                try:
                    raw_command, fields = resolve_command(payload, current_state)
                    if str(payload.get("action") or "").lower() == "start":
                        acceptance = pre_run_binding.accept_start_payload(
                            payload,
                            current_state,
                            selection_path=PENDING_SELECTION_PATH,
                            history_path=RUN_HISTORY_PATH,
                            manifest_path=FREEZE_MANIFEST_PATH,
                            cohort_review_path=COHORT_REVIEW_PATH,
                        )
                        fields["start_acceptance_sha256"] = acceptance[
                            "sha256"
                        ]
                except (
                    CommandRejected,
                    pre_run_binding.PreRunBindingError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    atomic_write_json(RECEIPT_PATH, rejection_receipt(payload, current_state, exc))
                    continue

                receipt = {
                    "request_id": payload.get("id"),
                    "success": None,
                    "status": "accepted",
                    "error": None,
                    "accepted_state_seq": current_state["state_seq"],
                    "result_state_seq": None,
                    **fields,
                }
                atomic_write_json(RECEIPT_PATH, receipt)
                pending = {
                    "receipt": receipt,
                    "payload": payload,
                    "raw_command": raw_command,
                    "accepted_decision_id": current_state["decision_id"],
                    "accepted_phase": current_state.get("phase"),
                    "accepted_event_id": (((current_state.get("game_state") or {}).get("screen_state") or {}).get("event_id")),
                    "accepted_event_option_label": (
                        normalized_game_id(
                            event_option_label(
                                selected_bound_option(payload, current_state)
                            )
                        )
                        if current_state.get("phase") == "EVENT"
                        else None
                    ),
                    "accepted_event_effect_signature": (
                        event_effect_signature(current_state)
                        if current_state.get("phase") == "EVENT"
                        else None
                    ),
                    "accepted_event_reward_card_id": (
                        event_choice_reward_card_id(payload, current_state)
                        if current_state.get("phase") == "EVENT"
                        else None
                    ),
                    "accepted_room_phase": (current_state.get("game_state") or {}).get("room_phase"),
                    "accepted_turn": (((current_state.get("game_state") or {}).get("combat_state") or {}).get("turn")),
                    "unchanged_frames": 0,
                    "unstable_frames": 0,
                    "settle_frames": 0,
                    "stale_error_frames": 0,
                    "dispatch_pending": (
                        str(payload.get("action") or "").lower() == "choose"
                        and current_state.get("phase") == "HAND_SELECT"
                    ),
                    "dispatch_frames": 0,
                    "dispatch_stable_frames": 0,
                    "dispatch_signature": None,
                    "proceed_settle_timeout_frames": (
                        proceed_settle_timeout_frames(current_state)
                        if payload.get("action") == "proceed"
                        else None
                    ),
                }
                pending["accepted_followup_context"] = (
                    accepted_grid_followup_context(payload, current_state)
                )
                opened_parent_count = opened_card_reward_parent_count(payload, current_state)
                if opened_parent_count is not None:
                    pending["opened_card_reward_parent_count"] = opened_parent_count
                event_postcondition = event_choice_postcondition(payload, current_state)
                if event_postcondition is not None:
                    pending["event_postcondition"] = event_postcondition
                match_context = match_game_choice_context(
                    payload, current_state, match_first_flip_card_id
                )
                if match_context is not None:
                    pending["match_game_context"] = match_context
                    match_postcondition = match_game_reward_postcondition(
                        match_context, current_state
                    )
                    if match_postcondition is not None:
                        pending["event_postcondition"] = match_postcondition
                if (
                    current_state.get("phase") == "CARD_REWARD"
                    and str(payload.get("action") or "").lower() in {"return", "choose"}
                    and active_card_reward_parent_count is not None
                ):
                    pending["parent_card_reward_count_before"] = active_card_reward_parent_count
                if payload.get("action") == "choose" and current_state.get("phase") in {"GRID", "HAND_SELECT"}:
                    accepted_option = next(
                        (
                            option for option in current_state.get("options") or []
                            if option.get("option_id") == payload.get("option_id")
                        ),
                        None,
                    )
                    pending["accepted_card_instance_id"] = (
                        ((accepted_option or {}).get("target") or {}).get("card_instance_id")
                    )
                pending_ref["active"] = True
                if pending.get("dispatch_pending"):
                    # The pre-dispatch branch above owns the first raw STATE
                    # probe and will emit CHOOSE only after two stable frames.
                    _emit_command("STATE", output_lock)
                else:
                    _emit_command(raw_command, output_lock)
                break
    finally:
        wake_stop.set()
        wake_thread.join(timeout=1.0)
        release_bridge_instance(instance_token)


if __name__ == "__main__":
    main()
