"""Read-only advice from immutable CommunicationMod observer frames.

No bridge, stsctl, coordinator, controller or Action.execute is imported/called.
Every calculation uses a new agent: a recommendation is never treated as a play.
"""
import hashlib
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src/spirecomm-master"))
from spirecomm.ai.agent import SimpleAgent
from spirecomm.spire.game import Game


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Advice:
    token: str
    action: str
    target: str
    reason: str
    scene: str
    evidence: dict


SCENES = {"NONE": "战斗", "COMBAT_REWARD": "战斗奖励", "CARD_REWARD": "卡牌奖励",
          "BOSS_REWARD": "首领奖励", "MAP": "地图", "SHOP_SCREEN": "商店",
          "SHOP_ROOM": "商店入口", "REST": "篝火", "GRID": "卡牌选择",
          "HAND_SELECT": "手牌选择", "EVENT": "事件", "CHEST": "宝箱", "GAME_OVER": "结算"}
REASONS = {
    "campfire_survival_rest_floor": "当前生命低于策略的生存储备，优先在篝火恢复生命。",
    "map_lookahead_with_survival_constraints": "比较后续路线的战斗消耗与补给机会，优先满足当前生命的生存约束。",
    "collect_visible_combat_reward": "领取当前可见且可用的奖励，增加本局资源。",
    "ordered_turn_search": "比较当前可用出牌顺序，权衡伤害、格挡与后续回合收益。",
    "prevent_avoidable_hp_loss": "优先使用能降低本回合生命损失的行动。",
    "all_enemies_passively_doomed": "预测敌人会被已有持续效果击败，无需追加单体伤害。",
    "no_positive_marginal_action": "当前剩余行动的预期收益不足，建议结束回合。",
    "play_phase_exhausted": "当前可用出牌阶段已结束。",
    "zero_loss_attack_progress": "预测能保住本回合生命，同时推进输出。",
    "future_turn_energy_setup": "当前风险可控，优先建立后续回合能量收益。",
    "future_turn_defense_setup": "当前风险可控，优先建立后续回合防御。",
}


def frame_token(frame, now=None):
    now = time.time() if now is None else now
    if not isinstance(frame, dict) or frame.get("schema_version") != 1 or frame.get("mode") != "advisor":
        raise FrameError("状态来源不是只读顾问模式，请关闭游戏后从顾问入口重新启动。")
    stamp = frame.get("observed_at")
    if not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= 2.5:
        raise FrameError("连接已中断或状态已过期，旧建议已清除。")
    if not isinstance(frame.get("session"), str) or not frame["session"] or type(frame.get("state_seq")) is not int or frame["state_seq"] < 1:
        raise FrameError("状态缺少有效的会话或版本。")
    state = frame.get("state")
    if not isinstance(state, dict):
        raise FrameError("游戏状态格式不完整。")
    digest = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return f'{frame["session"]}:{frame["state_seq"]}:{frame.get("ready")}:{digest}'


def read_frame(path):
    try:
        path = Path(path)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise FrameError("状态文件过大，停止读取。")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FrameError("尚未连接到游戏，或状态正在更新。" if isinstance(exc, OSError) else "状态文件不完整，等待下一帧。") from exc


def _name(obj):
    return str(getattr(obj, "name", None) or getattr(obj, "card_id", None) or getattr(obj, "relic_id", None) or "未知")


def describe_action(action, game):
    kind = type(action).__name__
    target = "无"
    monster = getattr(action, "target_monster", None)
    if monster is None and getattr(action, "target_index", None) is not None:
        index = action.target_index
        monster = next((m for m in game.monsters if m.monster_index == index), None)
    if monster is not None:
        target = f"{_name(monster)}（敌人 {monster.monster_index + 1}，生命 {monster.current_hp}）"
    if kind == "PlayCardAction":
        card = action.card or game.hand[action.card_index]
        if card not in game.hand:
            raise FrameError("推荐卡牌已不在当前手牌。")
        return f"打出 {_name(card)}（手牌 {game.hand.index(card) + 1}）", target
    if kind == "PotionAction":
        potion = action.potion or game.potions[action.potion_index]
        return f"{'使用' if action.use else '丢弃'}药水 {_name(potion)}", target
    if kind == "CardSelectAction":
        labels = [f"{_name(c)}（位置 {game.screen.cards.index(c) + 1}）" for c in action.cards]
        return "选择：" + ("、".join(labels) or "不选择卡牌并确认"), "当前选择区"
    if kind == "ChooseMapNodeAction":
        return f"前往 {dict(R='篝火', M='普通战斗', E='精英', T='宝箱').get(action.node.symbol, {'$': '商店', '?': '未知事件'}.get(action.node.symbol, action.node.symbol))} 节点（列 {action.node.x + 1}，层 {action.node.y + 1}）", "地图"
    if kind == "ChooseMapBossAction":
        return "前往首领", "地图首领节点"
    if kind == "RestAction":
        labels = {"REST": "休息回血", "SMITH": "升级卡牌", "RECALL": "取红宝石钥匙", "LIFT": "举重", "DIG": "挖掘", "TOKE": "移除卡牌"}
        return labels.get(action.rest_option.name, action.rest_option.name), "篝火"
    if kind in {"BuyCardAction", "BuyRelicAction", "BuyPotionAction", "BossRewardAction", "CardRewardAction"}:
        obj = next((getattr(action, key, None) for key in ("card", "relic", "potion") if getattr(action, key, None) is not None), None)
        return ("购买 " if kind.startswith("Buy") else "领取 ") + (_name(obj) if obj else "歌唱碗：增加最大生命"), "当前商品/奖励"
    if kind == "BuyPurgeAction":
        return "购买删牌服务", "商店"
    if kind == "CombatRewardAction":
        reward = action.combat_reward
        labels = {"GOLD": "金币", "CARD": "卡牌", "POTION": "药水", "RELIC": "遗物", "SAPPHIRE_KEY": "蓝宝石钥匙", "STOLEN_GOLD": "取回金币"}
        return "领取 " + labels.get(reward.reward_type.name, reward.reward_type.name), f"奖励 {game.screen.rewards.index(reward) + 1}"
    labels = {"EndTurnAction": "结束回合", "ProceedAction": "继续 / 确认", "CancelAction": "跳过 / 返回",
              "ChooseShopkeeperAction": "进入商店", "OpenChestAction": "打开宝箱", "OptionalCardSelectConfirmAction": "确认已选卡牌", "StateAction": "等待游戏状态更新"}
    if kind in labels:
        return labels[kind], target
    if hasattr(action, "choice_index"):
        index = action.choice_index
        label = action.name or (game.choice_list[index] if 0 <= index < len(game.choice_list) else f"选项 {index + 1}")
        return "选择 " + str(label), f"当前选项 {index + 1}"
    raise FrameError("该场景暂不支持可靠建议，请手动操作。")


