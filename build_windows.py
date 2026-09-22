"""Build an allowlisted Windows distribution; never install a mod or launch a game.

Usage: python build_windows.py --python-home PATH --jdk PATH [--game-dir PATH]
Requires a developer CPython with Tk, JDK 8, and Windows .NET Framework csc.
Friends only receive the resulting ZIP and need none of the build tools.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from assistant_paths import ROOT, installation

MODULES = "assistant_release assistant_app assistant_advisor assistant_paths assistant_runtime autoplay autoplay_runner bridge campaign_attempt campaign_selector cohort_report cohort_review death_replay decision_case_corpus decision_case_replay decision_case_resolution decision_cases deepseek_macro freeze_manifest independent_oracle launch_game macro_policy macro_wire policy_contracts pre_run_binding prompt_assembler quick_campaign quick_start release_preflight run_context strategy_audit stsctl".split()


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_selected(source, target, suffixes):
    for path in source.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes and not any(part in {"__pycache__", "site-packages", ".git", "test", "tests", "idlelib", "ensurepip"} for part in path.parts):
            copy_file(path, target / path.relative_to(source))


def mod_compile_command(javac, classpath, classes, argsfile):
    # -encoding governs source files; JDK 8 reads @argfiles using file.encoding.
    # ModTheSpire resolves patch parameters via LocalVariableTable, not MethodParameters.
    return [str(javac), "-J-Dfile.encoding=UTF-8", "-g:source,lines,vars", "-encoding", "UTF-8", "-source", "8", "-target", "8", "-cp", classpath, "-d", str(classes), "@"+str(argsfile)]


def build_verification_environment(app, jdk, info):
    environment = os.environ.copy()
    environment.update({
        "JAVA_HOME": str(jdk),
        "STS_ASSISTANT_RELEASE_TESTS": "1",
        "STS_ASSISTANT_TEST_JAR": str(app / "CommunicationMod.jar"),
        "STS_ASSISTANT_TEST_GAME": str(info.game),
        "STS_ASSISTANT_TEST_MTS": str(info.mts),
        "STS_ASSISTANT_TEST_BASEMOD": str(info.basemod),
    })
    return environment


def build_mod(destination, build, jdk, info):
    javac = jdk / "bin/javac.exe"
    if not javac.is_file():
        raise RuntimeError("构建通信 Mod 需要 JDK（指定 --jdk，目录内应有 bin/javac.exe）。")
    missing = info.errors()
    if missing:
        raise RuntimeError("构建需要本地游戏与 Mod 作为编译依赖：" + "；".join(missing))
    classes = build / "java-classes"
    classes.mkdir()
    source = ROOT / "src/CommunicationMod-1.2.1/src/main"
    sources = sorted((source / "java").rglob("*.java"))
    argsfile = build / "javac-sources.txt"
    argsfile.write_text("\n".join('"' + p.as_posix() + '"' for p in sources), encoding="utf-8")
    cp = os.pathsep.join(str(p) for p in (info.game / "desktop-1.0.jar", info.mts, info.basemod))
    result = subprocess.run(mod_compile_command(javac, cp, classes, argsfile), capture_output=True)
    (build / "javac.log").write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"通信 Mod 编译失败，请查看 {build / 'javac.log'}")
    metadata = {"modid": "CommunicationMod", "name": "Communication Mod + Read-only Advisor", "author_list": ["Forgotten Arbiter", "sts-cli contributors"], "description": "Protocol-v2 controller and passive manual-play advisor. Advisor mode rejects all commands.", "version": "1.2.1-assistant.1", "sts_version": "12-18-2022", "mts_version": "3.18.1", "dependencies": ["basemod"]}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as jar:
        for path in sorted(classes.rglob("*.class")):
            jar.write(path, path.relative_to(classes).as_posix())
        jar.writestr("ModTheSpire.json", json.dumps(metadata))
        jar.write(source / "resources/Icon.png", "Icon.png")
        jar.write(ROOT / "src/CommunicationMod-1.2.1/LICENSE", "LICENSE")


def copy_runtime(source, target):
    if not (source / "pythonw.exe").is_file() or not (source / "tcl").is_dir():
        raise RuntimeError("--python-home 必须是包含 pythonw.exe、DLLs、Lib 和 tcl 的 Windows CPython 运行时。")
    target.mkdir()
    for path in source.iterdir():
        if path.is_file() and (path.name in {"python.exe", "pythonw.exe", "LICENSE.txt"} or path.name.startswith(("python3", "vcruntime140")) and path.suffix == ".dll"):
            copy_file(path, target / path.name)
    copy_selected(source / "Lib", target / "Lib", {".py", ".txt", ".pem"})
    # No site-packages, pip, project env, Codex packages or bytecode are included.
    for path in (source / "DLLs").iterdir():
        if path.suffix.lower() in {".pyd", ".dll"} and not path.name.startswith("_test") and path.name != "_ctypes_test.pyd":
            copy_file(path, target / "DLLs" / path.name)
    for path in (source / "tcl").rglob("*"):
        if path.is_file():
            copy_file(path, target / "tcl" / path.relative_to(source / "tcl"))


def audit_release(package):
    forbidden = {".env", "state.json", "run-history.jsonl", "run-result.json", "command.json", "settings.json", "advisor-state.json"}
    manifest = {}
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        if path.name in forbidden or path.name.startswith(".env") or path.suffix in {".log", ".save", ".autosave", ".pyc"} or any(part in {"site-packages", "logs", ".git", "saves", "preferences", "__pycache__"} for part in path.parts):
            raise RuntimeError("发布白名单检查失败：" + relative)
        if path.suffix.lower() in {".py", ".json", ".md", ".cs", ".ps1", ".cmd", ".java"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            # Detect assigned private key material, not environment variable names/docs.
            import re
            if re.search(r"(?:sk-[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", text):
                raise RuntimeError("发布内容包含疑似密钥：" + relative)
        manifest[relative] = {"size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return manifest


def verify_source(app, build, jdk, info):
    import freeze_manifest
    before = freeze_manifest.runtime_artifact_snapshot(ROOT)
    source_before = freeze_manifest.source_snapshot(ROOT)
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-p", "test_*.py"], cwd=ROOT, env=build_verification_environment(app, jdk, info), capture_output=True, text=True, encoding="utf-8", errors="replace")
    (build / "full-tests.log").write_text(tests.stdout + tests.stderr, encoding="utf-8")
    diff = subprocess.run(["git", "diff", "--check"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (build / "diff-check.log").write_text(diff.stdout + diff.stderr, encoding="utf-8")
    after = freeze_manifest.runtime_artifact_snapshot(ROOT)
    verification = {"full_tests": freeze_manifest._completed_process_record(tests), "git_diff_check": freeze_manifest._completed_process_record(diff), "runtime_artifacts_unchanged": before == after,
        "runtime_artifacts_before_sha256": freeze_manifest.snapshot_digest(before), "runtime_artifacts_after_sha256": freeze_manifest.snapshot_digest(after)}
    freeze_manifest._validate_verification_evidence(verification)
    if source_before != freeze_manifest.source_snapshot(ROOT):
        raise RuntimeError("测试期间源码发生变化，必须重新构建。")
    shipped = {}
    shipped_paths = set(freeze_manifest.source_snapshot(app))
    shipped_paths.update(path.relative_to(app).as_posix() for path in (app / "viewer").rglob("*") if path.is_file())
    for relative in sorted(shipped_paths):
        if relative == "CommunicationMod.jar":
            # The compiled JAR is verified by test_assistant_java against this exact package.
            pass
        else:
            original = ROOT / relative
            if not original.is_file() or original.read_bytes() != (app / relative).read_bytes():
                raise RuntimeError("发布源码与通过测试的源码不一致：" + relative)
        shipped[relative] = hashlib.sha256((app / relative).read_bytes()).hexdigest()
    proof = {"verification": verification, "packaged_sources": shipped}
    path = build / "source-verification.json"
    path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    sealed = subprocess.run([str(app / "runtime/python.exe"), "-B", "-E", "-s", str(app / "assistant_release.py"), "--verification", str(path)], cwd=app, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (build / "seal.log").write_text(sealed.stdout + sealed.stderr, encoding="utf-8")
    if sealed.returncode:
        raise RuntimeError("自动模式静态凭据生成失败，请查看 " + str(build / "seal.log"))
    return json.loads(sealed.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-home", type=Path, default=Path(sys.executable).parent)
    parser.add_argument("--jdk", type=Path, required=True)
    parser.add_argument("--game-dir", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    build = ROOT / ".tools" / ("assistant-build-" + stamp)
    build.mkdir(parents=True)
    package = args.output.resolve() / ("SpireAssistant-Windows-" + stamp)
    package.mkdir(parents=True)
    app = package / "app"
    app.mkdir()
    for module in MODULES:
        copy_file(ROOT / (module + ".py"), app / (module + ".py"))
    for rel, suffixes in (("src/spirecomm-master/spirecomm", {".py"}), ("src/CommunicationMod-1.2.1/src", {".java", ".json", ".png"}), ("knowledge", {".md", ".json", ".txt"}), ("prompts", {".md", ".json", ".txt"}), ("viewer/static", {".html", ".css", ".js"})):
        copy_selected(ROOT / rel, app / rel, suffixes)
    copy_file(ROOT / "viewer/server.py", app / "viewer/server.py")
    for name in ("CommunicationMod-1.2.1", "spirecomm-master"):
        copy_file(ROOT / "src" / name / "LICENSE", app / "src" / name / "LICENSE")
        copy_file(ROOT / "src" / name / "LICENSE", package / "licenses" / (name + "-LICENSE.txt"))
    copy_selected(ROOT / "packaging/licenses", package / "licenses", {".txt", ".html"})
    copy_selected(ROOT / "packaging/licenses", app / "packaging/licenses", {".txt", ".html"})
    jdk = args.jdk.resolve()
    info = installation(args.game_dir)
    build_mod(app / "CommunicationMod.jar", build, jdk, info)
    copy_runtime(args.python_home.resolve(), app / "runtime")
    csc = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(csc), "/nologo", "/target:winexe", "/platform:x64", "/reference:System.Windows.Forms.dll", "/out:"+str(package / "SpireAssistant.exe"), str(ROOT / "packaging/Launcher.cs")], check=True)
    for name in ("WINDOWS_ASSISTANT_README.md", "THIRD_PARTY_NOTICES.md", "ASSISTANT_TEST_RECORD.md", "DEEPSEEK_MACRO.md"):
        copy_file(ROOT / "docs" / name, package / name)
    copy_file(ROOT / "build_windows.py", app / "build_windows.py")
    copy_file(ROOT / "packaging/Launcher.cs", app / "packaging/Launcher.cs")
    for name in ("decision-cases-v2.jsonl", "decision-case-resolution-invariants-v1.json"):
        copy_file(ROOT / "test_fixtures" / name, app / "test_fixtures" / name)
    # Seal only after the full workspace suite passed and each shipped source matched it.
    gate = verify_source(app, build, jdk, info)
    manifest = audit_release(package)
    (package / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    archive = shutil.make_archive(str(package), "zip", root_dir=package.parent, base_dir=package.name)
    print(json.dumps({"package": str(package), "zip": archive, "files": len(manifest), "zip_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(), "build_log": str(build), "auto_static_gate": gate}, ensure_ascii=False))


if __name__ == "__main__":
    main()
