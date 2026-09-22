# 尖塔助手（Windows）

[English](README.en.md) | 简体中文

玩家手动操作《Slay the Spire》，助手实时显示推荐动作、目标和判断依据。本地建议无需 API Key。

## 下载和使用

前往 [Windows 发布页](https://github.com/majiamajiabj-boop/sts-cli/releases/tag/windows-20260921-230502)，下载 ZIP 并完整解压，双击 `SpireAssistant.exe`。运行环境已包含，无需安装 Python、Git 或开发工具。

需要自行拥有游戏，并通过 Steam 安装 ModTheSpire 和 BaseMod。详细安装、手动选择目录、实时顾问、自动游玩及战报入口见 [使用说明](docs/WINDOWS_ASSISTANT_README.md)。朋友可以从发布页下载完整 ZIP，无需开发环境。

## 当前版本

- 中文侧栏界面、大字建议、可选精简置顶窗口；已修复本机 125% 缩放下的 DPI 模糊。
- 顾问模式仅读取快照，与自动游玩互斥；状态变化、结算或断线后撤下旧建议。
- 已发布 EXE 的完整回归 2947 项通过，并已在源码目录之外启动验证；整理后的源码回归为 2948 项。
- 玩家已确认战斗、手动操作刷新、奖励和地图正常。商店、篝火、完整卡牌选择、退出游戏断线等实机项目仍待验收；不将回放等同于实机验证。

详见 [测试记录与已知限制](docs/ASSISTANT_TEST_RECORD.md)、[第三方分发说明](docs/THIRD_PARTY_NOTICES.md) 和 [阶段进度](docs/ASSISTANT_PROGRESS.md)。

## 源码与构建

本仓库首次上传为已测试工作区的完整源码快照，包含既有策略修改及其回归测试。构建入口为 `build_windows.py`，准备条件和命令见 [从源码构建](docs/WINDOWS_ASSISTANT_README.md#从源码构建)。CommunicationMod Java 源码位于 `src/CommunicationMod-1.2.1`，EXE 启动器位于 `packaging/Launcher.cs`。

游戏、游戏运行时、ModTheSpire 和 BaseMod 不随包分发。第三方代码的许可证保留在各源码目录及 `packaging/licenses`。本版本仅供本地分享，没有上传创意工坊。

## 仓库导航

| 位置 | 用途 |
| --- | --- |
| `assistant_*.py` | Windows 界面、路径发现、只读顾问及运行管理 |
| `build_windows.py`、`packaging/` | Windows EXE 构建入口、启动器与许可 |
| `tests/` | 项目回归测试 |
| `test_fixtures/` | 回归样例及发布门禁固定案例 |
| `docs/` | 使用说明、测试记录、协议与进度 |
| `src/` | CommunicationMod 和 spirecomm 源码 |
| `viewer/` | 本地战报查看器 |
| `knowledge/`、`prompts/` | 游戏知识与可选远程模型提示 |
| `experiments/` | 独立实验及研究记录 |
| `scripts/` | 工作区维护工具 |
| `dist/` | 当前本地分享包（不提交 Git） |

开发时从仓库根目录运行 `python -m unittest discover -p "test_*.py"`；单项测试使用 `python -m unittest tests.test_assistant -v`。完整布局、归档恢复和本地分支说明见 [仓库维护说明](docs/REPOSITORY_LAYOUT.md)。

[公开发布检查记录](docs/PUBLICATION_REVIEW.md)
