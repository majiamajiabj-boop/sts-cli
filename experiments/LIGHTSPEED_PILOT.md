# sts_lightspeed 离线接入试验

本试验不接管游戏，不修改 fast-policy-v5 的决策，不将模拟胜率作为心脏通关率。

## 固定来源与构建

- 候选架构：[AttemorySystem/spire-agent](https://github.com/AttemorySystem/spire-agent)，检查版本 `8fcaa767479f62aa2528d2e9be260de97172c6b0`。
- 模拟器：[Attemory/sts_lightspeed](https://github.com/Attemory/sts_lightspeed)，固定版本 `46e14e4adc23d2c1c738df8902a6f297109fee50`，与上述 spire-agent 子模块一致。
- JSON 子模块：`0b345b20c888f7dc8888485768e4bf9a6be29de0`。
- Windows 本地构建：CMake 4.4.3、Ninja 1.13.2、Zig 0.16.0，产物为 `battle-sim.exe`、`card-reward-eval.exe`。
- 上游源码在忽略目录 `.tools/`，未修改；许可证保留在各源码目录。本项目没有复制上游策略源码。

```powershell
./experiments/build_lightspeed.ps1
$py = 'python'
& $py -m unittest discover -s experiments -p 'test_*.py'
& $py experiments/lightspeed_probe.py --output experiments/results/new-unique-run
```

输出目录必须不存在，防止覆盖先前结果。每次记录源码提交、二进制哈希、输入哈希、原 attempt/state_seq 和搜索预算。完整输出在 `experiments/results/`。

## 第一轮结果

最近六局、共 2,661 个 play/end/potion 战斗决策进入接口检查：

| attempt | 角色 | 战斗快照数 | 历史快照直接导入 |
|---|---|---:|---|
| afcd85ad-38cd-4603-9c14-d6c5afb57a2a | 铁甲 | 507 | 缺敌人 move_id、回合计数 |
| 1151664c-3ab2-482b-b887-bfc3d9efd5a9 | 猎手 | 490 | 角色未完整支持，且缺字段 |
| 321ec432-20af-4d1b-b235-29bb5c8d0476 | 机器人 | 413 | 另缺生成球历史等字段 |
| f1a67c5c-d14e-41b9-8059-cd0982d2b817 | 铁甲 | 272 | 缺敌人 move_id、回合计数 |
| d71ce03e-1fc1-4dbf-aa1e-be8a6b026ee9 | 猎手 | 431 | 角色未完整支持，且缺字段 |
| f43cc68e-9ec8-448f-bb6e-05cc3e6e585f | 机器人 | 548 | 另缺生成球历史等字段 |

探针对铁甲、机器人每局固定选择第二幕第一个和最后一个已映射遭遇，使用永久牌组生成新战斗。
每场 4 个随机世界、每次决策最多 300 次模拟/40ms，最多 160 次决策。上游自检 12 项通过。

| attempt / 楼层 | 遭遇 | 模拟胜利 / 完成数 |
|---|---|---:|
| afcd85ad / 18 | Spheric Guardian | 4 / 4 |
| afcd85ad / 33 | Automaton | 4 / 4 |
| 321ec432 / 18 | Shell Parasite | 4 / 4 |
| 321ec432 / 22 | Centurion And Healer | 3 / 4 |
| f1a67c5c / 18 | Spheric Guardian | 4 / 4 |
| f1a67c5c / 33 | Champ | 2 / 4 |
| f43cc68e / 18 | Shell Parasite | 4 / 4 |
| f43cc68e / 33 | Automaton | 4 / 4 |

全部 32 次模拟达到终局，其中 29 次胜利。这个比例仅描述上述小样本模拟，不能外推整局或心脏胜率，也不能说明优于当前策略。
原始报告：`results/lightspeed-20260920-v3/report.json`。首次试跑识别到三处遭遇名称映射错误；保留失败输出，修正为模拟器的枚举后重跑，未修改模拟器规则。

## 为什么暂不接入实战

1. **日志不是完整的模拟状态。** `autoplay.py::_bounded_monster_snapshot` 丢弃原协议中的 move_id；`authoritative_state_snapshot` 丢弃战斗计数。不能从可见意图可靠还原所有怪物动作，也不能把缺失历史当作零。
2. **上游评估器有额外信息。** `apps/card-reward-eval.cpp::evaluateIndependentBattle` 用 `BattleScumSearcher2 searcher(battle)` 搜索，再把动作执行到同一个 `battle`；搜索掌握该模拟世界的隐藏抽牌顺序和 RNG。本次结果是有额外信息的参考基线，不是未知未来条件下的策略验证。
3. **重新生成遭遇不等于历史回放。** 复用了永久牌组、HP、遗物，但重抽开局和敌人参数；战斗开始后记录的遗物计数也可能与入场前不同。药水被禁用，还没有同一模拟世界下的本地策略对照。
4. **角色与机制覆盖尚未通过。** 猎手不在候选完整支持范围。机器人导入需要生成球计数、Emotion Chip 等额外状态。自检通过不代表所有原版机制都正确。

因此此次只有离线探针，无线上策略开关。不能通过缺字段补零、直接按显示名发命令，或跳过协议审计来强行上线。

## 下一阶段的明确入口

先保留完整战斗重建字段并逐步核对模拟器与真实游戏的一步转移；再将搜索中的未知抽牌/RNG 与实际执行世界隔离，对相同遭遇、相同执行种子做成组对照。构筑和路线共享模拟评估可以在这层可信后接入。

进入六局实战前需完成：必要状态无缺失；动作通过现有实例 ID 绑定；同预算比较本地策略；未覆盖机制明确拒绝；完整测试/回放/冻结通过。六局只能验证运行和明显退化，不能证明稳定胜率提升。

## 日志清理

`retain_recent_attempts.ps1` 默认预览，`-Apply` 执行。仅处理 `logs/attempts` 的旧目录及对应过期 `logs/start-acceptances`，执行前拒绝活动控制器和路径链接，执行后比对保留文件哈希。

2026-09-20 删除 205 个旧对局目录、203 个过期启动记录，释放 11,293.6 MiB；保留最近六局、关联启动记录和自包含的历史回归样本库。运行基础设施日志和既有未提交代码保留。

## 本地验证

完整项目单测 2,907 项通过；探针边界测试 6 项通过；清理后历史 DecisionCase 回放 6,968 例为 clear，未发现 issue、eligible unknown 或 unresolved。`git diff --check` 通过。完整单测产生的两个空测试目录（attempt-case、attempt-missing-receipt）已清理，探针只将 UUID 目录计为真实对局。

## 原任务独立复核

“分析低层数与通关失败原因”使用 8 worlds / 600 simulations / 80ms 对相同 8 场景进行了两次离线复测。两次均完成 64 次战斗，分别胜利 61、63 次；百夫长与治疗师分别 6/8、7/8，冠军分别 7/8、8/8，其余场景均 8/8。两组使用重叠随机世界，不能合并当作 128 个独立样本。同一输入、同一预算下仍有结果波动，时间上限和搜索随机性需要纳入后续对照。

复核确认：上游隐藏 RNG 信息、禁用药水、新生成遭遇、缺少本地策略对照均限制结论；目前不能声称实战或心脏胜率提高。复核未启动游戏。结果分别保存在 `results/lightspeed-independent-20260920/report.json` 与 `results/lightspeed-independent-20260920-rerun/report.json`。

## 后续实现

未知抽牌条件下的状态接口、三组离线对照、独立审查与上线门槛见 [BLIND_COMBAT_LAB.md](BLIND_COMBAT_LAB.md)。该阶段仍未启用线上搜索。
