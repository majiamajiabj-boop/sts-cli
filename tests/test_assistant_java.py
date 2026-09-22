"""Execute compiled production observer/command rejection logic without game play."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from assistant_paths import ROOT, Installation, installation, java_property


def jdk_home():
    explicit = os.environ.get("JAVA_HOME")
    if explicit and (Path(explicit) / "bin/javac.exe").is_file():
        return Path(explicit)
    return next((p.parent.parent for p in (ROOT / ".tools").glob("jdk*/**/bin/javac.exe")), None)


def test_installation():
    game = os.environ.get("STS_ASSISTANT_TEST_GAME")
    if not game:
        return installation()
    game = Path(game)
    return Installation(game, game / "jre/bin/java.exe", Path(os.environ["STS_ASSISTANT_TEST_MTS"]), Path(os.environ["STS_ASSISTANT_TEST_BASEMOD"]))


def test_jar_path():
    explicit = os.environ.get("STS_ASSISTANT_TEST_JAR")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise RuntimeError("本次构建指定的测试 JAR 不存在，不能回退到旧发布包。")
        return path
    packages = sorted((ROOT / "dist").glob("SpireAssistant-Windows-*/app/CommunicationMod.jar"))
    return packages[-1] if packages else None


class JavaAdvisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jdk = jdk_home()
        if os.environ.get("STS_ASSISTANT_RELEASE_TESTS") == "1":
            if cls.jdk is None or not os.environ.get("STS_ASSISTANT_TEST_JAR"):
                raise RuntimeError("发布构建必须提供可执行 JDK 和本次 JAR，不允许跳过 Java 验证。")
            if test_installation().errors():
                raise RuntimeError("发布构建的 Java 测试依赖缺失，不允许跳过验证。")
            test_jar_path()
        if cls.jdk is None:
            raise unittest.SkipTest("Java 离线执行测试需要构建用 JDK")

    def test_production_snapshot_sequence_heartbeat_busy_unicode(self):
        with tempfile.TemporaryDirectory(prefix="顾问 Java ") as tmp:
            directory = Path(tmp)
            source = directory / "communicationmod"
            source.mkdir()
            production = ROOT / "src/CommunicationMod-1.2.1/src/main/java/communicationmod/AdvisorSnapshot.java"
            (source / "AdvisorSnapshot.java").write_bytes(production.read_bytes())
            (source / "GameStateConverter.java").write_text('''package communicationmod;
public class GameStateConverter { public static String raw="{\\"in_game\\":true,\\"x\\":1}"; public static String getCommunicationState(){return raw;} }
''')
            (source / "GameStateListener.java").write_text('''package communicationmod;
public class GameStateListener { public static boolean ready=true; public static boolean isAdvisorReady(){return ready;} }
''')
            (source / "Probe.java").write_text('''package communicationmod;
import java.nio.file.*;
public class Probe {
 public static void main(String[] args) throws Exception {
  Path p=Paths.get(System.getProperty("sts.assistant.state"));
  AdvisorSnapshot.update(); System.out.println(new String(Files.readAllBytes(p),"UTF-8"));
  Thread.sleep(120); AdvisorSnapshot.update(); System.out.println(new String(Files.readAllBytes(p),"UTF-8"));
  GameStateListener.ready=false; Thread.sleep(120); AdvisorSnapshot.update(); System.out.println(new String(Files.readAllBytes(p),"UTF-8"));
  GameStateConverter.raw="{\\"in_game\\":true,\\"x\\":2}"; Thread.sleep(120); AdvisorSnapshot.update(); System.out.println(new String(Files.readAllBytes(p),"UTF-8"));
 }
}''')
            info = test_installation()
            cp = str(info.game / "desktop-1.0.jar")
            if not Path(cp).is_file():
                self.skipTest("Java 测试需要游戏 JAR 中的 Gson")
            compile_result = subprocess.run([str(self.jdk / "bin/javac.exe"), "-encoding", "UTF-8", "-cp", cp, "-d", str(directory)] + [str(p) for p in source.glob("*.java")], capture_output=True)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run([str(self.jdk / "bin/java.exe"), "-Dsts.assistant.mode=advisor", "-Dsts.assistant.state="+str(directory / "实时 建议.json"), "-cp", str(directory)+os.pathsep+cp, "communicationmod.Probe"], capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([row["state_seq"] for row in rows], [1, 1, 2, 3])
            self.assertGreater(rows[1]["observed_at"], rows[0]["observed_at"])
            self.assertEqual(len({row["session"] for row in rows}), 1)
            self.assertFalse(rows[2]["ready"])
            self.assertEqual(rows[3]["state"]["x"], 2)

    def test_release_jar_rejects_every_command_without_initializing_game(self):
        package = test_jar_path()
        if package is None:
            self.skipTest("先构建发布包，才能测试实际 JAR")
        info = test_installation()
        if info.errors():
            self.skipTest("需要游戏和已订阅 Mod 作为类路径，仅读取类，不启动游戏")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "CommandProbe.java"
            source.write_text('''import communicationmod.*;
public class CommandProbe {
 public static void main(String[] args) throws Exception {
  for(String cmd: new String[]{"play 1 0", "end", "choose 0", "potion use 0", "proceed", "cancel", "start ironclad", "resume ironclad", "state", "key A", "click 0 0", "wait 10"}) {
   try { CommandExecutor.executeCommand(cmd); throw new AssertionError("Command escaped: "+cmd); }
   catch (InvalidCommandException e) { if(!e.getMessage().contains("Read-only advisor")) throw e; }
  }
  CommunicationMod.queueCommand("play 1 0");
  java.lang.reflect.Field queue=CommunicationMod.class.getDeclaredField("readQueue"); queue.setAccessible(true);
  if(queue.get(null)!=null && !((java.util.Queue)queue.get(null)).isEmpty()) throw new AssertionError("queued command");
  System.out.println("12 commands rejected; no game initialization; queue blocked");
 }
}''')
            cp = os.pathsep.join(str(p) for p in (package, info.game / "desktop-1.0.jar", info.mts, info.basemod))
            compiled = subprocess.run([str(self.jdk / "bin/javac.exe"), "-cp", cp, "-d", str(directory), str(source)], capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(self.jdk / "bin/java.exe"), "-Dsts.assistant.mode=advisor", "-cp", str(directory)+os.pathsep+cp, "CommandProbe"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"12 commands rejected", result.stdout)

    def test_release_jar_input_patches_inject_with_real_modthespire(self):
        package = test_jar_path()
        info = test_installation()
        if package is None or info.errors():
            self.skipTest("需要发布 JAR 与本机 ModTheSpire 编译依赖")
        with tempfile.TemporaryDirectory(prefix="真实 补丁注入 ") as tmp:
            directory = Path(tmp)
            source = directory / "PatchProbe.java"
            source.write_text("""import javassist.*;
