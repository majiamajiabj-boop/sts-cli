// Offline experiment using the separately pinned MIT-licensed sts_lightspeed.
// No communication bridge, live game process, sockets, or command execution.
#include <algorithm>
#include <chrono>
#include <iostream>
#include <map>
#include <memory>
#include <random>
#include <sstream>
#include <set>
#include <stdexcept>
#include <nlohmann/json.hpp>
#include "convert/ProspectiveBattle.h"
#include "sim/search/ActionEnumerator.h"
#include "sim/search/RootActionPolicy.h"

using namespace sts;
using namespace sts::search;
using json = nlohmann::json;

// Upstream uses -3 for curses internally; CommunicationMod uses Java's -2.
static int protocolCost(const CardInstance &c, int value) {
    return c.getType() == CardType::CURSE && value == -3 ? -2 : value;
}

static void require(bool ok, const std::string &message) {
    if (!ok) throw std::runtime_error(message);
}

// Replace every future RNG stream, not merely the searcher's action RNG.
// Sort before shuffling: permuting the hidden execution pile cannot alter a
// sampled planning world. Preserve hand order and visible discard identities.
static BattleContext sampleWorld(const BattleContext &observed, uint64_t seed,
                                 const std::vector<int> &knownTop = {}) {
    BattleContext copy = observed;
    copy.seed = seed;
    copy.aiRng = Random(seed + 11);
    copy.cardRandomRng = Random(seed + 23);
    copy.miscRng = Random(seed + 37);
    copy.monsterHpRng = Random(seed + 53);
    copy.potionRng = Random(seed + 71);
    copy.shuffleRng = Random(seed + 97);
    std::vector<CardInstance> fixedTop;
    for (int id : knownTop) {
        auto &pile = copy.cards.drawPile;
        auto found = std::find_if(pile.begin(), pile.end(), [id](const auto &c) { return c.uniqueId == id; });
        require(found != pile.end(), "known topdeck absent from observed inventory");
        fixedTop.push_back(*found);
        pile.erase(found);
    }
    std::sort(copy.cards.drawPile.begin(), copy.cards.drawPile.end(),
        [](const auto &a, const auto &b) { return a.uniqueId < b.uniqueId; });
    std::mt19937_64 rng(seed);
    std::shuffle(copy.cards.drawPile.begin(), copy.cards.drawPile.end(), rng);
    // Native draw pile is bottom-first. Only the observed prefix is fixed;
    // every other card remains sampled independently of execution order.
    for (auto c = fixedTop.rbegin(); c != fixedTop.rend(); ++c) copy.cards.drawPile.push_back(*c);
    return copy;
}

static std::vector<search::Action> legal(const BattleContext &bc, bool allInstances = false) {
    require(bc.outcome == Outcome::UNDECIDED, "battle already ended");
    require(bc.inputState == InputState::PLAYER_NORMAL ||
        (bc.inputState == InputState::CARD_SELECT &&
         bc.cardSelectInfo.cardSelectTask == CardSelectTask::HEADBUTT),
        "unsupported decision screen (only normal play and Headbutt supported)");
    std::vector<search::Action> actions;
    enumerateActions(bc, ActionEnumerationOptions{}, true, actions);
    if (allInstances && bc.inputState == InputState::PLAYER_NORMAL) {
        for (int i=0; i<bc.cards.cardsInHand; ++i) {
            const auto &c = bc.cards.hand[i];
            for (int t=0; t<(c.requiresTarget() ? bc.monsters.monsterCount : 1); ++t) {
                search::Action a(ActionType::CARD,i,t);
                if (a.isValidAction(bc) && std::find(actions.begin(),actions.end(),a)==actions.end())
                    actions.push_back(a);
            }
        }
    }
    require(!actions.empty(), "no legal actions");
    return actions;
}

