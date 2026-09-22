using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Launcher {
    private static string Quote(string value) {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value) {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(ch); slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }
    [STAThread]
    public static int Main(string[] args) {
        try {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string app = Path.Combine(root, "app");
            string python = Path.Combine(app, "runtime", "pythonw.exe");
            string script = Path.Combine(app, "assistant_app.py");
            if (!File.Exists(python) || !File.Exists(script))
                throw new Exception("发布包不完整。请先将整个 ZIP 解压到可写文件夹，再运行 EXE；不要只复制 EXE。缺少内置运行环境或程序文件。");
            string data = Path.Combine(root, "data");
            Directory.CreateDirectory(data);
            string probe = Path.Combine(data, ".write-test-" + Guid.NewGuid().ToString("N"));
            File.WriteAllText(probe, "ok"); File.Delete(probe);
            var command = new StringBuilder("-B -E -s ").Append(Quote(script));
            foreach (string arg in args) command.Append(' ').Append(Quote(arg));
            var start = new ProcessStartInfo(python, command.ToString());
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WorkingDirectory = app;
            start.EnvironmentVariables["STS_ASSISTANT_DATA"] = data;
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            start.RedirectStandardError = true;
            using (Process child = Process.Start(start)) {
                string error = child.StandardError.ReadToEnd();
                child.WaitForExit();
                if (child.ExitCode != 0) {
                    File.WriteAllText(Path.Combine(data, "assistant-error.txt"), error, Encoding.UTF8);
                    throw new Exception("助手未能启动或意外退出。请查看 data/assistant-error.txt 中的原因。\n" + error.Substring(0, Math.Min(900, error.Length)));
                }
                return child.ExitCode;
            }
        } catch (Exception ex) {
            MessageBox.Show(ex.Message, "尖塔助手 · 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