def recommend(frame):
    token = frame_token(frame)
    state = frame["state"]
    if not state.get("in_game"):
        raise FrameError("已连接；请手动进入对局。")
    if frame.get("ready") is not True:
        raise FrameError("游戏正在结算，等待稳定状态。")
    raw = state.get("game_state") or {}
    if raw.get("class") not in {"IRONCLAD", "THE_SILENT", "DEFECT"}:
        raise FrameError("目前支持铁甲战士、静默猎手和故障机器人；此角色暂无建议。")
    game = Game.from_json(raw, state.get("available_commands") or [])
    agent = SimpleAgent(chosen_class=game.character, goal_mode="UNLOCK", macro_advisor=None)
    action = agent.get_next_action_in_game(game)
    title, target = describe_action(action, game)
    details = dict(agent.last_noncombat_decision or agent.combat_planner.last_decision or {})
    reason = REASONS.get(details.get("reason"), "")
    if not reason:
        reason = ("本地策略根据当前卡组、生命、金币与可选项比较收益。" if game.choice_available else "本地策略根据当前可用行动与敌人状态作出判断。")
    facts = [f"生命 {game.current_hp}/{game.max_hp}", f"金币 {game.gold}"]
    for key, label in (("projected_attack_hp_loss", "预测攻击失血"), ("projected_end_turn_hp_loss", "预测回合结束失血"), ("plan_score", "方案评分")):
        if isinstance(details.get(key), (int, float)):
            facts.append(f"{label} {details[key]:g}")
    reason += "\n" + "；".join(facts) + "。"
    selected = next((row for row in details.get("candidates", []) if isinstance(row, dict) and row.get("id") == details.get("chosen_id")), None)
    if selected:
        consequences = selected.get("consequences") or {}
        benefits = []
        for key, label in (("hp_delta", "生命变化"), ("gold_delta", "金币变化"), ("path_survival_risk", "路线风险估计")):
            value = consequences.get(key)
            if isinstance(value, (int, float)) and value:
                benefits.append(f"{label} {value:+g}")
        if isinstance(selected.get("score"), (int, float)):
            benefits.append(f"本地评分 {selected['score']:g}")
        if benefits:
            reason += "\n" + "；".join(benefits) + "。"
    if details.get("reason"):
        reason += "\n策略依据：" + str(details["reason"])
    return Advice(token, title, target, reason, SCENES.get(raw.get("screen_type"), str(raw.get("screen_type"))), details)


class AdviceSession:
    """UI-owned state; worker results cannot resurrect invalidated advice."""
    def __init__(self, calculate=recommend):
        self.calculate = calculate
        self.token = None
        self.advice = None
        self.status = "等待连接"
        self.frame = None
        self.busy = False
        self.completed = queue.Queue()
        self.attempted = None

    def invalidate(self, reason):
        self.token = self.frame = self.advice = self.attempted = None
        self.status = reason

    def observe(self, frame, now=None):
        try:
            token = frame_token(frame, now)
            if not frame["state"].get("in_game"):
                raise FrameError("已连接；请手动进入对局。")
            if frame.get("ready") is not True:
                raise FrameError("游戏正在结算，旧建议已清除。")
        except FrameError as exc:
            self.invalidate(str(exc))
            return
        if token != self.token:
            self.token, self.frame = token, frame
            self.advice = None
            self.attempted = None
            self.status = "已连接 · 正在计算当前状态建议"

    def collect(self):
        while not self.completed.empty():
            token, result, error = self.completed.get_nowait()
            self.busy = False
            if token == self.token:
                self.advice = result
                self.status = error or "已连接 · 建议已更新（仅供手动操作）"

    def start_pending(self):
        if self.busy or self.frame is None or self.attempted == self.token:
            return
        frame, token = self.frame, self.token
        self.busy = True
        self.attempted = token
        def worker():
            try:
                result = self.calculate(frame)
                if result.token != token:
                    raise FrameError("计算结果不属于当前状态。")
                self.completed.put((token, result, None))
            except Exception as exc:
                self.completed.put((token, None, f"建议暂不可用：{exc}"))
        threading.Thread(target=worker, daemon=True, name="readonly-advice").start()

    def tick(self, path):
        try:
            self.observe(read_frame(path))
        except FrameError as exc:
            self.invalidate(str(exc))
        self.collect()
        self.start_pending()