static json blindSearch(const BattleContext &execution, uint64_t planningSeed,
                        int worlds, int simulations, const std::string &allocation,
                        const std::vector<int> &knownTop) {
    require(worlds >= 1 && worlds <= 32 && simulations >= 1 && simulations <= 50000,
            "invalid bounded search budget");
    require(allocation == "balanced" || allocation == "adaptive_quarter",
            "unknown root allocation policy");
    const auto actions = legal(execution);
    const bool adaptive = allocation == "adaptive_quarter";
    require(!adaptive || simulations >= static_cast<int>(actions.size()),
            "budget cannot cover every root action");
    // Predeclared rule: at most a quarter of the budget for fair exploration,
    // but at least one sample per legal root. Remaining visits use upstream UCT.
    const int floor = adaptive ? std::max(1, simulations / (4 * static_cast<int>(actions.size()))) : 2048;
    json budgets = json::array();
    std::map<uint32_t, std::vector<RootActionThreadSample>> samples;
    for (auto a : actions) samples[a.bits] = {};
    for (int w = 0; w < worlds; ++w) {
        auto sampled = sampleWorld(execution, planningSeed + 1000003ULL * w, knownTop);
        std::vector<int> sampledKnownTop;
        for (std::size_t i=0; i<knownTop.size(); ++i)
            sampledKnownTop.push_back(sampled.cards.drawPile[sampled.cards.drawPile.size()-1-i].uniqueId);
        require(sampledKnownTop == knownTop, "sampled world lost observed topdeck");
        BattleScumSearcher2 searcher(sampled);
        searcher.allowPotions = false;
        searcher.balanceRootActions = !adaptive;
        searcher.minRootActionVisits = floor;
        searcher.search(simulations, 60000);
        require(searcher.stopReason != "time_budget",
                "search stopped before fixed simulation budget");
        int visits = 0, minimum = simulations, maximum = 0;
        for (const auto &edge : searcher.root.edges) {
            const int count = static_cast<int>(edge.simulationCount);
            visits += count; minimum = std::min(minimum, count); maximum = std::max(maximum, count);
        }
        budgets.push_back({{"world", w}, {"stop_reason", searcher.stopReason},
            { "root_visits", visits}, {"min_visits", minimum}, {"max_visits", maximum},
            {"known_topdeck", sampledKnownTop}});
        for (auto a : actions) {
            auto edge = std::find_if(searcher.root.edges.begin(), searcher.root.edges.end(),
                [a](const auto &e) { return e.action == a; });
            // Root dedup may merge equivalent cards. Missing edges are not losses.
            require(edge != searcher.root.edges.end(), "root action omitted by engine");
            samples[a.bits].push_back(sampleFromEdge(*edge));
        }
    }
    std::vector<RootActionCandidate> candidates;
    json evidence = json::array();
    for (auto a : actions) {
        auto agg = aggregateRootActionThreads(samples.at(a.bits));
        auto c = candidateFromAggregate(agg, execution, a.getActionType() == ActionType::END_TURN);
        candidates.push_back(c);
        evidence.push_back({{"token", a.bits}, {"visits", c.visits},
            {"winning_sampled_worlds", c.winningRngWorlds},
            {"mean_best_win_hp", c.meanBestWinEndHp}, {"mean_value", c.meanValue}});
    }
    int idx = selectRootActionWithEndTurnSafety(candidates);
    require(idx >= 0 && idx < static_cast<int>(actions.size()), "no selected root action");
    return {{"token", actions[idx].bits}, {"candidates", evidence},
        {"allocation", allocation}, {"root_action_count", actions.size()},
        {"configured_root_visit_floor", floor}, {"balance_root_actions", !adaptive}, {"world_budgets", budgets}};
}

// Verified against getMove bytecode in the installed desktop-1.0.jar.
static const char *visibleIntent(MonsterMoveId move) {
    switch (move) {
        case MMID::SPHERIC_GUARDIAN_ACTIVATE: return "DEFEND";
        case MMID::SPHERIC_GUARDIAN_HARDEN: return "ATTACK_DEFEND";
        case MMID::SPHERIC_GUARDIAN_ATTACK_DEBUFF:
        case MMID::THE_CHAMP_FACE_SLAP: return "ATTACK_DEBUFF";
        case MMID::THE_CHAMP_DEFENSIVE_STANCE:
        case MMID::BRONZE_AUTOMATON_BOOST: return "DEFEND_BUFF";
        case MMID::THE_CHAMP_TAUNT: return "DEBUFF";
        case MMID::THE_CHAMP_GLOAT:
        case MMID::THE_CHAMP_ANGER: return "BUFF";
        case MMID::BRONZE_AUTOMATON_STUNNED: return "STUN";
        case MMID::BRONZE_ORB_STASIS: return "STRONG_DEBUFF";
        case MMID::BRONZE_ORB_SUPPORT_BEAM: return "DEFEND";
        default: return isMoveAttack(move) ? "ATTACK" : "UNKNOWN";
    }
}

