import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_paths import Installation
from build_windows import build_verification_environment, mod_compile_command
from tests.test_assistant_java import jdk_home, test_jar_path


class PortableBuildTests(unittest.TestCase):
    def test_jdk8_compiles_utf8_argfile_in_chinese_space_directory(self):
        jdk = jdk_home()
        if jdk is None:
            self.skipTest("需要 JDK 执行实际参数文件编码测试")
        with tempfile.TemporaryDirectory(prefix="构建 中文 ") as tmp:
            root = Path(tmp)
            source = root / "Hello.java"
            source.write_text("class Hello {}", encoding="utf-8")
            argsfile = root / "源文件 列表.txt"
            argsfile.write_text('"' + source.as_posix() + '"', encoding="utf-8")
            result = subprocess.run(mod_compile_command(jdk / "bin/javac.exe", "", root, argsfile), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "Hello.class").is_file())

    def test_custom_output_jar_is_used_instead_of_latest_dist(self):
        with tempfile.TemporaryDirectory(prefix="自定义 发布 ") as tmp:
            app = Path(tmp)
            jar = app / "CommunicationMod.jar"
            jar.write_bytes(b"explicit target selection test")
            info = Installation(app, app / "java.exe", app / "mts.jar", app / "base.jar")
            environment = build_verification_environment(app, app / "JDK", info)
            self.assertEqual(environment["JAVA_HOME"], str(app / "JDK"))
            self.assertEqual(environment["STS_ASSISTANT_RELEASE_TESTS"], "1")
            with patch.dict(os.environ, environment):
                self.assertEqual(test_jar_path(), jar)
                with patch.dict(os.environ, {"STS_ASSISTANT_TEST_JAR": str(app / "absent.jar")}):
                    with self.assertRaisesRegex(RuntimeError, "不能回退"):
                        test_jar_path()
