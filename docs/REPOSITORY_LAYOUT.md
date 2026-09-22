# 仓库布局与维护

## 开发入口

- Windows 助手：`python assistant_app.py`。
- 完整回归：在仓库根目录执行 `python -m unittest discover -p "test_*.py"`。
- 顾问专项：`python -m unittest tests.test_assistant -v`。
- 构建：`python build_windows.py --python-home <Python目录> --jdk <JDK8目录> --game-dir <游戏目录>`。详细准备见 [使用说明](WINDOWS_ASSISTANT_README.md#从源码构建)。
- Java、顾问和自动模式的实际游戏验证仍遵守根目录 AGENTS.md。整理与回归不会启动游戏。

测试模块集中在 `tests/`，共享测试辅助对象使用 `tests.test_*` 导入。`test_fixtures/` 同时被生产回放和发布门禁读取，保留原位置。`viewer/test_viewer.py` 由顾问测试入口纳入完整回归；`experiments/` 的实验测试继续单独运行。

运行模块留在根目录：桥接、启动器、状态文件及发布构建依赖统一根路径。`state.json`、`command.json`、发布门禁 JSON、logs 和个人 .env 等本地数据按原协议保留；Git 忽略它们，不要手工搬动这些有效状态文件。

## 产物归档

维护工具默认将旧 Java 构建、临时脚本、历史 JAR、备份 JSON 和截图，以及旧版本发布包，归入 `.local-archive/<时间>/` 并保留 `moves.json`。用户已于 2026-09-22 要求永久清理上一批归档：该批文件已删除，旧 moves.json 只作为删除记录保留，不能再用于恢复。后续新归档仍可按下述命令恢复。`.tools/` 保留必要工具链、构建日志和验收证据；重复编译结果和一次性修改脚本已清除。

关闭游戏、助手和控制器后，可执行：

```powershell
python scripts/archive_workspace.py
python scripts/archive_workspace.py --apply
python scripts/archive_workspace.py --restore .local-archive/<时间>/moves.json
```

第一条只预览；第二条移动明确列出的旧产物；第三条恢复原路径，遇到同名文件会停止而不会覆盖。工具保护当前 `CommunicationMod.jar`、原始最新分享包、密钥、有效状态、日志及存档。当前分享包由 `dist/latest-assistant-release.json` 指定，标记无效时拒绝归档发布目录。归档不用于释放磁盘空间；确认无用前保留它。

## 分支与发布

公开版本维护分支为 `publish/public`，对应 GitHub 的 `main`。原本地 `main` 和历史发布分支保留原有历史；包含个人提交身份的旧远程另存为私有历史备份，不随公开仓库分发。开始整理时逐文件确认工作区的 264 个源码文件与已发布提交一致，保存源码 ZIP 和旧索引后对齐分支，未丢弃用户修改。

已发布的 `windows-20260921-230502` 标签与 ZIP 保持不变。它的包内说明仍在 ZIP 根目录；GitHub 最新源码中的说明现位于 `docs/`。测试与文档迁移纳入源码冻结校验，旧源码凭据不会被当作新工作区的有效凭据；需要运行新版自动模式时按正常构建与门禁流程重新生成。

本机目录整理检查日志：`.tools/repository-cleanup-20260922/`；永久清理清单和结果：`.tools/permanent-cleanup-20260922/`。重复源码 ZIP、旧索引与未发布的构建验证包已清除；源码仍可通过 Git 提交追溯。分享给朋友始终使用 Release 的原始 ZIP，不发送本地运行目录。
