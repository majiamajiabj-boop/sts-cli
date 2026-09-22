"""Portable discovery and configuration. Importing this module never starts a game."""
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSHOP = {"ModTheSpire": "1605060445", "BaseMod": "1605833019"}


def data_dir():
    return Path(os.environ.get("STS_ASSISTANT_DATA", str(ROOT / "assistant-data"))).resolve()


def load_settings():
    try:
        value = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "settings.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def steam_roots():
    roots = []
    if os.environ.get("STEAM_PATH"):
        roots.append(Path(os.environ["STEAM_PATH"]))
    if os.name == "nt":
        import winreg
        for hive, key, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, name)[0]))
            except OSError:
                pass
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / "Steam")
    return list(dict.fromkeys(p.resolve() for p in roots))


def steam_libraries(roots=None):
    result = []
    for root in (steam_roots() if roots is None else roots):
        root = Path(root)
        result.append(root)
        for config in (root / "steamapps/libraryfolders.vdf", root / "config/libraryfolders.vdf"):
            try:
                content = config.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            for key, value in re.findall(r'"([^"\r\n]+)"\s*"((?:\\.|[^"\\])*)"', content):
                if key == "path" or (key.isdigit() and (":" in value or value.startswith("/"))):
                    result.append(Path(value.replace("\\\\", "\\")))
    return list(dict.fromkeys(p.resolve() for p in result))


def discover_games(libraries=None):
    games = []
    for library in (steam_libraries() if libraries is None else libraries):
        apps = Path(library) / "steamapps"
        install = "SlayTheSpire"
        try:
            manifest = (apps / "appmanifest_646570.acf").read_text(encoding="utf-8-sig")
            match = re.search(r'"installdir"\s*"([^"\r\n]+)"', manifest)
            if match and Path(match[1]).name == match[1]:
                install = match[1]
        except OSError:
            pass
        game = apps / "common" / install
        if (game / "desktop-1.0.jar").is_file():
            games.append(game.resolve())
    return list(dict.fromkeys(games))


@dataclass(frozen=True)
class Installation:
    game: Path
    java: Path
    mts: Path
    basemod: Path

    def errors(self):
        errors = []
        for path, message in (
            (self.game / "desktop-1.0.jar", "未找到游戏本体，请选择包含 desktop-1.0.jar 的 SlayTheSpire 文件夹。"),
            (self.java, "未找到游戏自带 Java，请在 Steam 验证游戏文件完整性。"),
            (self.mts, "缺少 ModTheSpire，请在 Steam 创意工坊订阅并等待下载，或手动选择 JAR。"),
            (self.basemod, "缺少 BaseMod，请在 Steam 创意工坊订阅并等待下载，或手动选择 JAR。"),
        ):
            if not path.is_file():
                errors.append(message)
        return errors


def installation(game=None, settings=None, libraries=None):
    settings = load_settings() if settings is None else settings
    libraries = steam_libraries() if libraries is None else list(libraries)
    detected = discover_games(libraries)
    game = Path(game or os.environ.get("STS_GAME_DIR") or settings.get("game_dir") or (detected[0] if detected else ROOT / "未选择游戏目录")).resolve()
    if game.parent.name.lower() == "common":
        libraries.insert(0, game.parent.parent.parent)
    def mod(name):
        explicit = os.environ.get("STS_" + name.upper()) or settings.get(name.lower())
        if explicit:
            return Path(explicit).resolve()
        candidates = [game / (name + ".jar"), game / "mods" / (name + ".jar")]
        candidates += [Path(p) / "steamapps/workshop/content/646570" / WORKSHOP[name] / (name + ".jar") for p in libraries]
        return next((p for p in candidates if p.is_file()), candidates[0])
    return Installation(game, game / "jre/bin/java.exe", mod("ModTheSpire"), mod("BaseMod"))


def java_property(value):
    # java.util.Properties loads ISO-8859-1; escape Unicode and metacharacters.
    value = str(value).replace("\\", "/")
    out = []
    for char in value:
        if ord(char) > 127:
            encoded = char.encode("utf-16-be")
            out.extend("\\u%04x" % int.from_bytes(encoded[i:i+2], "big") for i in range(0, len(encoded), 2))
        elif char in ":=#!":
            out.append("\\" + char)
        elif char in "\r\n":
            raise ValueError("路径不能包含换行符。")
        else:
            out.append(char)
    return "".join(out)


def bundled_python(root=ROOT):
    candidate = Path(root) / "runtime/python.exe"
    return candidate if candidate.is_file() else Path(sys.executable)
