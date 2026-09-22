# 连续选牌界面的关闭屏障死锁（2026-09-20）

## 实战证据

原验证批次决策哈希 `db6bf3937abeb44f`、控制器哈希 `6f8a430f9e54112d`，只完成一局正常终局：`78d0fc13-e59e-4119-bddc-dc0a63d66d5e`，战士 Act 2 F24 死亡。第二局 `9c351739-22f7-4189-a7c6-3ad5eafccb80` 是运行失败，不能算作正常游戏死亡或策略胜率样本。

第二局猎手在 Act 2 F33 收藏家、T5 使用 Burst 后使用 Survivor：

| autoplay.log 行号 | before_seq → after_seq | 动作 |
|---:|---|---|
| 636 | 809869 → 809870 | play Survivor |
| 637 | 809870 → 809873 | choose |
| 638 | 809873 → 809874 | proceed |
| 645 | 809874 → 809878 | 第二次 choose |

以上日志位于 `logs/attempts/9c351739-22f7-4189-a7c6-3ad5eafccb80/`。`last-authoritative-state.json` 的 state_seq=809998：HAND_SELECT、DiscardAction、max_cards=1，Bite 已选、Grand Finale 留在手中，available_commands 有 confirm，但 ready_for_command=false。`controller.stderr.log` 报 120 次轮询后 HAND_SELECT 仍未就绪。该局没有 `run-result.json` 正常终局文件；运行错误与终止证据在 controller-exit-audit.json/run-audit.json 中。

## 因果链（高置信度）

日志直接证明连续选牌后确认界面长期不可操作。Java 内部等待计数没有被日志序列化，具体内部路径由当前源代码与可执行复现确定：

第一次选牌关闭 → `GameStateListener.hasDungeonStateChanged` 设置关闭等待计数 4 → 第二个选牌界面打开并发布稳定状态，但没有取消旧计数 → 下一次选择触发状态变化 → 旧关闭分支等待动作队列清空 → DiscardAction 又等待玩家确认 → 循环等待。

这属于协议状态机错误；不能据此认定 Fairy 修复导致第二幕策略退步。该错误与角色无关，任何关闭后迅速重开的战斗选择界面都可能触发。

## 修复与验证

`src/CommunicationMod-1.2.1/src/main/java/communicationmod/GameStateListener.java`：在战斗中新选择界面打开时清除前一界面的关闭屏障。新界面之后关闭，仍建立新的屏障。未放宽动作队列排空条件、未延长超时、未修改 Python 策略或绕过绑定协议。

删除 `test_communication_mod_runtime.py` 中仅检查关闭屏障源码关键词的测试，用 `test_game_state_listener.py` 取代。新测试编译并执行实际生产监听器；只替代游戏环境的数据容器，不复制监听器判断。旧代码在“重开选牌后应发布确认状态”的断言失败（`.tools/hand-select-before-tests.log`），修复后通过。还检查连续 HAND_SELECT/GRID、最后关闭仍等待抽牌队列、瞬时空队列仍保留四次更新屏障。该测试不是完整游戏集成回放，真实输入与 Java 补丁队列仍需后续实战验证。

使用 JDK8、实际 desktop-1.0.jar 与现有 mod 编译监听器；仓库 CommunicationMod.jar 只有 GameStateListener.class 内容改变。旧 jar、编译结果和前后 SHA256 在 `.tools/hand-select-boundary-20260920/`。本次尚未部署或重启正在停留的游戏；验证任务必须重新部署并核实实际加载版本。

- 完整 **2,919 项测试通过**，99.667 秒，`.tools/hand-select-full-tests.log`。
- 当前固定历史语料 6,968 条 DecisionCase 检查 clear，`experiments/results/hand-select-replay-20260920.json`。这不是对刚跑两局全部行为的重新认证，也不能代替本次 Java 行为测试。
- `git diff --check` 通过。
- 新决策哈希 `b1f1d2a38f9f0a95`，控制器哈希 `190411a7ac0d6fdb`。Java jar 也参与版本指纹，因此不能把旧批次与新版混为同一组六局。

独立复核无阻断后，原验证任务应重新执行正式预检、运行时绑定和冻结，再按 selector 建立新版本六局批次。保留旧失败证据；遇 P0、协议/运行阻断仍暂停并通知主任务。不能用目前一个正常死亡样本判断胜率变化。
