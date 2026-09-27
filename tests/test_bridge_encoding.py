import os
import subprocess
import sys
import unittest


class BridgeEncodingTests(unittest.TestCase):
    def test_pipe_encoding_overrides_inherited_python_locale(self):
        message = '{"name":"防御 中文路径"}\n'.encode("utf-8")
        for encoding in ("gbk", "cp1252"):
            env = dict(os.environ, PYTHONIOENCODING=encoding, PYTHONUTF8="0")
            result = subprocess.run(
                [sys.executable, "-c", "import bridge,sys; bridge.configure_protocol_stdio(); sys.stdout.write(sys.stdin.read())"],
                input=message, capture_output=True, env=env, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(message, result.stdout.replace(b"\r\n", b"\n"))
