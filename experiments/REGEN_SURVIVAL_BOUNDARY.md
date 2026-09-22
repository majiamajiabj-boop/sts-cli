# 再生与反伤胜利的共享生存结算（2026-09-20）

## 结论

修复了生产 `fast-policy-v5` 三角色共用回合模型中的不一致：无瓶中精灵时，模型没有输出稳定终局 HP，生存层仍用毛伤害判断致死，忽略攻击前再生；反伤结束战斗后，旧代码又只清除下一回合自伤数值，却保留了已经扣血或消耗瓶中精灵的结果。

这是机制正确性修复，不是又一次调分或引入原生深度搜索。代码提交 `539c439`；当前决策哈希 `9c7a692beb0f28da`，控制器哈希 `6f8a430f9e54112d`。既有未提交工作保留，本次提交只包含本轮增量。

## 因果链与反例

实际日志两局均有再生：

- `321ec432-20af-4d1b-b235-29bb5c8d0476`，F22，T19–23，state_seq 分别为 803826、803833、803842、803851、803858；日志 `autoplay.log` 第 566、570、575、580、584 行。回血依次为 5、4、3、2、1，下一回合 HP 依次为 40、44、45、37、38。
- `f43cc68e-9ec8-448f-bb6e-05cc3e6e585f`，F13，T1–2，state_seq 806477、806484；日志第 207、211 行。下一回合 HP 为 41、45。

这些记录直接证明再生在真实回合间改变 HP。不能据此说当时那几回合被错误判死，或它们单独造成最终死亡。六局历史决策哈希均为 `375c9d7614c6a626`，控制器为 `872a0c6fab20e2fd`；这里只把前后状态当作真实机制证据，不把旧决策归因于当前实现。

当前源码的独立复现进一步证明错误仍存在于修复前：5 HP、再生 5、敌人攻击 6、反伤 3 可击杀最后敌人。反伤分支已算出先回到 10 HP，因此允许角色活着反杀；同一方案的风险分层却为 tier 0（致死），稳定 HP 为 None。正确结果应为 4 HP、净掉血 1、存活。

逻辑链：回血进入反伤临时 HP → 未进入共享 TurnDamageOutcome 的普通分支 → 终端评分退回“6 点毛伤害 ≥ 5 HP” → 将可存活的续招错误排除。统一有序 HP 输出后，风险层与反伤阶段使用同一结果。

反例保留：再生只有 1 时，5+1−6=0，仍应判死且不反伤；满血 10、再生 5 不能预存溢出治疗，受 6 伤后仍是 4；手牌 Burn 已致死，后续再生不能救活。反伤胜利后不再进入 Brutality 的下一回合，也不能因此消耗瓶中精灵。

本机游戏字节码提供独立顺序依据：`RegenPower.atEndOfTurn` 将 `RegenAction` 入队；`RegenAction.update` 调用 `heal(amount, true)` 后递减 Regeneration。`GameActionManager.callEndOfTurnActions`、回合结束队列及现有真实 HP 记录共同用于核对阶段。证据输出位于 `.tools/regen-java-reference/`。未执行游戏动作，也未声称完整 Java 状态机已被认证。

## 改动

- `combat_predictor.projected_turn_outcome` 现在为普通分支也提供 initial/final HP、净变化和实际再生治疗量，保持毛伤害字段独立。
- `FastCombatPlanner._terminal_turn_score` 用稳定 HP 判生存和安全余量；风险预算扣除实际发生的再生，不能预支超过血量上限的治疗。
- `_projected_turn_outcome_with_attack_reactions` 在敌方攻击完成后、下一回合开始前判断战斗结束，删除事后拼接不一致结果的代码。
- 既有再生反伤测试补上 HP、净损失及生存分层断言；新增死亡、溢出治疗、Brutality 和 Fairy 的阶段边界检查。

## 真实回合验证

新增 `experiments/turn_boundary_audit.py`，只从日志读取状态，不调用控制器。核对 attempt/run/代码哈希/序号绑定、楼层、ready 状态和严格下一回合边界，再调用当前生产 `_best_plan(..., groups=[])` 的 END 候选。

修复前 510 个可比较回合中，121 个已有稳定 HP 输出，389 个未提供。修复后 **510/510 稳定 HP 与真实记录一致**。这不是“修正了 389 个错误数值”，也不是 510 次新对局胜利。68 个战斗结束或非战斗边界明确排除；它们包含战后恢复等额外阶段，不能混入下一回合 HP 检查。

