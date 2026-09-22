# 尖塔助手（Windows）

玩家手动操作《Slay the Spire》，助手实时显示推荐动作、目标和判断依据。本地建议无需 API Key。

## 下载和使用

前往 [Windows 发布页](https://github.com/majiamajiabj-boop/sts-cli/releases/tag/windows-20260921-230502)，下载 ZIP 并完整解压，双击 `SpireAssistant.exe`。运行环境已包含，无需安装 Python、Git 或开发工具。

需要自行拥有游戏，并通过 Steam 安装 ModTheSpire 和 BaseMod。详细安装、手动选择目录、实时顾问、自动游玩及战报入口见 [使用说明](WINDOWS_ASSISTANT_README.md)。仓库目前为私有，访问和下载需要仓库权限。

## 当前版本

- 中文侧栏界面、大字建议、可选精简置顶窗口；已修复本机 125% 缩放下的 DPI 模糊。
- 顾问模式仅读取快照，与自动游玩互斥；状态变化、结算或断线后撤下旧建议。
- 完整回归 2947 项通过，实际 EXE 已在源码目录之外启动并验证。
- 玩家已确认战斗、手动操作刷新、奖励和地图正常。商店、篝火、完整卡牌选择、退出游戏断线等实机项目仍待验收；不将回放等同于实机验证。

详见 [测试记录与已知限制](ASSISTANT_TEST_RECORD.md)、[第三方分发说明](THIRD_PARTY_NOTICES.md) 和 [阶段进度](ASSISTANT_PROGRESS.md)。

## 源码与构建

本仓库首次上传为已测试工作区的完整源码快照，包含既有策略修改及其回归测试。构建入口为 `build_windows.py`，准备条件和命令见 [从源码构建](WINDOWS_ASSISTANT_README.md#从源码构建)。CommunicationMod Java 源码位于 `src/CommunicationMod-1.2.1`，EXE 启动器位于 `packaging/Launcher.cs`。

游戏、游戏运行时、ModTheSpire 和 BaseMod 不随包分发。第三方代码的许可证保留在各源码目录及 `packaging/licenses`。本版本仅供本地分享，没有上传创意工坊。