import com.evacipated.cardcrawl.modthespire.patcher.PrefixPatchInfo;
public class PatchProbe {
 public static void main(String[] args) throws Exception {
  ClassPool pool = new ClassPool(true);
  for (String path : args) pool.insertClassPath(path);
  CtClass input = pool.get("com.megacrit.cardcrawl.helpers.input.InputAction");
  String[][] cases = {{"isPressed", "PressedPatch"}, {"isJustPressed", "JustPressedPatch"}};
  for (String[] entry : cases) {
   CtMethod original = input.getDeclaredMethod(entry[0]);
   CtMethod prefix = pool.get("communicationmod.patches.InputActionPatch$" + entry[1]).getDeclaredMethod("Prefix");
   new PrefixPatchInfo(original, prefix).doPatch();
  }
  if (input.toBytecode().length == 0) throw new AssertionError("empty patched bytecode");
  System.out.println("real-modthespire-input-patches-injected");
 }
}""", encoding="utf-8")
            paths = [str(p) for p in (package, info.game / "desktop-1.0.jar", info.mts, info.basemod)]
            cp = os.pathsep.join(paths)
            compiled = subprocess.run([str(self.jdk / "bin/javac.exe"), "-encoding", "UTF-8", "-cp", cp, "-d", str(directory), str(source)], capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(self.jdk / "bin/java.exe"), "-cp", str(directory)+os.pathsep+cp, "PatchProbe"] + paths, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(b"real-modthespire-input-patches-injected", result.stdout)

    def test_properties_json_args_roundtrip(self):
        from launch_game import _write_bridge_config
        with tempfile.TemporaryDirectory(prefix="属性 路径 ") as tmp:
            directory = Path(tmp)
            config = directory / "config.properties"
            python = directory / "运行 时/python.exe"
            bridge = directory / "助手 路径/bridge.py"
            _write_bridge_config(config, python_path=python, bridge_path=bridge)
            source = directory / "PropertyProbe.java"
            source.write_text('''import java.util.*; import java.io.*; import com.google.gson.Gson;
public class PropertyProbe { public static void main(String[] args) throws Exception {
 Properties p=new Properties(); try(InputStream in=new FileInputStream(args[0])){p.load(in);}
 String[] decoded=new Gson().fromJson(p.getProperty("commandJson"),String[].class);
 if(decoded.length!=2 || !decoded[0].equals(args[1]) || !decoded[1].equals(args[2])) throw new AssertionError(p);
 System.out.println("unicode-space-json-roundtrip-ok");
} }''')
            cp = str(test_installation().game / "desktop-1.0.jar")
            if not Path(cp).is_file():
                self.skipTest("需要本机 Gson 编译类路径")
            compiled = subprocess.run([str(self.jdk / "bin/javac.exe"), "-cp", cp, "-d", str(directory), str(source)], capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(self.jdk / "bin/java.exe"), "-cp", str(directory)+os.pathsep+cp, "PropertyProbe", str(config), python.as_posix(), bridge.as_posix()], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
