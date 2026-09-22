"""Explicit user-invoked launch actions; no launch at import or GUI startup."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from assistant_paths import ROOT, data_dir, installation, bundled_python


class ModeLock:
    """OS-owned interprocess lock shared by all copies under this Windows account."""
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpireAssistant")
        self.handle = None

    def acquire(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.directory / "mode.lock").open("a+b")
        self.handle.seek(0)
        if not self.handle.read(1):
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("另一个助手已占用游戏模式，请先退出它。") from exc
        return self

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None


def running_game_processes():
    if os.name != "nt":
        return []
    # Read only. No Stop-Process and no process-name-wide termination.
    ps = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    script = "Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^(javaw?|SlayTheSpire|pythonw?)\\.exe$') -and ($_.CommandLine -match '(ModTheSpire\\.jar|desktop-1\\.0\\.jar|SlayTheSpire|autoplay(?:_shared_v4)?\\.py|campaign_attempt\\.py)') } | Select-Object ProcessId,Name | ConvertTo-Json -Compress"
    result = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("无法检查游戏进程，未启动游戏。请关闭现有游戏后重试。")
    value = json.loads(result.stdout.strip() or "[]")
    return value if isinstance(value, list) else [value]


def require_idle_game():
    processes = running_game_processes()
    if processes:
        raise RuntimeError("检测到游戏或自动控制器仍在运行（PID " + ", ".join(str(p["ProcessId"]) for p in processes) + "）。请手动退出后再选择模式。")


def install_mod(config):
    require_idle_game()
    info = installation(settings=config)
    errors = info.errors()
    if errors:
        raise RuntimeError("\n".join(errors))
    source = ROOT / "CommunicationMod.jar"
    if not source.is_file():
        raise RuntimeError("发布包缺少 CommunicationMod.jar，请重新解压完整发布包。")
    directory = info.game / "mods"
    directory.mkdir(exist_ok=True)
    target = directory / source.name
    if target.exists() and target.read_bytes() != source.read_bytes():
        backup = target.with_name(f"CommunicationMod.jar.backup-{time.time_ns()}")
        shutil.copy2(target, backup)
    shutil.copy2(source, target)
    # ModTheSpire may be installed in a different Steam library.
    if info.basemod.parent != directory and "workshop" not in info.basemod.parts:
        dest = directory / "BaseMod.jar"
        if dest.exists() and dest.read_bytes() != info.basemod.read_bytes():
            shutil.copy2(dest, dest.with_name(f"BaseMod.jar.backup-{time.time_ns()}"))
        shutil.copy2(info.basemod, dest)
    return info


def advisor_command(info, state_path):
    return [str(info.java), "-Dsts.assistant.mode=advisor", f"-Dsts.assistant.state={Path(state_path).resolve()}", "-jar", str(info.mts), "--skip-intro", "--mods", "basemod,CommunicationMod"]


def start_advisor(config):
    info = install_mod(config)
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "advisor-state.json"
    state.unlink(missing_ok=True)
    # No bridge external process is configured or started in observer mode.
    with (directory / "advisor-game.log").open("ab") as log:
        return subprocess.Popen(advisor_command(info, state), cwd=info.game, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def auto_preflight():
    path = ROOT / "release-preflight.json"
    if not path.is_file():
        raise RuntimeError("此包尚无通过验证的自动模式发布凭据。自动游玩未启动；请使用含有效发布凭据的版本。")
    import quick_campaign
    import freeze_manifest
    hashes = quick_campaign.current_policy_hashes(ROOT)
    freeze_manifest.validate_static_preflight(json.loads(path.read_text(encoding="utf-8")), ROOT, hashes["decision_hash"], hashes["controller_hash"])


def start_auto(config, remote=None):
    auto_preflight()
    info = install_mod(config)
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(name, None)
    env.update({"STS_GAME_DIR": str(info.game), "STS_MODTHESPIRE": str(info.mts), "STS_BASEMOD": str(info.basemod)})
    if remote:
        env.update(remote)
    with (data_dir() / "auto-start.log").open("ab") as log:
        return subprocess.Popen([str(bundled_python()), str(ROOT / "quick_campaign.py")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