struct Lab {
    std::unique_ptr<BattleContext> battle;
    json source;
    bool rememberTopdeck = false;
    std::vector<int> knownTop; // top-first, derived from observed actions, never hidden order

    void executeObserved(search::Action action) {
        const int drawnBefore = battle->cardsDrawn;
        const auto type = action.getActionType();
        const bool selected = type == ActionType::SINGLE_CARD_SELECT &&
            battle->cardSelectInfo.cardSelectTask == CardSelectTask::HEADBUTT;
        const auto played = type == ActionType::CARD ?
            battle->cards.hand[action.getSourceIdx()].getId() : CardId::INVALID;
        const int autoTop = played == CardId::HEADBUTT && battle->cards.discardPile.size() == 1 ?
            battle->cards.discardPile[0].uniqueId : -1;
        if (rememberTopdeck && selected)
            knownTop.insert(knownTop.begin(), battle->cards.discardPile.at(action.getSelectIdx()).uniqueId);
        action.execute(*battle);
        if (!rememberTopdeck) return;
        // Enemy actions can remove cards (Stasis); random insertion can break
        // the known prefix. Forget conservatively at these observable events.
        if (type == ActionType::END_TURN || played == CardId::RECKLESS_CHARGE) {
            knownTop.clear();
            return;
        }
        const int drawn = battle->cardsDrawn - drawnBefore;
        require(drawn >= 0, "draw event counter moved backwards");
        knownTop.erase(knownTop.begin(), knownTop.begin() + std::min<std::size_t>(drawn, knownTop.size()));
        // Headbutt auto-selects a singleton discard. Infer identity from its
        // public relocation, and decline to infer through interleaved draws.
        if (autoTop >= 0 && drawn == 0 && battle->inputState == InputState::PLAYER_NORMAL) {
            const auto inPile = [autoTop](const auto &pile) {
                return std::any_of(pile.begin(), pile.end(), [autoTop](const auto &c) { return c.uniqueId == autoTop; });
            };
            if (inPile(battle->cards.drawPile) && !inPile(battle->cards.discardPile))
                knownTop.insert(knownTop.begin(), autoTop);
        }
    }
    std::map<std::pair<std::string,int>,json> catalog;
    uint64_t revision = 0;
    bool exposeExecutionDrawOrder = false;

    json card(const CardInstance &c) const {
        const auto id = std::string(cardStringIds[static_cast<int>(c.id)]);
        auto entry = catalog.find({id, c.getUpgradeCount()});
        json result;
        if (entry != catalog.end()) result = entry->second;
        else {
            require(id == "Burn" || id == "Dazed" || id == "Wound",
                    "missing observed card metadata: " + id);
            result = {{"id",id}, {"name",id}, {"type","STATUS"}, {"rarity","COMMON"},
                      {"base_damage",0}, {"base_block",0}, {"magic_number",0}};
        }
        result["uuid"] = std::to_string(c.uniqueId);
        result["card_instance_id"] = std::to_string(c.uniqueId);
        result["upgrades"] = c.getUpgradeCount();
        result["cost"] = c.isFreeToPlay(*battle) ? 0 : protocolCost(c, c.costForTurn);
        result["combat_cost"] = protocolCost(c, c.cost);
        result["has_target"] = c.requiresTarget();
        result["is_playable"] = c.canUseOnAnyTarget(*battle);
        result["exhausts"] = c.doesExhaust();
        result["ethereal"] = c.isEthereal();
        result["misc"] = c.specialData;
        // Card.damage is applyPowers damage before target-specific modifiers.
        auto untargeted = *battle;
        untargeted.monsters.arr[0] = Monster{};
        int baseDamage = result.value("base_damage", 0);
        if (c.id == CardId::PERFECTED_STRIKE)
            baseDamage = 6 + battle->cards.strikeCount * (c.upgraded ? 3 : 2);
        result["damage"] = c.getType() == CardType::ATTACK
            ? untargeted.calculateCardDamage(c, 0, baseDamage) : 0;
        int baseBlock = result.value("base_block", 0);
        result["block"] = baseBlock > 0 ? battle->calculateCardBlock(baseBlock) : 0;
        return result;
    }

