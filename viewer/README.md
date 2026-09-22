# 尖塔战报

这是一个只读的本地网页，仅展示 `logs/attempts` 中已经完成并归档的最近十局游戏。每局同时展示实际模型调用、有效建议和最终采纳次数，详情中可查看一致、回退、缓存、延迟与估算费用。

## 启动（推荐）

直接双击 `start-viewer.cmd`，或者在当前 `viewer` 目录的 PowerShell 中运行：

```powershell
.\start-viewer.cmd
```

然后打开 <http://127.0.0.1:8765>。

如果 PowerShell 当前位于仓库根目录，则运行：

```powershell
.\viewer\start-viewer.cmd
```

`.cmd` 启动方式不受 PowerShell 脚本执行策略影响。

服务只监听本机回环地址，只接受 GET/HEAD 请求，不读取当前局的传输文件，也不会写入任何游戏记录。关闭 PowerShell 窗口或按 `Ctrl+C` 即可停止。