| attempt_id | HP 匹配 | 明确排除 |
|---|---:|---:|
| 1151664c-3ab2-482b-b887-bfc3d9efd5a9 | 79 | 17 |
| 321ec432-20af-4d1b-b235-29bb5c8d0476 | 85 | 6 |
| afcd85ad-38cd-4603-9c14-d6c5afb57a2a | 107 | 2 |
| d71ce03e-1fc1-4dbf-aa1e-be8a6b026ee9 | 75 | 18 |
| f1a67c5c-d14e-41b9-8059-cd0982d2b817 | 54 | 1 |
| f43cc68e-9ec8-448f-bb6e-05cc3e6e585f | 110 | 24 |

报告：`experiments/results/turn-boundary-before-20260920/report.json` 与 `turn-boundary-release-20260920/report.json`。每行保留原始日志位置、state_seq、历史版本和当前预测；报告绑定输入日志及当前 Python 源码 SHA256。仅恢复原有 card_instance_id→uuid 别名；缺失的地图不参与 END 评价，未补造怪物历史或战斗计数。

## 验证与实战安排

- 665 项相关战斗检查通过；完整套件 **2,917 项通过**，耗时 103.728 秒，见 `.tools/regen-final-full-tests.log`。
- 当前哈希的 6,968 个 DecisionCase 检查 clear：2,324 直接审计、2,091 不适用、2,553 有解决证据，issue/unknown/unresolved 均为 0。这不是 6,968 次真实对局重放。
- `git diff --check` 通过。
- 原验证任务正在独立复核。确认无阻断后由该任务完成运行时绑定和正式冻结，再运行六局、仅 P0/协议/运行故障暂停；本轮未启用原生搜索。

### 验证命令

```powershell
python -m unittest discover -p 'test_*.py' -q
python experiments/turn_boundary_audit.py --require-match --output experiments/results/a-new-boundary-report
python decision_case_replay.py --decision-hash 9c7a692beb0f28da --write experiments/results/a-new-case-replay.json
```

仍不能承诺心脏击杀率提升。当前证据支持降低“再生可救却判死”和“战斗已结束却扣下一回合 HP/药水”的错误；整局收益需固定版本实战验证。

## 独立复核后的 Fairy 补充修复

原验证任务发现一个真实遗漏并阻断实战：1/100 HP、Burn 2、瓶中精灵、Thorns 3，对手 3 HP 攻击 6。公共预测器输出复活后 24 HP，反伤包装器却仍使用自行计算的 0 HP，遗漏击杀。六局尚未启动；该证据保存在 `experiments/results/regen-boundary-review-20260920.md`。这是改前已存在、首次修复没有覆盖的共享账本问题，不能归为某次真实对局已证实的失败原因。

提交 `a5e8a05` 删除反伤包装器重复的回合末伤害/回血和逐击扣血计算，改为接收公共预测器 `player_health_observer` 通知。通知发生在手牌伤害、自动复活、再生之后，以及每次敌方攻击的伤害/复活结算之后、反应之前。Fairy 的有序逐包结算保持原有语义，无 Fairy 的毛伤害指标保持不变。

新增回归覆盖 Burn 先触发 Fairy、攻击触发 Fairy 后反杀中止多段、复活后再生、复活耗尽再次死亡；对 Thorns/Flame Barrier 分别检查，并检查 Static Discharge 与 Painful Stabs。使用改前源码快照执行同一批新测试，得到 9 个断言失败、0 异常，证明它们能够检出原错误；当前全部通过。前一轮 510 个真实 HP 匹配没有揭露这一错误，因为仅最终玩家血量无法证明敌方血量和反应触发也正确。

最新版本：decision_hash `db6bf3937abeb44f`，controller_hash `6f8a430f9e54112d`。667 项相关检查、完整 2,919 项测试通过（98.625 秒，`.tools/fairy-reaction-full-tests.log`）；当前 6,968 条 DecisionCase 检查 clear，见 `experiments/results/fairy-reaction-replay-20260920.json`；真实 510 个可比 HP 边界仍匹配、68 明确排除，见 `experiments/results/fairy-reaction-boundary-20260920/report.json`。`git diff --check` 通过。

原任务正在复核补充修复；通过后按最新哈希完成正式预检、运行时绑定、冻结，继续原授权的恰好六局验证。旧哈希 `9c7a692beb0f28da` 不再作为本批运行版本。仍不能据此宣称通关率提高。