    json view() const {
        require(bool(battle), "start required");
        const auto &bc = *battle;
        json out = {{"known_topdeck",knownTop}, {"revision",revision}, {"turn",bc.turn + 1}, {"hp",bc.player.curHp},
            {"outcome",bc.outcome == Outcome::PLAYER_VICTORY ? "win" :
                bc.outcome == Outcome::PLAYER_LOSS ? "loss" : "ongoing"}};
        if (bc.outcome != Outcome::UNDECIDED) return out;
        const auto actions = legal(bc, true);
        json g = source.at("game_state");
        g["seed"] = 0;  // never give the policy the simulator's execution seed
        g["potions"] = json::array();
        g["room_phase"] = "COMBAT";
        g["screen_type"] = "NONE";
        g["screen_state"] = json::object();
        g.erase("choice_list");
        g["is_screen_up"] = false;
        g["map"] = json::array();
        g["current_hp"] = bc.player.curHp;
        g["max_hp"] = bc.player.maxHp;
        for (auto &c : g["deck"]) c["uuid"] = c.value("card_instance_id", c.at("id").get<std::string>());
        json powers = json::array();
        for (int i = 1; i <= int(PlayerStatus::THE_BOMB); ++i) {
            auto status = static_cast<PlayerStatus>(i);
            if (!bc.player.hasStatusRuntime(status)) continue;
            int amount = 1;
            if (status == PlayerStatus::ARTIFACT || status == PlayerStatus::DEXTERITY ||
                status == PlayerStatus::FOCUS || status == PlayerStatus::STRENGTH ||
                bc.player.statusMap.count(status)) amount = bc.player.getStatusRuntime(status);
            powers.push_back({{"id",playerStatusIds[i]}, {"name",playerStatusIds[i]}, {"amount",amount}});
        }
        json cs = {{"turn",bc.turn + 1}, {"draw_pile_order_known",exposeExecutionDrawOrder}, {"cards_discarded_this_turn",bc.player.cardsDiscardedThisTurn},
            {"cards_played_this_turn",bc.player.cardsPlayedThisTurn},
            {"attacks_played_this_turn",bc.player.attacksPlayedThisTurn},
            {"skills_played_this_turn",bc.player.skillsPlayedThisTurn},
            {"powers_played_this_combat",bc.powersPlayedThisCombat},
            {"times_damaged",bc.player.timesDamagedThisCombat},
            {"lightning_channeled",bc.player.lightningChanneled},
            {"frost_channeled",bc.player.frostChanneled},
            {"emotion_chip_pending",bc.player.hasRelic<R::EMOTION_CHIP>() && bc.player.lastDamageTaken > 0},
            {"centennial_puzzle_used_this_combat",false},
            {"player",{{"current_hp",bc.player.curHp}, {"max_hp",bc.player.maxHp},
                {"energy",bc.player.energy}, {"block",bc.player.block}, {"powers",powers},
                {"orbs",json::array()}, {"max_orbs",0}}}};
        cs["hand"] = json::array();
        for (int i=0; i<bc.cards.cardsInHand; ++i) cs["hand"].push_back(card(bc.cards.hand[i]));
        for (auto pair : {std::make_pair("draw_pile", &bc.cards.drawPile),
                          std::make_pair("discard_pile", &bc.cards.discardPile),
                          std::make_pair("exhaust_pile", &bc.cards.exhaustPile)}) {
            cs[pair.first] = json::array();
            auto cards = *pair.second;
            if (std::string(pair.first) == "draw_pile" && !exposeExecutionDrawOrder)
                std::sort(cards.begin(), cards.end(), [](auto &a, auto &b){return a.uniqueId < b.uniqueId;});
            for (const auto &c : cards) cs[pair.first].push_back(card(c));
        }
        cs["monsters"] = json::array();
        for (int i=0; i<bc.monsters.monsterCount; ++i) {
            const auto &m = bc.monsters.arr[i];
            const auto id = monsterIdIds[static_cast<int>(m.id)];
            json mp = json::array();
            for (int j=1; j<=int(MonsterStatus::STASIS); ++j) {
                auto status = static_cast<MonsterStatus>(j);
                int amount = m.getStatusInternal(status);
                if (!amount || std::string(enemyStatusIds[j]) == "INVALID") continue;
                json p = {{"id",enemyStatusIds[j]}, {"name",enemyStatusIds[j]}, {"amount",amount}};
                if (status == MonsterStatus::STASIS && m.isDeadOrEscaped()) continue;
                if (status == MonsterStatus::STASIS) {
                    p["card"] = card(bc.cards.stasisCards[std::min(i,1)]);
                }
                mp.push_back(p);
            }
            auto damage = m.getMoveBaseDamage(bc);
            json mj = {{"id",id}, {"name",id}, {"current_hp",m.curHp}, {"max_hp",m.maxHp},
                {"block",m.block}, {"powers",mp}, {"half_dead",m.halfDead},
                {"is_gone",m.isDeadOrEscaped()}, {"intent",visibleIntent(m.moveHistory[0])},
                {"move_base_damage",damage.damage}, {"move_hits",damage.attackCount},
                {"move_adjusted_damage",m.calculateDamageToPlayer(bc,damage.damage)}};
            for (int move=0; move<128; ++move) {
                auto translated = getMonsterMoveFromId(m.id, move);
                if (translated != MonsterMoveId::INVALID && translated == m.moveHistory[0])
                    mj["move_id"] = move;
                if (translated != MonsterMoveId::INVALID && translated == m.moveHistory[1])
                    mj["last_move_id"] = move;
            }
            cs["monsters"].push_back(mj);
        }
        // Snapshot only supported relic counters. Other changing counters need
        // an explicit exporter before the cohort can be enlarged.
        for (auto &r : g["relics"]) {
            auto id = r.at("id").get<std::string>();
            if (id == "Shuriken") r["counter"] = bc.player.attacksPlayedThisTurn % 3;
            if (id == "Pocketwatch") r["counter"] = bc.player.cardsPlayedThisTurn;
        }
        g["combat_state"] = cs;
        out["game_state"] = g;
        out["select_task"] = bc.inputState == InputState::CARD_SELECT ? "HEADBUTT" : "NONE";
        out["actions"] = json::array();
        for (auto a : actions) {
            json aj = {{"token",a.bits}};
            if (a.getActionType() == ActionType::CARD) {
                aj["kind"] = "play";
                aj["card_uuid"] = std::to_string(bc.cards.hand[a.getSourceIdx()].uniqueId);
                aj["target"] = a.getTargetIdx();
            } else if (a.getActionType() == ActionType::END_TURN) aj["kind"] = "end";
            else {
                aj["kind"] = "select";
                aj["card_uuid"] = std::to_string(bc.cards.discardPile.at(a.getSelectIdx()).uniqueId);
            }
            out["actions"].push_back(aj);
        }
        return out;
    }

    json handle(const json &request) {
        auto op = request.at("op").get<std::string>();
        if (op == "start") {
            battle.reset();
            knownTop.clear();
            rememberTopdeck = request.value("remember_topdeck", false);
            catalog.clear();
            source = request.at("spec");
            exposeExecutionDrawOrder = request.value("expose_execution_draw_order",false);
            require(source.at("game_state").at("class") == "IRONCLAD", "Ironclad only");
            // Explicit pilot coverage; unknown mechanisms cannot silently join a comparison.
            static const std::set<std::string> supportedCards = {"Bash","Battle Trance","Bite","Cleave","Clothesline","Defend_R","Demon Form","Doubt","Headbutt","Immolate","Inflame","Metallicize","Offering","Perfected Strike","Pommel Strike","Power Through","Rage","Reckless Charge","Rupture","Second Wind","Shrug It Off","Strike_R","Thunderclap","Twin Strike"};
            static const std::set<std::string> supportedRelics = {"Blood Vial","Burning Blood","Ectoplasm","Empty Cage","Eternal Feather","Golden Idol","Gremlin Horn","MutagenicStrength","NeowsBlessing","Odd Mushroom","Paper Frog","Pocketwatch","Red Mask","Shuriken","Tiny Chest","Toy Ornithopter","TungstenRod"};
            for (const auto &c : source.at("game_state").at("deck"))
                require(supportedCards.count(c.at("id").get<std::string>()), "card outside pilot coverage");
            for (const auto &r : source.at("game_state").at("relics"))
                require(supportedRelics.count(r.at("id").get<std::string>()), "relic outside pilot coverage");
            require(source.at("game_state").at("potions").empty(), "pilot requires no potions");
            auto spec = evaluation::parseProspectiveBattleSpec(source);
            require(spec.targets.size() == 1 &&
                (spec.targets[0] == MonsterEncounter::SPHERIC_GUARDIAN ||
                 spec.targets[0] == MonsterEncounter::AUTOMATON ||
                 spec.targets[0] == MonsterEncounter::CHAMP), "encounter outside pilot coverage");
            // Local game bytecode Rage.<init> passes iconst_0 as energy cost.
            // The pinned upstream table omits RAGE from its zero-cost group.
            // Validate the whole supported deck so another silent mismatch
            // cannot become apparent policy performance in this lab.
            for (const auto &c : source.at("game_state").at("deck")) {
                auto parsed = evaluation::parseCardSpec(c, "deck");
                CardInstance native(parsed.card);
                const int expected = native.getId() == CardId::RAGE ? 0 : protocolCost(native, native.cost);
                require(c.contains("cost") && c.at("cost").is_number_integer(), "observed deck cost required");
                require(c.at("cost").get<int>() == expected,
                    "observed/native deck cost mismatch: " + c.at("id").get<std::string>());
                if (c.contains("combat_cost"))
                    require(c.at("combat_cost").is_number_integer() &&
                            c.at("combat_cost").get<int>() == expected,
                        "observed/native combat_cost mismatch: " + c.at("id").get<std::string>());
            }
            auto gc = evaluation::buildProspectiveGame(spec, std::nullopt);
            require(spec.targets.size() == 1, "exactly one target required");
            battle = std::make_unique<BattleContext>(evaluation::buildProspectiveBattle(
                gc, spec.targets.at(0), request.at("world").get<int>()));
            // Apply before either arm sees or executes any action. Supported
            // cards do not create new Rage instances during combat.
            const auto correctCost = [](auto &c) {
                if (c.getId() == CardId::RAGE) c.cost = c.costForTurn = 0;
            };
            for (int i=0; i<battle->cards.cardsInHand; ++i) correctCost(battle->cards.hand[i]);
            for (auto &c : battle->cards.drawPile) correctCost(c);
            for (auto &c : battle->cards.discardPile) correctCost(c);
            catalog.clear();
            for (const auto &c : source.at("game_state").at("deck"))
                catalog[{c.at("id"), c.value("upgrades",0)}] = c;
            revision = 0;
            return view();
        }
        require(bool(battle), "start required");
        if (op == "view") return view();
        if (op == "search" || op == "check_hidden_invariance") {
            const auto seed = request.at("planning_seed").get<uint64_t>();
            const auto worlds = request.value("worlds",4);
            const auto sims = request.value("simulations",300);
            const auto allocation = request.value("allocation", std::string("balanced"));
            auto originalView = view();
            auto chosen = blindSearch(*battle,seed,worlds,sims,allocation,knownTop);
            if (op == "check_hidden_invariance") {
                auto hiddenChanged = sampleWorld(*battle,seed+887733,knownTop);
                auto other = blindSearch(hiddenChanged,seed,worlds,sims,allocation,knownTop);
                require(chosen == other, "hidden future changed search output");
                require(view() == originalView, "search mutated execution state");
                chosen["invariant"] = true;
            }
            chosen["revision"] = revision;
            return chosen;
        }
        if (op == "step") {
            require(request.at("revision").get<uint64_t>() == revision, "stale action revision");
            search::Action a(request.at("token").get<uint32_t>());
            auto actions = legal(*battle, true);
            require(std::find(actions.begin(),actions.end(),a) != actions.end(), "illegal action token");
            executeObserved(a);
            ++revision;
            return view();
        }
        throw std::runtime_error("unknown offline operation");
    }
};

int main() {
    Lab lab;
    std::string line;
    while (std::getline(std::cin,line)) {
        try { std::cout << lab.handle(json::parse(line)).dump() << std::endl; }
        catch (const std::exception &e) {
            std::cout << json({{"error",e.what()}}).dump() << std::endl;
        }
    }
}
