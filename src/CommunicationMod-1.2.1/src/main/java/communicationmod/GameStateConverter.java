package communicationmod;

import basemod.ReflectionHacks;
import com.google.gson.Gson;
import com.megacrit.cardcrawl.actions.GameActionManager;
import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.core.AbstractCreature;
import com.megacrit.cardcrawl.core.Settings;
import com.megacrit.cardcrawl.core.CardCrawlGame;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.events.AbstractEvent;
import com.megacrit.cardcrawl.map.MapEdge;
import com.megacrit.cardcrawl.map.MapRoomNode;
import com.megacrit.cardcrawl.monsters.AbstractMonster;
import com.megacrit.cardcrawl.monsters.EnemyMoveInfo;
import com.megacrit.cardcrawl.neow.NeowEvent;
import com.megacrit.cardcrawl.neow.NeowReward;
import com.megacrit.cardcrawl.orbs.AbstractOrb;
import com.megacrit.cardcrawl.potions.AbstractPotion;
import com.megacrit.cardcrawl.potions.PotionSlot;
import com.megacrit.cardcrawl.powers.AbstractPower;
import com.megacrit.cardcrawl.relics.AbstractRelic;
import com.megacrit.cardcrawl.relics.RunicDome;
import com.megacrit.cardcrawl.rewards.RewardItem;
import com.megacrit.cardcrawl.rooms.*;
import com.megacrit.cardcrawl.screens.DeathScreen;
import com.megacrit.cardcrawl.screens.GameOverScreen;
import com.megacrit.cardcrawl.screens.VictoryScreen;
import com.megacrit.cardcrawl.screens.select.GridCardSelectScreen;
import com.megacrit.cardcrawl.shop.ShopScreen;
import com.megacrit.cardcrawl.shop.StorePotion;
import com.megacrit.cardcrawl.shop.StoreRelic;
import com.megacrit.cardcrawl.ui.buttons.LargeDialogOptionButton;
import com.megacrit.cardcrawl.ui.panels.EnergyPanel;
import communicationmod.patches.GridCardSelectScreenPatch;
import communicationmod.patches.UpdateBodyTextPatch;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Collections;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class GameStateConverter {

    private static final Gson GSON = new Gson();
    private static final String WORLD_OF_GOOP_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.exordium.GoopPuddle";
    private static final String WORLD_OF_GOOP_SCREEN_CLASS =
            WORLD_OF_GOOP_EVENT_CLASS + "$CurScreen";
    private static final String ABSTRACT_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.AbstractEvent";
    private static final String CLERIC_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.exordium.Cleric";
    private static final String DESIGNER_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.shrines.Designer";
    private static final String DESIGNER_SCREEN_CLASS =
            DESIGNER_EVENT_CLASS + "$CurrentScreen";
    private static final String DESIGNER_OPTION_CLASS =
            DESIGNER_EVENT_CLASS + "$OptionChosen";
    private static final String CURSED_TOME_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.city.CursedTome";
    private static final String CURSED_TOME_SCREEN_CLASS =
            CURSED_TOME_EVENT_CLASS + "$CurScreen";
    private static final String MAUSOLEUM_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.city.TheMausoleum";
    private static final String MAUSOLEUM_SCREEN_CLASS =
            MAUSOLEUM_EVENT_CLASS + "$CurScreen";
    private static final String KNOWING_SKULL_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.city.KnowingSkull";
    private static final String KNOWING_SKULL_SCREEN_CLASS =
            KNOWING_SKULL_EVENT_CLASS + "$CurScreen";
    private static final String DEAD_ADVENTURER_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.exordium.DeadAdventurer";
    private static final String DEAD_ADVENTURER_SCREEN_CLASS =
            DEAD_ADVENTURER_EVENT_CLASS + "$CUR_SCREEN";
    private static final String SCRAP_OOZE_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.exordium.ScrapOoze";
    private static final String FACE_TRADER_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.shrines.FaceTrader";
    private static final String FACE_TRADER_SCREEN_CLASS =
            FACE_TRADER_EVENT_CLASS + "$CurScreen";
    private static final String DUPLICATOR_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.shrines.Duplicator";
    private static final String BONFIRE_EVENT_CLASS =
            "com.megacrit.cardcrawl.events.shrines.Bonfire";
    private static final String BONFIRE_SCREEN_CLASS =
            BONFIRE_EVENT_CLASS + "$CUR_SCREEN";
    private static final String[] FACE_TRADER_RELIC_IDS = {
            "CultistMask", "FaceOfCleric", "GremlinMask", "NlothsMask",
            "SsserpentHead",
    };
    private static final int KNOWING_SKULL_GOLD_REWARD = 90;
    private static final String[] CURSED_TOME_BOOK_IDS = {
            "Necronomicon", "Enchiridion", "Nilry's Codex",
    };

    /*
     * AbstractPlayer.saveFileExists() does substantially more than its name
     * suggests: it reads, decodes and deserializes the complete autosave to
     * prove that it is not corrupt.  A communication frame is emitted after
     * practically every card played, so doing that for all three characters
     * on every frame caused a synchronous disk/GC hitch in combat.
     *
     * Autosave validity is actionable only on the main menu (START/RESUME).
     * Refresh it there and retain that authoritative menu snapshot while a
     * run is active.  The first frame after attaching the mod directly to an
     * existing run still initializes the cache once for protocol completeness.
     */
    private static final EnumMap<AbstractPlayer.PlayerClass, Boolean> AUTOSAVE_VALIDITY =
            new EnumMap<>(AbstractPlayer.PlayerClass.class);

    /*
     * Power conversion used to call Class.getDeclaredFields() up to nine
     * times per power, per emitted combat frame.  Reflection returns a new
     * Field[] on every call.  Power classes do not change at runtime, so cache
     * their accessible declared-field table once.
     */
    private static final Map<Class<?>, Map<String, Field>> DECLARED_FIELDS =
            new ConcurrentHashMap<>();

    /* The dungeon map is immutable within an act in the base game. */
    private static Object cachedMapIdentity = null;
    private static int cachedMapSignature = 0;
    private static ArrayList<Object> cachedMapJson = null;

    private static void refreshAutosaveValidity() {
        if (CardCrawlGame.characterManager == null) {
            return;
        }
        for (AbstractPlayer.PlayerClass playerClass : new AbstractPlayer.PlayerClass[] {
                AbstractPlayer.PlayerClass.IRONCLAD,
                AbstractPlayer.PlayerClass.THE_SILENT,
                AbstractPlayer.PlayerClass.DEFECT
        }) {
            AUTOSAVE_VALIDITY.put(
                    playerClass,
                    CardCrawlGame.characterManager.getCharacter(playerClass).saveFileExists());
        }
    }

    private static void putAutosaveValidity(HashMap<String, Object> response, boolean isInGame) {
        if (CardCrawlGame.characterManager == null) {
            return;
        }
        if (!isInGame || AUTOSAVE_VALIDITY.size() < 3) {
            refreshAutosaveValidity();
        }
        response.put("ironclad_autosave_exists", AUTOSAVE_VALIDITY.get(AbstractPlayer.PlayerClass.IRONCLAD));
        response.put("silent_autosave_exists", AUTOSAVE_VALIDITY.get(AbstractPlayer.PlayerClass.THE_SILENT));
        response.put("defect_autosave_exists", AUTOSAVE_VALIDITY.get(AbstractPlayer.PlayerClass.DEFECT));
    }

    /**
     * Creates a JSON representation of the status of CommunicationMod that will be sent to the external process.
     * The JSON object returned contains:
     * - "available_commands" (list): A list of commands (strings) available to the user
     * - "ready_for_command" (boolean): Denotes whether the game state is stable and ready to receive a command
     * - "in_game" (boolean): True if in the main menu, False if the player is in the dungeon
     * - "game_state" (object): Present if in_game=True, contains the game state object returned by getGameState()
     * @return A string containing the JSON representation of CommunicationMod's status
     */
    public static String getCommunicationState() {
        HashMap<String, Object> response = new HashMap<>();
        response.put("available_commands", CommandExecutor.getAvailableCommands());
        response.put("ready_for_command", GameStateListener.isWaitingForCommand());
        boolean isInGame = CommandExecutor.isInDungeon();
        response.put("in_game", isInGame);
        response.put("fast_mode", Settings.FAST_MODE);
        // Unlock progress must be observable before START so the controller can
        // select only characters whose prerequisite victory is still missing.
        response.put("key_system_unlocked", Settings.isFinalActAvailable);
        response.put("ironclad_third_act_win", CardCrawlGame.playerPref.getBoolean(AbstractPlayer.PlayerClass.IRONCLAD.name() + "_WIN", false));
        response.put("silent_third_act_win", CardCrawlGame.playerPref.getBoolean(AbstractPlayer.PlayerClass.THE_SILENT.name() + "_WIN", false));
        response.put("defect_third_act_win", CardCrawlGame.playerPref.getBoolean(AbstractPlayer.PlayerClass.DEFECT.name() + "_WIN", false));
        putAutosaveValidity(response, isInGame);
        if(isInGame) {
            response.put("game_state", getGameState());
        }
        return GSON.toJson(response);
    }


    /**
     * Creates a JSON representation of the game state, which will be sent to the client.
     * Always present:
     * - "screen_name" (string): The name of the Enum representing the current screen (defined by Mega Crit)
     * - "is_screen_up" (boolean): The game's isScreenUp variable
     * - "screen_type" (string): The type of screen (or decision) that the user if facing (defined by Communication Mod)
     * - "screen_state" (object): The state of the current state, see getScreenState() (as defined by Communication Mod)
     * - "room_phase" (string): The phase of the current room (COMBAT, EVENT, etc.)
     * - "action_phase" (string): The phase of the action manager (WAITING_FOR_USER_INPUT, EXECUTING_ACTIONS)
     * - "room_type" (string): The name of the class of the current room (ShopRoom, TreasureRoom, MonsterRoom, etc.)
     * - "current_hp" (int): The player's current hp
     * - "max_hp" (int): The player's maximum hp
     * - "floor" (int): The current floor number
     * - "act" (int): The current act number
     * - "act_boss" (string): The name of the current Act's visible boss encounter
     * - "gold" (int): The player's current gold total
     * - "seed" (long): The seed used by the current game
     * - "class" (string): The player's current class
     * - "ascension_level" (int): The ascension level of the current run
     * - "relics" (list): A list of the player's current relics
     * - "deck" (list): A list of the cards in the player's deck
     * - "potions" (list): A list of the player's potions (empty slots are PotionSlots)
     * - "map" (list): The current dungeon map
     * Sometimes present:
     * - "current_action" (list): The class name of the action in the action manager queue, if not empty
     * - "combat_state" (list): The state of the combat (draw pile, monsters, etc.)
     * - "choice_list" (list): If the command is available, the possible choices for the choose command
     * @return A HashMap encoding the JSON representation of the game state
     */
    private static HashMap<String, Object> getGameState() {
        HashMap<String, Object> state = new HashMap<>();

        state.put("screen_name", AbstractDungeon.screen.name());
        state.put("is_screen_up", AbstractDungeon.isScreenUp);
        state.put("screen_type", ChoiceScreenUtils.getCurrentChoiceType());
        state.put("room_phase", AbstractDungeon.getCurrRoom().phase.toString());
        state.put("action_phase", AbstractDungeon.actionManager.phase.toString());
        if(AbstractDungeon.actionManager.currentAction != null) {
            state.put("current_action", AbstractDungeon.actionManager.currentAction.getClass().getSimpleName());
        }
        state.put("room_type", AbstractDungeon.getCurrRoom().getClass().getSimpleName());
        state.put("current_hp", AbstractDungeon.player.currentHealth);
        state.put("max_hp", AbstractDungeon.player.maxHealth);
        state.put("floor", AbstractDungeon.floorNum);
        state.put("act", AbstractDungeon.actNum);
        state.put("act_boss", AbstractDungeon.bossKey);
        state.put("gold", AbstractDungeon.player.gold);
        state.put("seed", Settings.seed);
        state.put("class", AbstractDungeon.player.chosenClass.name());
        state.put("ascension_level", AbstractDungeon.ascensionLevel);
        // Run identity and final-act facts are emitted explicitly so an
        // external controller never needs to infer them from UI text or a
        // previous frame.
        state.put("is_daily", Settings.isDailyRun);
        state.put("is_endless", Settings.isEndless);
        state.put("is_trial", Settings.isTrial);
        state.put("is_standard_run", !Settings.isDailyRun && !Settings.isEndless && !Settings.isTrial);
        state.put("key_system_unlocked", Settings.isFinalActAvailable);
        state.put("ironclad_third_act_win", CardCrawlGame.playerPref.getBoolean(AbstractPlayer.PlayerClass.IRONCLAD.name() + "_WIN", false));
        state.put("silent_third_act_win", CardCrawlGame.playerPref.getBoolean(AbstractPlayer.PlayerClass.THE_SILENT.name() + "_WIN", false));
        state.put("defect_third_act_win", CardCrawlGame.playerPref.getBoolean(AbstractPlayer.PlayerClass.DEFECT.name() + "_WIN", false));
        state.put("has_ruby_key", Settings.hasRubyKey);
        state.put("has_emerald_key", Settings.hasEmeraldKey);
        state.put("has_sapphire_key", Settings.hasSapphireKey);
        boolean runVictory = GameOverScreen.isVictory && ChoiceScreenUtils.getCurrentChoiceType() == ChoiceScreenUtils.ChoiceType.GAME_OVER;
        boolean heartDefeated = runVictory && AbstractDungeon.actNum == 4;
        state.put("run_victory", runVictory);
        state.put("heart_defeated", heartDefeated);
        if (heartDefeated) {
            state.put("final_boss_id", "CorruptHeart");
        }

        ArrayList<Object> relics = new ArrayList<>();
        for(AbstractRelic relic : AbstractDungeon.player.relics) {
            relics.add(convertRelicToJson(relic));
        }

        state.put("relics", relics);

        ArrayList<Object> deck = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.player.masterDeck.group) {
            deck.add(convertCardToJson(card));
        }

        state.put("deck", deck);

        ArrayList<Object> potions = new ArrayList<>();
        for(AbstractPotion potion : AbstractDungeon.player.potions) {
            potions.add(convertPotionToJson(potion));
        }

        state.put("potions", potions);

        state.put("map", getCachedMapJson());
        if(CommandExecutor.isChooseCommandAvailable()) {
            state.put("choice_list", ChoiceScreenUtils.getCurrentChoiceList());
        }
        if(AbstractDungeon.getCurrRoom().phase.equals(AbstractRoom.RoomPhase.COMBAT)) {
            state.put("combat_state", getCombatState());
        }
        state.put("screen_state", getScreenState());
        return state;
    }

    private static HashMap<String, Object> getRoomState() {
        AbstractRoom currentRoom = AbstractDungeon.getCurrRoom();
        HashMap<String, Object> state = new HashMap<>();
        if(currentRoom instanceof TreasureRoom) {
            state.put("chest_type", ((TreasureRoom)currentRoom).chest.getClass().getSimpleName());
            state.put("chest_open", ((TreasureRoom) currentRoom).chest.isOpen);
        } else if(currentRoom instanceof TreasureRoomBoss) {
            state.put("chest_type", ((TreasureRoomBoss)currentRoom).chest.getClass().getSimpleName());
            state.put("chest_open", ((TreasureRoomBoss) currentRoom).chest.isOpen);
        } else if(currentRoom instanceof RestRoom) {
            state.put("has_rested", currentRoom.phase == AbstractRoom.RoomPhase.COMPLETE);
            state.put("rest_options", ChoiceScreenUtils.getRestRoomChoices());
        }
        return state;
    }

    /**
     * This method removes the special text formatting characters found in the game.
     * These extra formatting characters are turned into things like colored or wiggly text in game, but
     * we would like to report the text without dealing with these characters.
     * @param text The text for which the formatting should be removed
     * @return The input text, with the formatting characters removed
     */
    private static String removeTextFormatting(String text) {
        text = text.replaceAll("~|@(\\S+)~|@", "$1");
        return text.replaceAll("#.|NL", "");
    }

    /**
     * Align already-reflected Neow rewards with the currently enabled buttons.
     *
     * The localized option label is used only as an exact join key.  It is not
     * emitted as mechanics evidence.  Reward order is deliberately ignored so
     * a presentation-only button reorder cannot change the semantic contract.
     * An empty result means that the current screen is ambiguous and callers
     * must omit contracts rather than guessing by index.
     */
    static ArrayList<HashMap<String, Object>> alignNeowContracts(
            int screenNum,
            ArrayList<String> enabledLabels,
            ArrayList<HashMap<String, Object>> reflectedRewards) {
        ArrayList<HashMap<String, Object>> aligned = new ArrayList<>();
        if (screenNum != 3) {
            if (enabledLabels.size() == 1) {
                HashMap<String, Object> contract = new HashMap<>();
                contract.put("contract_version", 1);
                contract.put("contract_kind", "NEOW_DIALOG_ADVANCE");
                contract.put("screen_num", screenNum);
                contract.put("resource_effect", "NONE");
                aligned.add(contract);
            }
            return aligned;
        }
        if (enabledLabels.isEmpty() || enabledLabels.size() != reflectedRewards.size()) {
            return aligned;
        }

        HashMap<String, HashMap<String, Object>> byExactLabel = new HashMap<>();
        for (HashMap<String, Object> evidence : reflectedRewards) {
            Object labelValue = evidence.get("option_label");
            Object contractValue = evidence.get("contract");
            if (!(labelValue instanceof String) || !(contractValue instanceof HashMap)
                    || byExactLabel.containsKey((String) labelValue)) {
                aligned.clear();
                return aligned;
            }
            @SuppressWarnings("unchecked")
            HashMap<String, Object> contract = (HashMap<String, Object>) contractValue;
            byExactLabel.put((String) labelValue, contract);
        }
        for (String label : enabledLabels) {
            HashMap<String, Object> contract = byExactLabel.remove(label);
            if (contract == null) {
                aligned.clear();
                return aligned;
            }
            aligned.add(new HashMap<>(contract));
        }
        if (!byExactLabel.isEmpty()) {
            aligned.clear();
        }
        return aligned;
    }

    static String canonicalNeowDrawbackDefKind(
            String drawbackKind, String reflectedKind) {
        if ("NONE".equals(drawbackKind)) {
            return null;
        }
        if (drawbackKind != null && drawbackKind.equals(reflectedKind)) {
            return reflectedKind;
        }
        throw new IllegalArgumentException(
                "Neow drawback definition does not match reward drawback");
    }

    /**
     * Build typed World of Goop option contracts from already-reflected
     * instance values.  Every shape argument is deliberate: callers must
     * prove the exact base-game class, private field types, INTRO enum, and
     * two-button surface before any mechanics claim is emitted.
     */
    static ArrayList<HashMap<String, Object>> buildWorldOfGoopContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> damageType,
            Object damageValue,
            Class<?> goldType,
            Object goldValue,
            Class<?> goldLossType,
            Object goldLossValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!WORLD_OF_GOOP_EVENT_CLASS.equals(eventClassName)
                || !WORLD_OF_GOOP_SCREEN_CLASS.equals(screenClassName)
                || !fieldsArePrivate
                || damageType != int.class
                || goldType != int.class
                || goldLossType != int.class
                || !(damageValue instanceof Integer)
                || !(goldValue instanceof Integer)
                || !(goldLossValue instanceof Integer)
                || (Integer) damageValue < 0
                || (Integer) goldValue < 0
                || (Integer) goldLossValue < 0) {
            return contracts;
        }

        if ("RESULT".equals(screenName) && buttonCount == 1) {
            // GoopPuddle replaces both consequence-bearing INTRO buttons
            // with one no-op leave/continue button after resolving either
            // branch.  Emit an exact typed no-delta contract so consumers do
            // not mistake this legitimate result surface for reflection
            // failure, while still avoiding localized button text.
            contracts.add(worldOfGoopContract(
                    0, "CONTINUE", new HashMap<String, Object>()));
            return contracts;
        }
        if (!"INTRO".equals(screenName) || buttonCount != 2) {
            return contracts;
        }

        HashMap<String, Object> gatherParameters = new HashMap<>();
        gatherParameters.put("gold_gain", goldValue);
        gatherParameters.put("hp_damage", damageValue);
        contracts.add(worldOfGoopContract(0, "GATHER", gatherParameters));

        HashMap<String, Object> leaveParameters = new HashMap<>();
        leaveParameters.put("gold_loss", goldLossValue);
        contracts.add(worldOfGoopContract(1, "LEAVE", leaveParameters));
        return contracts;
    }

    private static HashMap<String, Object> worldOfGoopContract(
            int originalButtonIndex,
            String optionKind,
            HashMap<String, Object> parameters) {
        HashMap<String, Object> contract = new HashMap<>();
        contract.put("contract_version", 1);
        contract.put("contract_kind", "BASE_GAME_EVENT_OPTION");
        contract.put("event_id", "World of Goop");
        contract.put("event_class", WORLD_OF_GOOP_EVENT_CLASS);
        contract.put("original_button_index", originalButtonIndex);
        contract.put("option_kind", optionKind);
        contract.put("parameters", parameters);
        return contract;
    }

    private static HashMap<String, Object> stagedEventContract(
            String eventId,
            String eventClass,
            String eventStage,
            int originalButtonIndex,
            String optionKind,
            HashMap<String, Object> instanceParameters,
            HashMap<String, Object> parameters) {
        HashMap<String, Object> contract = new HashMap<>();
        contract.put("contract_version", 1);
        contract.put("contract_kind", "BASE_GAME_EVENT_OPTION");
        contract.put("event_id", eventId);
        contract.put("event_class", eventClass);
        contract.put("event_stage", eventStage);
        contract.put("original_button_index", originalButtonIndex);
        contract.put("option_kind", optionKind);
        contract.put(
                "instance_parameters",
                new HashMap<String, Object>(instanceParameters));
        contract.put(
                "parameters", new HashMap<String, Object>(parameters));
        return contract;
    }

    /** Build The Cleric contracts only from the exact reflected instance. */
    static ArrayList<HashMap<String, Object>> buildClericContracts(
            String eventClassName,
            String screenOwnerClassName,
            int buttonCount,
            boolean effectFieldsPrivate,
            boolean screenFieldProtected,
            Class<?> screenNumType,
            Object screenNumValue,
            Class<?> purifyCostType,
            Object purifyCostValue,
            Class<?> healAmtType,
            Object healAmtValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!CLERIC_EVENT_CLASS.equals(eventClassName)
                || !ABSTRACT_EVENT_CLASS.equals(screenOwnerClassName)
                || !effectFieldsPrivate
                || !screenFieldProtected
                || screenNumType != int.class
                || purifyCostType != int.class
                || healAmtType != int.class
                || !(screenNumValue instanceof Integer)
                || !(purifyCostValue instanceof Integer)
                || !(healAmtValue instanceof Integer)) {
            return contracts;
        }
        int screenNum = (Integer) screenNumValue;
        int purifyCost = (Integer) purifyCostValue;
        int healAmt = (Integer) healAmtValue;
        if ((purifyCost != 50 && purifyCost != 75) || healAmt < 0) {
            return contracts;
        }

        HashMap<String, Object> instance = new HashMap<>();
        instance.put("heal_amount", healAmt);
        instance.put("heal_gold_cost", 35);
        instance.put("purify_cost", purifyCost);
        if (screenNum == 99 && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "The Cleric", CLERIC_EVENT_CLASS, "RESULT", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if (screenNum != 0 || buttonCount != 3) {
            return contracts;
        }

        HashMap<String, Object> heal = new HashMap<>();
        heal.put("gold_cost", 35);
        heal.put("heal_amount", healAmt);
        contracts.add(stagedEventContract(
                "The Cleric", CLERIC_EVENT_CLASS, "MAIN", 0,
                "HEAL", instance, heal));

        HashMap<String, Object> purify = new HashMap<>();
        purify.put("gold_cost_if_purgeable", purifyCost);
        purify.put("purge_select_count", 1);
        purify.put("selection_mode", "PLAYER_SELECT");
        contracts.add(stagedEventContract(
                "The Cleric", CLERIC_EVENT_CLASS, "MAIN", 1,
                "PURIFY", instance, purify));
        contracts.add(stagedEventContract(
                "The Cleric", CLERIC_EVENT_CLASS, "MAIN", 2,
                "LEAVE", instance, new HashMap<String, Object>()));
        return contracts;
    }

    /** Build Designer contracts from its realized construction-time rolls. */
    static ArrayList<HashMap<String, Object>> buildDesignerContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            String optionClassName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> adjustmentType,
            Object adjustmentValue,
            Class<?> cleanUpType,
            Object cleanUpValue,
            Class<?> adjustCostType,
            Object adjustCostValue,
            Class<?> cleanUpCostType,
            Object cleanUpCostValue,
            Class<?> fullServiceCostType,
            Object fullServiceCostValue,
            Class<?> hpLossType,
            Object hpLossValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!DESIGNER_EVENT_CLASS.equals(eventClassName)
                || !DESIGNER_SCREEN_CLASS.equals(screenClassName)
                || !DESIGNER_OPTION_CLASS.equals(optionClassName)
                || !fieldsArePrivate
                || adjustmentType != boolean.class
                || cleanUpType != boolean.class
                || adjustCostType != int.class
                || cleanUpCostType != int.class
                || fullServiceCostType != int.class
                || hpLossType != int.class
                || !(adjustmentValue instanceof Boolean)
                || !(cleanUpValue instanceof Boolean)
                || !(adjustCostValue instanceof Integer)
                || !(cleanUpCostValue instanceof Integer)
                || !(fullServiceCostValue instanceof Integer)
                || !(hpLossValue instanceof Integer)) {
            return contracts;
        }
        boolean adjustmentUpgradesOne = (Boolean) adjustmentValue;
        boolean cleanUpRemovesCards = (Boolean) cleanUpValue;
        int adjustCost = (Integer) adjustCostValue;
        int cleanUpCost = (Integer) cleanUpCostValue;
        int fullServiceCost = (Integer) fullServiceCostValue;
        int hpLoss = (Integer) hpLossValue;
        boolean exactCostTuple =
                (adjustCost == 40 && cleanUpCost == 60
                        && fullServiceCost == 90 && hpLoss == 3)
                || (adjustCost == 50 && cleanUpCost == 75
                        && fullServiceCost == 110 && hpLoss == 5);
        if (!exactCostTuple) {
            return contracts;
        }

        HashMap<String, Object> instance = new HashMap<>();
        instance.put("adjustment_upgrades_one", adjustmentUpgradesOne);
        instance.put("clean_up_removes_cards", cleanUpRemovesCards);
        instance.put("adjust_cost", adjustCost);
        instance.put("clean_up_cost", cleanUpCost);
        instance.put("full_service_cost", fullServiceCost);
        instance.put("hp_loss", hpLoss);
        if ("INTRO".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Designer", DESIGNER_EVENT_CLASS, "INTRO", 0,
                    "OPEN_SERVICES", instance,
                    new HashMap<String, Object>()));
            return contracts;
        }
        if ("DONE".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Designer", DESIGNER_EVENT_CLASS, "DONE", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if (!"MAIN".equals(screenName) || buttonCount != 4) {
            return contracts;
        }

        HashMap<String, Object> adjustment = new HashMap<>();
        adjustment.put("gold_cost", adjustCost);
        if (adjustmentUpgradesOne) {
            adjustment.put("upgrade_select_count", 1);
            adjustment.put("selection_mode", "PLAYER_SELECT");
        } else {
            adjustment.put("upgrade_max_count", 2);
            adjustment.put("selection_mode", "RANDOM_UP_TO_AVAILABLE");
        }
        contracts.add(stagedEventContract(
                "Designer", DESIGNER_EVENT_CLASS, "MAIN", 0,
                adjustmentUpgradesOne
                        ? "ADJUSTMENT_GRID_UPGRADE"
                        : "ADJUSTMENT_RANDOM_UPGRADE",
                instance, adjustment));

        HashMap<String, Object> cleanUp = new HashMap<>();
        cleanUp.put("gold_cost", cleanUpCost);
        cleanUp.put("selection_mode", "PLAYER_SELECT");
        if (cleanUpRemovesCards) {
            cleanUp.put("purge_select_count", 1);
        } else {
            cleanUp.put("transform_select_count", 2);
            cleanUp.put("transform_result", "RANDOM");
        }
        contracts.add(stagedEventContract(
                "Designer", DESIGNER_EVENT_CLASS, "MAIN", 1,
                cleanUpRemovesCards
                        ? "CLEAN_UP_GRID_PURGE"
                        : "CLEAN_UP_GRID_TRANSFORM",
                instance, cleanUp));

        HashMap<String, Object> fullService = new HashMap<>();
        fullService.put("gold_cost", fullServiceCost);
        fullService.put("purge_select_count", 1);
        fullService.put("random_upgrade_max_count", 1);
        fullService.put(
                "selection_mode", "PLAYER_SELECT_THEN_RANDOM_UP_TO_AVAILABLE");
        contracts.add(stagedEventContract(
                "Designer", DESIGNER_EVENT_CLASS, "MAIN", 2,
                "FULL_SERVICE", instance, fullService));

        HashMap<String, Object> punch = new HashMap<>();
        punch.put("hp_loss", hpLoss);
        contracts.add(stagedEventContract(
                "Designer", DESIGNER_EVENT_CLASS, "MAIN", 3,
                "PUNCH_AND_LEAVE", instance, punch));
        return contracts;
    }

    private static boolean isExactCursedTomeBookPool(
            ArrayList<String> pool) {
        if (pool == null || pool.isEmpty()) {
            return false;
        }
        if (pool.size() == 1 && "Circlet".equals(pool.get(0))) {
            return true;
        }
        int priorIndex = -1;
        for (String relicId : pool) {
            int foundIndex = -1;
            for (int index = 0;
                    index < CURSED_TOME_BOOK_IDS.length;
                    index += 1) {
                if (CURSED_TOME_BOOK_IDS[index].equals(relicId)) {
                    foundIndex = index;
                    break;
                }
            }
            if (foundIndex <= priorIndex) {
                return false;
            }
            priorIndex = foundIndex;
        }
        return true;
    }

    /** Build Cursed Tome stage contracts without advancing its RNG. */
    static ArrayList<HashMap<String, Object>> buildCursedTomeContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> finalDmgType,
            Object finalDmgValue,
            Class<?> damageTakenType,
            Object damageTakenValue,
            ArrayList<String> randomBookPool) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!CURSED_TOME_EVENT_CLASS.equals(eventClassName)
                || !CURSED_TOME_SCREEN_CLASS.equals(screenClassName)
                || !fieldsArePrivate
                || finalDmgType != int.class
                || damageTakenType != int.class
                || !(finalDmgValue instanceof Integer)
                || !(damageTakenValue instanceof Integer)
                || !isExactCursedTomeBookPool(randomBookPool)) {
            return contracts;
        }
        int finalDmg = (Integer) finalDmgValue;
        int damageTaken = (Integer) damageTakenValue;
        if ((finalDmg != 10 && finalDmg != 15) || damageTaken < 0) {
            return contracts;
        }
        boolean exactStageDamage =
                (("INTRO".equals(screenName)
                        || "PAGE_1".equals(screenName))
                        && damageTaken == 0)
                || ("PAGE_2".equals(screenName) && damageTaken == 1)
                || ("PAGE_3".equals(screenName) && damageTaken == 3)
                || ("LAST_PAGE".equals(screenName) && damageTaken == 6)
                || ("END".equals(screenName)
                        && (damageTaken == 0
                            || damageTaken == 9
                            || damageTaken == 6 + finalDmg));
        if (!exactStageDamage) {
            return contracts;
        }

        HashMap<String, Object> instance = new HashMap<>();
        instance.put("final_hp_loss", finalDmg);
        instance.put("damage_taken", damageTaken);
        instance.put(
                "random_relic_pool", new ArrayList<String>(randomBookPool));
        if ("INTRO".equals(screenName) && buttonCount == 2) {
            HashMap<String, Object> enter = new HashMap<>();
            enter.put("future_hp_loss_to_complete", 6 + finalDmg);
            enter.put("random_relic_count", 1);
            enter.put("reward_surface", "COMBAT_REWARD");
            enter.put("selection_mode", "UNIFORM_MISC_RNG");
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "INTRO", 0,
                    "ENTER_RANDOM_BOOK_CHAIN", instance, enter));
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "INTRO", 1,
                    "LEAVE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if ("PAGE_1".equals(screenName) && buttonCount == 1) {
            HashMap<String, Object> read = new HashMap<>();
            read.put("hp_loss", 1);
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "PAGE_1", 0,
                    "READ_PAGE_1", instance, read));
            return contracts;
        }
        if ("PAGE_2".equals(screenName) && buttonCount == 1) {
            HashMap<String, Object> read = new HashMap<>();
            read.put("hp_loss", 2);
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "PAGE_2", 0,
                    "READ_PAGE_2", instance, read));
            return contracts;
        }
        if ("PAGE_3".equals(screenName) && buttonCount == 1) {
            HashMap<String, Object> read = new HashMap<>();
            read.put("hp_loss", 3);
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "PAGE_3", 0,
                    "READ_PAGE_3", instance, read));
            return contracts;
        }
        if ("LAST_PAGE".equals(screenName) && buttonCount == 2) {
            HashMap<String, Object> complete = new HashMap<>();
            complete.put("hp_loss", finalDmg);
            complete.put("random_relic_count", 1);
            complete.put("reward_surface", "COMBAT_REWARD");
            complete.put("selection_mode", "UNIFORM_MISC_RNG");
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "LAST_PAGE", 0,
                    "COMPLETE_RANDOM_BOOK", instance, complete));
            HashMap<String, Object> stop = new HashMap<>();
            stop.put("hp_loss", 3);
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "LAST_PAGE", 1,
                    "STOP", instance, stop));
            return contracts;
        }
        if ("END".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Cursed Tome", CURSED_TOME_EVENT_CLASS, "END", 0,
                    "PROCEED", instance, new HashMap<String, Object>()));
        }
        return contracts;
    }

    /** Build The Mausoleum contracts from its exact private stage/percent. */
    static ArrayList<HashMap<String, Object>> buildMausoleumContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> percentType,
            Object percentValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!MAUSOLEUM_EVENT_CLASS.equals(eventClassName)
                || !MAUSOLEUM_SCREEN_CLASS.equals(screenClassName)
                || !fieldsArePrivate
                || percentType != int.class
                || !(percentValue instanceof Integer)
                || ((Integer) percentValue != 50
                    && (Integer) percentValue != 100)) {
            return contracts;
        }
        int curseProbabilityPercent = (Integer) percentValue;
        HashMap<String, Object> instance = new HashMap<>();
        instance.put("curse_probability_percent", curseProbabilityPercent);
        if ("INTRO".equals(screenName) && buttonCount == 2) {
            HashMap<String, Object> open = new HashMap<>();
            open.put("random_relic_count", 1);
            open.put(
                    "relic_selection_mode",
                    "RANDOM_TIER_THEN_SCREENLESS_RELIC");
            open.put("curse_card_id", "Writhe");
            open.put(
                    "curse_probability_percent", curseProbabilityPercent);
            contracts.add(stagedEventContract(
                    "The Mausoleum", MAUSOLEUM_EVENT_CLASS, "INTRO", 0,
                    "OPEN", instance, open));
            contracts.add(stagedEventContract(
                    "The Mausoleum", MAUSOLEUM_EVENT_CLASS, "INTRO", 1,
                    "LEAVE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if ("RESULT".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "The Mausoleum", MAUSOLEUM_EVENT_CLASS, "RESULT", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
        }
        return contracts;
    }

    /** Build Knowing Skull contracts from its exact private dynamic costs. */
    static ArrayList<HashMap<String, Object>> buildKnowingSkullContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> potionCostType,
            Object potionCostValue,
            Class<?> cardCostType,
            Object cardCostValue,
            Class<?> goldCostType,
            Object goldCostValue,
            Class<?> leaveCostType,
            Object leaveCostValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!KNOWING_SKULL_EVENT_CLASS.equals(eventClassName)
                || !KNOWING_SKULL_SCREEN_CLASS.equals(screenClassName)
                || !fieldsArePrivate
                || potionCostType != int.class
                || cardCostType != int.class
                || goldCostType != int.class
                || leaveCostType != int.class
                || !(potionCostValue instanceof Integer)
                || !(cardCostValue instanceof Integer)
                || !(goldCostValue instanceof Integer)
                || !(leaveCostValue instanceof Integer)) {
            return contracts;
        }
        int potionCost = (Integer) potionCostValue;
        int cardCost = (Integer) cardCostValue;
        int goldCost = (Integer) goldCostValue;
        int leaveCost = (Integer) leaveCostValue;
        if (potionCost <= 0 || cardCost <= 0
                || goldCost <= 0 || leaveCost <= 0) {
            return contracts;
        }

        HashMap<String, Object> instance = new HashMap<>();
        instance.put("potion_cost", potionCost);
        instance.put("card_cost", cardCost);
        instance.put("gold_cost", goldCost);
        instance.put("leave_cost", leaveCost);
        instance.put("gold_reward", KNOWING_SKULL_GOLD_REWARD);
        if ("INTRO_1".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Knowing Skull", KNOWING_SKULL_EVENT_CLASS,
                    "INTRO_1", 0, "OPEN_QUESTIONS", instance,
                    new HashMap<String, Object>()));
            return contracts;
        }
        if ("COMPLETE".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Knowing Skull", KNOWING_SKULL_EVENT_CLASS,
                    "COMPLETE", 0, "CONTINUE", instance,
                    new HashMap<String, Object>()));
            return contracts;
        }
        if (!"ASK".equals(screenName) || buttonCount != 4) {
            return contracts;
        }

        HashMap<String, Object> potion = new HashMap<>();
        potion.put("hp_loss", potionCost);
        potion.put("reward_count", 1);
        potion.put("reward_kind", "RANDOM_POTION");
        contracts.add(stagedEventContract(
                "Knowing Skull", KNOWING_SKULL_EVENT_CLASS, "ASK", 0,
                "TAKE_POTION", instance, potion));

        HashMap<String, Object> gold = new HashMap<>();
        gold.put("hp_loss", goldCost);
        gold.put("gold_gain", KNOWING_SKULL_GOLD_REWARD);
        contracts.add(stagedEventContract(
                "Knowing Skull", KNOWING_SKULL_EVENT_CLASS, "ASK", 1,
                "TAKE_GOLD", instance, gold));

        HashMap<String, Object> card = new HashMap<>();
        card.put("hp_loss", cardCost);
        card.put("reward_count", 1);
        card.put("reward_color", "COLORLESS");
        card.put("reward_rarity", "UNCOMMON");
        card.put("selection_mode", "RANDOM");
        contracts.add(stagedEventContract(
                "Knowing Skull", KNOWING_SKULL_EVENT_CLASS, "ASK", 2,
                "TAKE_CARD", instance, card));

        HashMap<String, Object> leave = new HashMap<>();
        leave.put("hp_loss", leaveCost);
        contracts.add(stagedEventContract(
                "Knowing Skull", KNOWING_SKULL_EVENT_CLASS, "ASK", 3,
                "LEAVE", instance, leave));
        return contracts;
    }

    /** Build Dead Adventurer contracts from its already-shuffled reward queue. */
    static ArrayList<HashMap<String, Object>> buildDeadAdventurerContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> numRewardsType,
            Object numRewardsValue,
            Class<?> encounterChanceType,
            Object encounterChanceValue,
            Class<?> rewardsType,
            Object rewardsValue,
            Class<?> enemyType,
            Object enemyValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!DEAD_ADVENTURER_EVENT_CLASS.equals(eventClassName)
                || !DEAD_ADVENTURER_SCREEN_CLASS.equals(screenClassName)
                || !fieldsArePrivate
                || numRewardsType != int.class
                || encounterChanceType != int.class
                || !ArrayList.class.equals(rewardsType)
                || enemyType != int.class
                || !(numRewardsValue instanceof Integer)
                || !(encounterChanceValue instanceof Integer)
                || !(rewardsValue instanceof ArrayList<?>)
                || !(enemyValue instanceof Integer)) {
            return contracts;
        }
        int numRewards = (Integer) numRewardsValue;
        int encounterChance = (Integer) encounterChanceValue;
        int enemy = (Integer) enemyValue;
        if (numRewards < 0 || numRewards > 3
                || (encounterChance != 25 + 25 * numRewards
                    && encounterChance != 35 + 25 * numRewards)
                || enemy < 0 || enemy > 2) {
            return contracts;
        }
        ArrayList<String> rewards = new ArrayList<>();
        for (Object reward : (ArrayList<?>) rewardsValue) {
            if (!(reward instanceof String)
                    || !("GOLD".equals(reward)
                         || "NOTHING".equals(reward)
                         || "RELIC".equals(reward))
                    || rewards.contains((String) reward)) {
                return contracts;
            }
            rewards.add((String) reward);
        }
        if (rewards.size() != 3 - numRewards) {
            return contracts;
        }
        String encounterId = new String[] {
                "3 Sentries", "Gremlin Nob", "Lagavulin Event",
        }[enemy];
        HashMap<String, Object> instance = new HashMap<>();
        instance.put("num_rewards", numRewards);
        instance.put("encounter_chance_percent", encounterChance);
        instance.put("remaining_rewards", new ArrayList<String>(rewards));
        instance.put("enemy_index", enemy);
        instance.put("encounter_id", encounterId);

        if ("INTRO".equals(screenName) && buttonCount == 2
                && !rewards.isEmpty()) {
            String reward = rewards.get(0);
            HashMap<String, Object> search = new HashMap<>();
            search.put("roll_min", 0);
            search.put("roll_max_inclusive", 99);
            search.put("encounter_roll_lt", encounterChance);
            search.put("encounter_id", encounterId);
            search.put("encounter_reward_gold_min", 25);
            search.put("encounter_reward_gold_max", 35);
            search.put("success_reward_kind", reward);
            search.put("success_gold_gain", "GOLD".equals(reward) ? 30 : 0);
            search.put("success_random_relic_count", "RELIC".equals(reward) ? 1 : 0);
            search.put(
                    "success_relic_selection_mode",
                    "RELIC".equals(reward)
                            ? "RANDOM_TIER_THEN_SCREENLESS_RELIC" : "NONE");
            contracts.add(stagedEventContract(
                    "Dead Adventurer", DEAD_ADVENTURER_EVENT_CLASS, "INTRO", 0,
                    "SEARCH", instance, search));
            contracts.add(stagedEventContract(
                    "Dead Adventurer", DEAD_ADVENTURER_EVENT_CLASS, "INTRO", 1,
                    "LEAVE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if ("FAIL".equals(screenName) && buttonCount == 1) {
            HashMap<String, Object> fight = new HashMap<>();
            fight.put("encounter_id", encounterId);
            fight.put("combat_reward_gold_min", 25);
            fight.put("combat_reward_gold_max", 35);
            contracts.add(stagedEventContract(
                    "Dead Adventurer", DEAD_ADVENTURER_EVENT_CLASS, "FAIL", 0,
                    "FIGHT", instance, fight));
            return contracts;
        }
        if (("SUCCESS".equals(screenName) || "ESCAPE".equals(screenName))
                && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Dead Adventurer", DEAD_ADVENTURER_EVENT_CLASS, screenName, 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
        }
        return contracts;
    }

    /** Build Scrap Ooze contracts from the exact repeat-attempt state. */
    static ArrayList<HashMap<String, Object>> buildScrapOozeContracts(
            String eventClassName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> chanceType,
            Object chanceValue,
            Class<?> damageType,
            Object damageValue,
            Class<?> totalDamageType,
            Object totalDamageValue,
            Class<?> screenNumType,
            Object screenNumValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!SCRAP_OOZE_EVENT_CLASS.equals(eventClassName)
                || !fieldsArePrivate
                || chanceType != int.class
                || damageType != int.class
                || totalDamageType != int.class
                || screenNumType != int.class
                || !(chanceValue instanceof Integer)
                || !(damageValue instanceof Integer)
                || !(totalDamageValue instanceof Integer)
                || !(screenNumValue instanceof Integer)) {
            return contracts;
        }
        int chance = (Integer) chanceValue;
        int damage = (Integer) damageValue;
        int totalDamage = (Integer) totalDamageValue;
        int screenNum = (Integer) screenNumValue;
        if (chance < 25 || chance > 105 || (chance - 25) % 10 != 0
                || damage < 3 || damage > 13
                || totalDamage < 0 || screenNum < 0 || screenNum > 1) {
            return contracts;
        }
        HashMap<String, Object> instance = new HashMap<>();
        instance.put("relic_chance_percent_displayed", chance);
        instance.put("damage", damage);
        instance.put("total_damage_dealt", totalDamage);
        instance.put("screen_num", screenNum);
        if (screenNum == 0 && buttonCount == 2) {
            HashMap<String, Object> reach = new HashMap<>();
            reach.put("hp_loss", damage);
            reach.put("roll_min", 0);
            reach.put("roll_max_inclusive", 99);
            reach.put("success_roll_min_inclusive", 99 - chance);
            reach.put("success_random_relic_count", 1);
            reach.put("success_relic_selection_mode", "RANDOM_TIER_THEN_SCREENLESS_RELIC");
            contracts.add(stagedEventContract(
                    "Scrap Ooze", SCRAP_OOZE_EVENT_CLASS, "MAIN", 0,
                    "REACH_INSIDE", instance, reach));
            contracts.add(stagedEventContract(
                    "Scrap Ooze", SCRAP_OOZE_EVENT_CLASS, "MAIN", 1,
                    "LEAVE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if (screenNum == 1 && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Scrap Ooze", SCRAP_OOZE_EVENT_CLASS, "RESULT", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
        }
        return contracts;
    }

    /** Build Face Trader contracts from its exact A0 prices and relic pool. */
    static ArrayList<HashMap<String, Object>> buildFaceTraderContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean screenPrivate,
            boolean effectFieldsPrivateStatic,
            Class<?> goldType,
            Object goldValue,
            Class<?> damageType,
            Object damageValue,
            ArrayList<String> randomFacePool) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!FACE_TRADER_EVENT_CLASS.equals(eventClassName)
                || !FACE_TRADER_SCREEN_CLASS.equals(screenClassName)
                || !screenPrivate || !effectFieldsPrivateStatic
                || goldType != int.class || damageType != int.class
                || !(goldValue instanceof Integer)
                || !(damageValue instanceof Integer)
                || randomFacePool == null || randomFacePool.isEmpty()) {
            return contracts;
        }
        int gold = (Integer) goldValue;
        int damage = (Integer) damageValue;
        if ((gold != 50 && gold != 75) || damage <= 0) {
            return contracts;
        }
        ArrayList<String> pool = new ArrayList<>();
        for (String relicId : randomFacePool) {
            boolean valid = "Circlet".equals(relicId);
            for (String baseId : FACE_TRADER_RELIC_IDS) {
                valid = valid || baseId.equals(relicId);
            }
            if (!valid || pool.contains(relicId)) {
                return contracts;
            }
            pool.add(relicId);
        }
        if (pool.contains("Circlet") && pool.size() != 1) {
            return contracts;
        }
        HashMap<String, Object> instance = new HashMap<>();
        instance.put("gold_reward", gold);
        instance.put("damage", damage);
        instance.put("random_face_pool", new ArrayList<String>(pool));
        if ("INTRO".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Face Trader", FACE_TRADER_EVENT_CLASS, "INTRO", 0,
                    "OPEN", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if ("MAIN".equals(screenName) && buttonCount == 3) {
            HashMap<String, Object> touch = new HashMap<>();
            touch.put("hp_loss", damage);
            touch.put("gold_gain", gold);
            contracts.add(stagedEventContract(
                    "Face Trader", FACE_TRADER_EVENT_CLASS, "MAIN", 0,
                    "TOUCH", instance, touch));
            HashMap<String, Object> trade = new HashMap<>();
            trade.put("random_relic_pool", new ArrayList<String>(pool));
            trade.put("random_relic_count", 1);
            trade.put("selection_mode", "UNIFORM_MISC_RNG_SHUFFLE_FIRST");
            contracts.add(stagedEventContract(
                    "Face Trader", FACE_TRADER_EVENT_CLASS, "MAIN", 1,
                    "TRADE", instance, trade));
            contracts.add(stagedEventContract(
                    "Face Trader", FACE_TRADER_EVENT_CLASS, "MAIN", 2,
                    "LEAVE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if ("RESULT".equals(screenName) && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Face Trader", FACE_TRADER_EVENT_CLASS, "RESULT", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
        }
        return contracts;
    }

    /** Build Duplicator contracts from its exact private screen number. */
    static ArrayList<HashMap<String, Object>> buildDuplicatorContracts(
            String eventClassName,
            int buttonCount,
            boolean screenPrivate,
            Class<?> screenNumType,
            Object screenNumValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!DUPLICATOR_EVENT_CLASS.equals(eventClassName)
                || !screenPrivate || screenNumType != int.class
                || !(screenNumValue instanceof Integer)) {
            return contracts;
        }
        int screenNum = (Integer) screenNumValue;
        if (screenNum < 0 || screenNum > 2) {
            return contracts;
        }
        HashMap<String, Object> instance = new HashMap<>();
        instance.put("screen_num", screenNum);
        if (screenNum == 0 && buttonCount == 2) {
            HashMap<String, Object> duplicate = new HashMap<>();
            duplicate.put("duplicate_select_count", 1);
            duplicate.put("selection_mode", "PLAYER_SELECT_CURRENT_DECK");
            contracts.add(stagedEventContract(
                    "Duplicator", DUPLICATOR_EVENT_CLASS, "MAIN", 0,
                    "DUPLICATE", instance, duplicate));
            contracts.add(stagedEventContract(
                    "Duplicator", DUPLICATOR_EVENT_CLASS, "MAIN", 1,
                    "LEAVE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if (screenNum == 2 && buttonCount == 1) {
            contracts.add(stagedEventContract(
                    "Duplicator", DUPLICATOR_EVENT_CLASS, "RESULT", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
        }
        // screenNum 1 is a transient update tick after the GRID returns and
        // is not a stable event choice surface.
        return contracts;
    }

    /** Build Bonfire Spirits contracts from its exact private stage. */
    static ArrayList<HashMap<String, Object>> buildBonfireContracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsPrivate,
            Class<?> cardSelectType,
            Object cardSelectValue) {
        ArrayList<HashMap<String, Object>> contracts = new ArrayList<>();
        if (!BONFIRE_EVENT_CLASS.equals(eventClassName)
                || !BONFIRE_SCREEN_CLASS.equals(screenClassName)
                || !fieldsPrivate
                || cardSelectType != boolean.class
                || !(cardSelectValue instanceof Boolean)
                || (Boolean) cardSelectValue
                || buttonCount != 1) {
            return contracts;
        }
        HashMap<String, Object> instance = new HashMap<>();
        instance.put("card_select", false);
        if ("INTRO".equals(screenName)) {
            contracts.add(stagedEventContract(
                    "Bonfire Elementals", BONFIRE_EVENT_CLASS, "INTRO", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
            return contracts;
        }
        if ("CHOOSE".equals(screenName)) {
            HashMap<String, Object> offer = new HashMap<>();
            offer.put("offer_select_count", 1);
            offer.put(
                    "selection_mode",
                    "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK");
            contracts.add(stagedEventContract(
                    "Bonfire Elementals", BONFIRE_EVENT_CLASS, "CHOOSE", 0,
                    "OFFER_CARD", instance, offer));
            return contracts;
        }
        if ("COMPLETE".equals(screenName)) {
            contracts.add(stagedEventContract(
                    "Bonfire Elementals", BONFIRE_EVENT_CLASS, "COMPLETE", 0,
                    "CONTINUE", instance, new HashMap<String, Object>()));
        }
        return contracts;
    }

    private static ArrayList<HashMap<String, Object>> getBaseGameEventContracts(
            AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        if (event == null) {
            return none;
        }
        String eventClassName = event.getClass().getName();
        if (WORLD_OF_GOOP_EVENT_CLASS.equals(eventClassName)) {
            return getWorldOfGoopEventContracts(event, buttonCount);
        }
        if (CLERIC_EVENT_CLASS.equals(eventClassName)) {
            return getClericEventContracts(event, buttonCount);
        }
        if (DESIGNER_EVENT_CLASS.equals(eventClassName)) {
            return getDesignerEventContracts(event, buttonCount);
        }
        if (CURSED_TOME_EVENT_CLASS.equals(eventClassName)) {
            return getCursedTomeEventContracts(event, buttonCount);
        }
        if (MAUSOLEUM_EVENT_CLASS.equals(eventClassName)) {
            return getMausoleumEventContracts(event, buttonCount);
        }
        if (KNOWING_SKULL_EVENT_CLASS.equals(eventClassName)) {
            return getKnowingSkullEventContracts(event, buttonCount);
        }
        if (DEAD_ADVENTURER_EVENT_CLASS.equals(eventClassName)) {
            return getDeadAdventurerEventContracts(event, buttonCount);
        }
        if (SCRAP_OOZE_EVENT_CLASS.equals(eventClassName)) {
            return getScrapOozeEventContracts(event, buttonCount);
        }
        if (FACE_TRADER_EVENT_CLASS.equals(eventClassName)) {
            return getFaceTraderEventContracts(event, buttonCount);
        }
        if (DUPLICATOR_EVENT_CLASS.equals(eventClassName)) {
            return getDuplicatorEventContracts(event, buttonCount);
        }
        if (BONFIRE_EVENT_CLASS.equals(eventClassName)) {
            return getBonfireEventContracts(event, buttonCount);
        }
        return none;
    }

    private static ArrayList<HashMap<String, Object>>
            getWorldOfGoopEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field damageField = eventClass.getDeclaredField("damage");
            Field goldField = eventClass.getDeclaredField("gold");
            Field goldLossField = eventClass.getDeclaredField("goldLoss");
            boolean fieldsArePrivate =
                    Modifier.isPrivate(screenField.getModifiers())
                    && Modifier.isPrivate(damageField.getModifiers())
                    && Modifier.isPrivate(goldField.getModifiers())
                    && Modifier.isPrivate(goldLossField.getModifiers());
            screenField.setAccessible(true);
            damageField.setAccessible(true);
            goldField.setAccessible(true);
            goldLossField.setAccessible(true);
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildWorldOfGoopContracts(
                    eventClass.getName(),
                    screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(),
                    buttonCount,
                    fieldsArePrivate,
                    damageField.getType(),
                    damageField.get(event),
                    goldField.getType(),
                    goldField.get(event),
                    goldLossField.getType(),
                    goldLossField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            // Class/field drift is an auditable unknown.  Never infer event
            // consequences from localized button text or ordinal position.
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>> getClericEventContracts(
            AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenNumField = AbstractEvent.class.getDeclaredField(
                    "screenNum");
            Field purifyCostField = eventClass.getDeclaredField("purifyCost");
            Field healAmtField = eventClass.getDeclaredField("healAmt");
            boolean effectFieldsPrivate =
                    Modifier.isPrivate(purifyCostField.getModifiers())
                    && Modifier.isPrivate(healAmtField.getModifiers());
            boolean screenFieldProtected =
                    Modifier.isProtected(screenNumField.getModifiers());
            screenNumField.setAccessible(true);
            purifyCostField.setAccessible(true);
            healAmtField.setAccessible(true);
            return buildClericContracts(
                    eventClass.getName(),
                    screenNumField.getDeclaringClass().getName(),
                    buttonCount,
                    effectFieldsPrivate,
                    screenFieldProtected,
                    screenNumField.getType(),
                    screenNumField.get(event),
                    purifyCostField.getType(),
                    purifyCostField.get(event),
                    healAmtField.getType(),
                    healAmtField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>> getDesignerEventContracts(
            AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("curScreen");
            Field optionField = eventClass.getDeclaredField("option");
            Field adjustmentField = eventClass.getDeclaredField(
                    "adjustmentUpgradesOne");
            Field cleanUpField = eventClass.getDeclaredField(
                    "cleanUpRemovesCards");
            Field adjustCostField = eventClass.getDeclaredField("adjustCost");
            Field cleanUpCostField = eventClass.getDeclaredField("cleanUpCost");
            Field fullServiceCostField = eventClass.getDeclaredField(
                    "fullServiceCost");
            Field hpLossField = eventClass.getDeclaredField("hpLoss");
            Field[] fields = {
                    screenField, optionField, adjustmentField, cleanUpField,
                    adjustCostField, cleanUpCostField, fullServiceCostField,
                    hpLossField,
            };
            boolean fieldsArePrivate = true;
            for (Field field : fields) {
                fieldsArePrivate = fieldsArePrivate
                        && Modifier.isPrivate(field.getModifiers());
                field.setAccessible(true);
            }
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildDesignerContracts(
                    eventClass.getName(),
                    screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(),
                    optionField.getType().getName(),
                    buttonCount,
                    fieldsArePrivate,
                    adjustmentField.getType(),
                    adjustmentField.get(event),
                    cleanUpField.getType(),
                    cleanUpField.get(event),
                    adjustCostField.getType(),
                    adjustCostField.get(event),
                    cleanUpCostField.getType(),
                    cleanUpCostField.get(event),
                    fullServiceCostField.getType(),
                    fullServiceCostField.get(event),
                    hpLossField.getType(),
                    hpLossField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<String> getCursedTomeRandomBookPool() {
        if (AbstractDungeon.player == null) {
            return null;
        }
        ArrayList<String> pool = new ArrayList<>();
        for (String relicId : CURSED_TOME_BOOK_IDS) {
            if (!AbstractDungeon.player.hasRelic(relicId)) {
                pool.add(relicId);
            }
        }
        if (pool.isEmpty()) {
            pool.add("Circlet");
        }
        return pool;
    }

    private static ArrayList<HashMap<String, Object>>
            getCursedTomeEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field finalDmgField = eventClass.getDeclaredField("finalDmg");
            Field damageTakenField = eventClass.getDeclaredField("damageTaken");
            boolean fieldsArePrivate =
                    Modifier.isPrivate(screenField.getModifiers())
                    && Modifier.isPrivate(finalDmgField.getModifiers())
                    && Modifier.isPrivate(damageTakenField.getModifiers());
            screenField.setAccessible(true);
            finalDmgField.setAccessible(true);
            damageTakenField.setAccessible(true);
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildCursedTomeContracts(
                    eventClass.getName(),
                    screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(),
                    buttonCount,
                    fieldsArePrivate,
                    finalDmgField.getType(),
                    finalDmgField.get(event),
                    damageTakenField.getType(),
                    damageTakenField.get(event),
                    getCursedTomeRandomBookPool());
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>> getMausoleumEventContracts(
            AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field percentField = eventClass.getDeclaredField("percent");
            boolean fieldsArePrivate =
                    Modifier.isPrivate(screenField.getModifiers())
                    && Modifier.isPrivate(percentField.getModifiers());
            screenField.setAccessible(true);
            percentField.setAccessible(true);
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildMausoleumContracts(
                    eventClass.getName(),
                    screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(),
                    buttonCount,
                    fieldsArePrivate,
                    percentField.getType(),
                    percentField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>>
            getKnowingSkullEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field potionCostField = eventClass.getDeclaredField("potionCost");
            Field cardCostField = eventClass.getDeclaredField("cardCost");
            Field goldCostField = eventClass.getDeclaredField("goldCost");
            Field leaveCostField = eventClass.getDeclaredField("leaveCost");
            Field[] fields = {
                    screenField, potionCostField, cardCostField,
                    goldCostField, leaveCostField,
            };
            boolean fieldsArePrivate = true;
            for (Field field : fields) {
                fieldsArePrivate = fieldsArePrivate
                        && Modifier.isPrivate(field.getModifiers());
                field.setAccessible(true);
            }
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildKnowingSkullContracts(
                    eventClass.getName(), screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(), buttonCount,
                    fieldsArePrivate,
                    potionCostField.getType(), potionCostField.get(event),
                    cardCostField.getType(), cardCostField.get(event),
                    goldCostField.getType(), goldCostField.get(event),
                    leaveCostField.getType(), leaveCostField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            // Missing/renamed fields are an auditable unknown.  Never infer
            // these repeatable HP prices from localized option text.
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>>
            getDeadAdventurerEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field numRewardsField = eventClass.getDeclaredField("numRewards");
            Field encounterChanceField = eventClass.getDeclaredField("encounterChance");
            Field rewardsField = eventClass.getDeclaredField("rewards");
            Field enemyField = eventClass.getDeclaredField("enemy");
            Field[] fields = {
                    screenField, numRewardsField, encounterChanceField,
                    rewardsField, enemyField,
            };
            boolean fieldsArePrivate = true;
            for (Field field : fields) {
                fieldsArePrivate = fieldsArePrivate
                        && Modifier.isPrivate(field.getModifiers());
                field.setAccessible(true);
            }
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildDeadAdventurerContracts(
                    eventClass.getName(), screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(), buttonCount,
                    fieldsArePrivate,
                    numRewardsField.getType(), numRewardsField.get(event),
                    encounterChanceField.getType(), encounterChanceField.get(event),
                    rewardsField.getType(), rewardsField.get(event),
                    enemyField.getType(), enemyField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>>
            getScrapOozeEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field chanceField = eventClass.getDeclaredField("relicObtainChance");
            Field damageField = eventClass.getDeclaredField("dmg");
            Field totalDamageField = eventClass.getDeclaredField("totalDamageDealt");
            Field screenNumField = eventClass.getDeclaredField("screenNum");
            Field[] fields = {
                    chanceField, damageField, totalDamageField, screenNumField,
            };
            boolean fieldsArePrivate = true;
            for (Field field : fields) {
                fieldsArePrivate = fieldsArePrivate
                        && Modifier.isPrivate(field.getModifiers());
                field.setAccessible(true);
            }
            return buildScrapOozeContracts(
                    eventClass.getName(), buttonCount, fieldsArePrivate,
                    chanceField.getType(), chanceField.get(event),
                    damageField.getType(), damageField.get(event),
                    totalDamageField.getType(), totalDamageField.get(event),
                    screenNumField.getType(), screenNumField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<String> getFaceTraderRelicPool() {
        if (AbstractDungeon.player == null) {
            return null;
        }
        ArrayList<String> pool = new ArrayList<>();
        for (String relicId : FACE_TRADER_RELIC_IDS) {
            if (!AbstractDungeon.player.hasRelic(relicId)) {
                pool.add(relicId);
            }
        }
        if (pool.isEmpty()) {
            pool.add("Circlet");
        }
        return pool;
    }

    private static ArrayList<HashMap<String, Object>>
            getFaceTraderEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field goldField = eventClass.getDeclaredField("goldReward");
            Field damageField = eventClass.getDeclaredField("damage");
            boolean screenPrivate = Modifier.isPrivate(screenField.getModifiers());
            boolean effectsPrivateStatic =
                    Modifier.isPrivate(goldField.getModifiers())
                    && Modifier.isStatic(goldField.getModifiers())
                    && Modifier.isPrivate(damageField.getModifiers())
                    && Modifier.isStatic(damageField.getModifiers());
            screenField.setAccessible(true);
            goldField.setAccessible(true);
            damageField.setAccessible(true);
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildFaceTraderContracts(
                    eventClass.getName(), screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(), buttonCount,
                    screenPrivate, effectsPrivateStatic,
                    goldField.getType(), goldField.get(null),
                    damageField.getType(), damageField.get(null),
                    getFaceTraderRelicPool());
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>>
            getDuplicatorEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenNumField = eventClass.getDeclaredField("screenNum");
            boolean screenPrivate = Modifier.isPrivate(
                    screenNumField.getModifiers());
            screenNumField.setAccessible(true);
            return buildDuplicatorContracts(
                    eventClass.getName(), buttonCount, screenPrivate,
                    screenNumField.getType(), screenNumField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>>
            getBonfireEventContracts(AbstractEvent event, int buttonCount) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Class<?> eventClass = event.getClass();
            Field screenField = eventClass.getDeclaredField("screen");
            Field cardSelectField = eventClass.getDeclaredField("cardSelect");
            boolean fieldsPrivate =
                    Modifier.isPrivate(screenField.getModifiers())
                    && Modifier.isPrivate(cardSelectField.getModifiers());
            screenField.setAccessible(true);
            cardSelectField.setAccessible(true);
            Object screenValue = screenField.get(event);
            if (!(screenValue instanceof Enum<?>)) {
                return none;
            }
            return buildBonfireContracts(
                    eventClass.getName(), screenField.getType().getName(),
                    ((Enum<?>) screenValue).name(), buttonCount,
                    fieldsPrivate, cardSelectField.getType(),
                    cardSelectField.get(event));
        } catch (ReflectiveOperationException | RuntimeException exception) {
            return none;
        }
    }

    private static ArrayList<HashMap<String, Object>> getNeowContracts(
            NeowEvent event, ArrayList<LargeDialogOptionButton> buttons) {
        ArrayList<HashMap<String, Object>> none = new ArrayList<>();
        try {
            Integer screenNum = ReflectionHacks.getPrivate(event, NeowEvent.class, "screenNum");
            if (screenNum == null) {
                return none;
            }
            ArrayList<String> enabledLabels = new ArrayList<>();
            for (LargeDialogOptionButton button : buttons) {
                if (!button.isDisabled) {
                    enabledLabels.add(button.msg);
                }
            }

            @SuppressWarnings("unchecked")
            ArrayList<NeowReward> rewards = ReflectionHacks.getPrivate(
                    event, NeowEvent.class, "rewards");
            ArrayList<HashMap<String, Object>> reflectedRewards = new ArrayList<>();
            if (rewards != null) {
                for (NeowReward reward : rewards) {
                    if (reward == null || reward.optionLabel == null
                            || reward.type == null || reward.drawback == null) {
                        return none;
                    }
                    Integer hpBonus = ReflectionHacks.getPrivate(
                            reward, NeowReward.class, "hp_bonus");
                    NeowReward.NeowRewardDrawbackDef drawbackDef = ReflectionHacks.getPrivate(
                            reward, NeowReward.class, "drawbackDef");
                    if (hpBonus == null) {
                        return none;
                    }

                    // The game may retain a private drawbackDef from a prior
                    // generated reward even when this public reward drawback
                    // is NONE.  Emitting that stale enum makes the Python
                    // bridge correctly reject three of the four Neow
                    // contracts.  NONE has no drawback definition; for a real
                    // drawback require the private definition to agree with
                    // the public authoritative enum.
                    String drawbackDefKind = canonicalNeowDrawbackDefKind(
                            reward.drawback.name(),
                            drawbackDef == null || drawbackDef.type == null
                                    ? null : drawbackDef.type.name());

                    HashMap<String, Object> parameters = new HashMap<>();
                    parameters.put("hp_bonus", hpBonus);
                    // NeowReward.cursed is an implementation detail of the
                    // reward object, not the authoritative choice drawback.
                    // The public enum is the only stable source for this
                    // protocol contract; otherwise a real CURSE drawback can
                    // be emitted as false and the independent verifier must
                    // (correctly) reject it.
                    parameters.put(
                            "cursed", "CURSE".equals(reward.drawback.name()));
                    parameters.put(
                            "drawback_def_kind",
                            drawbackDefKind);

                    HashMap<String, Object> contract = new HashMap<>();
                    contract.put("contract_version", 1);
                    contract.put("contract_kind", "NEOW_REWARD");
                    contract.put("reward_kind", reward.type.name());
                    contract.put("drawback_kind", reward.drawback.name());
                    contract.put("parameters", parameters);

                    HashMap<String, Object> evidence = new HashMap<>();
                    evidence.put("option_label", reward.optionLabel);
                    evidence.put("contract", contract);
                    reflectedRewards.add(evidence);
                }
            }
            return alignNeowContracts(screenNum, enabledLabels, reflectedRewards);
        } catch (RuntimeException exception) {
            // Reflection or shape drift is an auditable unknown, never a reason
            // to guess a reward from localized text or container order.
            return none;
        }
    }

    /**
     * The event state object contains:
     * "body_text" (string): The current body text for the event, or an empty string if there is none
     * "event_name" (string): The name of the event, in the current language
     * "event_id" (string): The ID of the event (NOTE: This implementation is sketchy and may not play nice with mods)
     * "options" (list): A list of options, in the order they are presented in game. Each option contains:
     * - "text" (string): The full text associated with the option (Eg. "[Banana] Heal 10 hp")
     * - "disabled" (boolean): Whether the current option or button is disabled. Disabled buttons cannot be chosen
     * - "label" (string): The simple label of a button or option (Eg. "Banana")
     * - "original_button_index" (int): The option's index in the unfiltered game container
     * - "choice_index" (int): The index of the option for the choose command, if applicable
     * @return The event state object
     */
    private static void putBaseGameEventProgress(
            HashMap<String, Object> state, AbstractEvent event) {
        if (state == null || event == null) {
            return;
        }
        Class<?> eventClass = event.getClass();
        String className = eventClass.getName();
        state.put("event_class", className);
        String[] auditedClasses = {
                "com.megacrit.cardcrawl.events.exordium.Sssserpent",
                "com.megacrit.cardcrawl.events.city.Vampires",
                "com.megacrit.cardcrawl.events.city.TheLibrary",
                "com.megacrit.cardcrawl.events.shrines.GoldShrine",
                "com.megacrit.cardcrawl.events.exordium.ShiningLight",
                "com.megacrit.cardcrawl.events.exordium.Mushrooms",
                "com.megacrit.cardcrawl.events.city.MaskedBandits",
                "com.megacrit.cardcrawl.events.city.Colosseum",
                "com.megacrit.cardcrawl.events.beyond.WindingHalls",
                "com.megacrit.cardcrawl.events.beyond.SensoryStone",
                "com.megacrit.cardcrawl.events.shrines.AccursedBlacksmith",
                "com.megacrit.cardcrawl.events.beyond.MysteriousSphere",
                "com.megacrit.cardcrawl.events.beyond.SpireHeart",
                BONFIRE_EVENT_CLASS,
        };
        boolean audited = false;
        for (String auditedClass : auditedClasses) {
            if (auditedClass.equals(className)) {
                audited = true;
                break;
            }
        }
        if (!audited) {
            return;
        }
        for (String fieldName : new String[]{"screen", "curScreen", "screenNum"}) {
            try {
                Field field = eventClass.getDeclaredField(fieldName);
                field.setAccessible(true);
                Object value = field.get(event);
                if (value instanceof Enum<?>) {
                    state.put("event_stage", ((Enum<?>) value).name());
                    return;
                }
                if (value instanceof Integer) {
                    state.put("screen_num", value);
                    return;
                }
            } catch (ReflectiveOperationException | RuntimeException exception) {
                // Try the next exact field name.  Missing progress evidence
                // intentionally leaves the event untyped downstream.
            }
        }
    }

    private static HashMap<String, Object> getEventState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> options = new ArrayList<>();
        ChoiceScreenUtils.EventDialogType eventDialogType = ChoiceScreenUtils.getEventDialogType();
        AbstractEvent event = AbstractDungeon.getCurrRoom().event;
        ArrayList<HashMap<String, Object>> neowContracts = new ArrayList<>();
        int choice_index = 0;
        if (eventDialogType == ChoiceScreenUtils.EventDialogType.IMAGE || eventDialogType == ChoiceScreenUtils.EventDialogType.ROOM) {
            ArrayList<LargeDialogOptionButton> eventButtons = ChoiceScreenUtils.getEventButtons();
            ArrayList<HashMap<String, Object>> eventContracts =
                    getBaseGameEventContracts(event, eventButtons.size());
            if (event instanceof NeowEvent) {
                neowContracts = getNeowContracts((NeowEvent) event, eventButtons);
            }
            int enabledNeowIndex = 0;
            for (int originalButtonIndex = 0;
                    originalButtonIndex < eventButtons.size();
                    originalButtonIndex += 1) {
                LargeDialogOptionButton button = eventButtons.get(originalButtonIndex);
                HashMap<String, Object> json_button = new HashMap<>();
                json_button.put("text", removeTextFormatting(button.msg));
                json_button.put("disabled", button.isDisabled);
                json_button.put("label", ChoiceScreenUtils.getOptionName(button.msg));
                json_button.put("original_button_index", originalButtonIndex);
                if (originalButtonIndex < eventContracts.size()) {
                    json_button.put(
                            "event_contract",
                            eventContracts.get(originalButtonIndex));
                }
                AbstractCard cardPreview = (AbstractCard) ReflectionHacks.getPrivate(
                        button, LargeDialogOptionButton.class, "cardToPreview");
                if (cardPreview != null) {
                    json_button.put("card", convertCardToJson(cardPreview));
                }
                if (!button.isDisabled) {
                    json_button.put("choice_index", choice_index);
                    if (enabledNeowIndex < neowContracts.size()) {
                        json_button.put("neow_contract", neowContracts.get(enabledNeowIndex));
                    }
                    enabledNeowIndex += 1;
                    choice_index += 1;
                }
                options.add(json_button);
            }
            state.put("body_text", removeTextFormatting(UpdateBodyTextPatch.bodyText));
        } else {
            ArrayList<String> miscOptions = ChoiceScreenUtils.getEventScreenChoices();
            for (int originalButtonIndex = 0;
                    originalButtonIndex < miscOptions.size();
                    originalButtonIndex += 1) {
                String misc_option = miscOptions.get(originalButtonIndex);
                HashMap<String, Object> json_button = new HashMap<>();
                json_button.put("text", misc_option);
                json_button.put("disabled", false);
                json_button.put("label", misc_option);
                json_button.put("original_button_index", originalButtonIndex);
                json_button.put("choice_index", choice_index);
                choice_index += 1;
                options.add(json_button);
            }
            state.put("body_text", "");
        }
        state.put("event_name", ReflectionHacks.getPrivateStatic(event.getClass(), "NAME"));
        if (event instanceof NeowEvent) {
            state.put("event_id", "Neow Event");
        } else {
            try {
                // AbstractEvent does not have a static "ID" field, but all of the events in the base game do.
                Field targetField = event.getClass().getDeclaredField("ID");
                state.put("event_id", (String)targetField.get(null));
            } catch (NoSuchFieldException | IllegalAccessException e) {
                state.put("event_id", "");
            }
            state.put("event_id", ReflectionHacks.getPrivateStatic(event.getClass(), "ID"));
        }
        putBaseGameEventProgress(state, event);
        state.put("options", options);
        return state;
    }

    /**
     * The card reward state object contains:
     * "bowl_available" (boolean): Whether the Singing Bowl button is present
     * "skip_available" (boolean): Whether the card reward is skippable
     * "cards" (list): The list of cards that can be chosen
     * @return The card reward state object
     */
    private static HashMap<String, Object> getCardRewardState() {
        HashMap<String, Object> state = new HashMap<>();
        state.put("bowl_available", ChoiceScreenUtils.isBowlAvailable());
        state.put("skip_available", ChoiceScreenUtils.isCardRewardSkipAvailable());
        ArrayList<Object> cardRewardJson = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.cardRewardScreen.rewardGroup) {
            cardRewardJson.add(convertCardToJson(card));
        }
        state.put("cards", cardRewardJson);
        return state;
    }

    /**
     * The combat reward screen state object contains:
     * "rewards" (list): A list of reward objects, each of which contains:
     * - "choice_index" (int): The exact index accepted by CHOOSE
     * - "reward_type" (string): The name of the RewardItem.RewardType enum for the reward
     * - "gold" (int): The amount of gold in the reward, if applicable
     * - "relic" (object): The relic in the reward, if applicable
     * - "potion" (object): The potion in the reward, if applicable
     * - "link" (object): The relic that the sapphire key is linked to, if applicable
     * @return The combat reward screen state object
     */
    private static HashMap<String, Object> getCombatRewardState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> rewards = new ArrayList<>();
        int choiceIndex = 0;
        for(RewardItem reward : AbstractDungeon.combatRewardScreen.rewards) {
            HashMap<String, Object> jsonReward = new HashMap<>();
            jsonReward.put("choice_index", choiceIndex);
            jsonReward.put("reward_type", reward.type.name());
            switch(reward.type) {
                case GOLD:
                case STOLEN_GOLD:
                    jsonReward.put("gold", reward.goldAmt + reward.bonusGold);
                    break;
                case RELIC:
                    jsonReward.put("relic", convertRelicToJson(reward.relic));
                    break;
                case POTION:
                    jsonReward.put("potion", convertPotionToJson(reward.potion));
                    break;
                case SAPPHIRE_KEY:
                    jsonReward.put("link", convertRelicToJson(reward.relicLink.relic));
            }
            rewards.add(jsonReward);
            choiceIndex += 1;
        }
        state.put("rewards", rewards);
        return state;
    }

    /**
     * The map screen state object contains:
     * "current_node" (object): The node object for the currently selected node, if applicable
     * "next_nodes" (list): A list of nodes that can be chosen next
     * "first_node_chosen" (boolean): Whether the first node in the act has already been chosen
     * "boss_available" (boolean): Whether the next node choice is a boss
     * @return The map screen state object
     */
    private static HashMap<String, Object> getMapScreenState() {
        HashMap<String, Object> state = new HashMap<>();
        if (AbstractDungeon.getCurrMapNode() != null) {
            state.put("current_node", convertMapRoomNodeToJson(AbstractDungeon.getCurrMapNode()));
        }
        ArrayList<Object> nextNodesJson = new ArrayList<>();
        for(MapRoomNode node : ChoiceScreenUtils.getMapScreenNodeChoices()) {
            nextNodesJson.add(convertMapRoomNodeToJson(node));
        }
        state.put("next_nodes", nextNodesJson);
        state.put("first_node_chosen", AbstractDungeon.firstRoomChosen);
        state.put("boss_available", ChoiceScreenUtils.bossNodeAvailable());
        return state;
    }

    /**
     * The boss reward screen state contains:
     * "relics" (list): A list of relics that can be chosen from the boss
     * Note: Blights are not supported.
     * @return The boss reward screen state object
     */
    private static HashMap<String, Object> getBossRewardState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> bossRelics = new ArrayList<>();
        for(AbstractRelic relic : AbstractDungeon.bossRelicScreen.relics) {
            bossRelics.add(convertRelicToJson(relic));
        }
        state.put("relics", bossRelics);
        return state;
    }

    /**
     * The shop screen state contains:
     * "cards" (list): A list of cards available to buy
     * "relics" (list): A list of relics available to buy
     * "potions" (list): A list of potions available to buy
     * "purge_available" (boolean): Whether the card remove option is available
     * "purge_cost" (int): The cost of the card remove option
     * @return The shop screen state object
     */
    private static HashMap<String, Object> getShopScreenState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> shopCards = new ArrayList<>();
        ArrayList<Object> shopRelics = new ArrayList<>();
        ArrayList<Object> shopPotions = new ArrayList<>();
        for(AbstractCard card : ChoiceScreenUtils.getShopScreenCards()) {
            HashMap<String, Object> jsonCard = convertCardToJson(card);
            jsonCard.put("price", card.price);
            shopCards.add(jsonCard);
        }
        for(StoreRelic relic : ChoiceScreenUtils.getShopScreenRelics()) {
            HashMap<String, Object> jsonRelic = convertRelicToJson(relic.relic);
            jsonRelic.put("price", relic.price);
            shopRelics.add(jsonRelic);
        }
        for(StorePotion potion : ChoiceScreenUtils.getShopScreenPotions()) {
            HashMap<String, Object> jsonPotion = convertPotionToJson(potion.potion);
            jsonPotion.put("price", potion.price);
            shopPotions.add(jsonPotion);
        }
        state.put("cards", shopCards);
        state.put("relics", shopRelics);
        state.put("potions", shopPotions);
        state.put("purge_available", AbstractDungeon.shopScreen.purgeAvailable);
        state.put("purge_cost", ShopScreen.actualPurgeCost);
        return state;
    }

    /**
     * The grid select screen state contains:
     * "cards" (list): The list of cards available to pick, including selected cards
     * "selected_cards" (list): The list of cards that are currently selected
     * "num_cards" (int): The number of cards that must be selected
     * "any_number" (boolean): Whether any number of cards can be selected
     * "for_upgrade" (boolean): Whether the selected cards will be upgraded
     * "for_transform" (boolean): Whether the selected cards will be transformed
     * _for_purge" (boolean): Whether the selected cards will be removed from the deck
     * "confirm_up" (boolean): Whether the confirm screen is up, and cards cannot be selected
     * @return The grid select screen state object
     */
    private static HashMap<String, Object> getGridState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> gridJson = new ArrayList<>();
        ArrayList<Object> gridSelectedJson = new ArrayList<>();
        ArrayList<AbstractCard> gridCards = ChoiceScreenUtils.getGridScreenCards();
        GridCardSelectScreen screen = AbstractDungeon.gridSelectScreen;
        for(AbstractCard card : gridCards) {
            gridJson.add(convertCardToJson(card));
        }
        for(AbstractCard card : screen.selectedCards) {
            gridSelectedJson.add(convertCardToJson(card));
        }
        int numCards = (int) ReflectionHacks.getPrivate(screen, GridCardSelectScreen.class, "numCards");
        boolean forUpgrade = (boolean) ReflectionHacks.getPrivate(screen, GridCardSelectScreen.class, "forUpgrade");
        boolean forTransform = (boolean) ReflectionHacks.getPrivate(screen, GridCardSelectScreen.class, "forTransform");
        boolean forPurge = (boolean) ReflectionHacks.getPrivate(screen, GridCardSelectScreen.class, "forPurge");
        state.put("cards", gridJson);
        state.put("selected_cards", gridSelectedJson);
        state.put("num_cards", numCards);
        state.put("any_number", screen.anyNumber);
        state.put("for_upgrade", forUpgrade);
        state.put("for_transform", forTransform);
        state.put("for_purge", forPurge);
        state.put("confirm_up", screen.confirmScreenUp || screen.isJustForConfirming);
        if ((screen.confirmScreenUp || screen.isJustForConfirming) && GridCardSelectScreenPatch.hoverCard != null) {
            state.put("last_selected_card", convertCardToJson(GridCardSelectScreenPatch.hoverCard));
        }
        return state;
    }

    /**
     * The hand select screen state contains:
     * "hand" (list): The list of cards currently in your hand, not including selected cards
     * "selected" (list): The list of currently selected cards
     * "max_cards" (int): The maximum number of cards that can be selected
     * "can_pick_zero" (boolean): Whether zero cards can be selected
     * @return The hand select screen state object
     */
    private static HashMap<String, Object> getHandSelectState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> handJson = new ArrayList<>();
        ArrayList<Object> selectedJson = new ArrayList<>();
        ArrayList<AbstractCard> handCards = AbstractDungeon.player.hand.group;
        // As far as I can tell, this comment is a Java 8 analogue of a Python list comprehension? I think just looping is more readable.
        // handJson = handCards.stream().map(GameStateConverter::convertCardToJson).collect(Collectors.toCollection(ArrayList::new));
        for(AbstractCard card : handCards) {
            handJson.add(convertCardToJson(card));
        }
        state.put("hand", handJson);
        ArrayList<AbstractCard> selectedCards = AbstractDungeon.handCardSelectScreen.selectedCards.group;
        for(AbstractCard card : selectedCards) {
            selectedJson.add(convertCardToJson(card));
        }
        state.put("selected", selectedJson);
        state.put("max_cards", AbstractDungeon.handCardSelectScreen.numCardsToSelect);
        state.put("can_pick_zero", AbstractDungeon.handCardSelectScreen.canPickZero);
        return state;
    }

    /**
     * The game over screen state contains:
     * "score" (int): Your final score
     * "victory" (boolean): Whether you won
     * @return The game over screen state object
     */
    private static HashMap<String, Object> getGameOverState() {
        HashMap<String, Object> state = new HashMap<>();
        int score = 0;
        boolean victory = false;
        if(AbstractDungeon.screen == AbstractDungeon.CurrentScreen.DEATH) {
            score = (int) ReflectionHacks.getPrivate(AbstractDungeon.deathScreen, GameOverScreen.class, "score");
            victory = GameOverScreen.isVictory;
        } else if(AbstractDungeon.screen == AbstractDungeon.CurrentScreen.VICTORY) {
            score = (int) ReflectionHacks.getPrivate(AbstractDungeon.victoryScreen, GameOverScreen.class, "score");
            victory = true;
        }
        state.put("score", score);
        state.put("victory", victory);
        return state;
    }

    /**
     * Gets the appropriate screen state object
     * @return An object containing your current screen state
     */
    private static HashMap<String, Object> getScreenState() {
        ChoiceScreenUtils.ChoiceType screenType = ChoiceScreenUtils.getCurrentChoiceType();
        switch (screenType) {
            case EVENT:
                return getEventState();
            case CHEST:
            case REST:
                return getRoomState();
            case CARD_REWARD:
                return getCardRewardState();
            case COMBAT_REWARD:
                return getCombatRewardState();
            case MAP:
                return getMapScreenState();
            case BOSS_REWARD:
                return getBossRewardState();
            case SHOP_SCREEN:
                return getShopScreenState();
            case GRID:
                return getGridState();
            case HAND_SELECT:
                return getHandSelectState();
            case GAME_OVER:
                return getGameOverState();
        }
        return new HashMap<>();
    }

    /**
     * Gets the state of the current combat in game.
     * The combat state object contains:
     * "draw_pile" (list): The list of cards in your draw pile
     * "discard_pile" (list): The list of cards in your discard pile
     * "exhaust_pile" (list): The list of cards in your exhaust pile
     * "hand" (list): The list of cards in your hand
     * "limbo" (list): The list of cards that are in 'limbo', which is used for a variety of effects in game.
     * "card_in_play" (object, optional): The card that is currently in play, if any.
     * "player" (object): The state of the player
     * "monsters" (list): A list of the enemies in the combat, including dead enemies
     * "turn" (int): The current turn (or round) number of the combat.
     * "cards_discarded_this_turn" (int): The number of cards discarded this turn.
     * "times_damaged" (int): The number of times the player has been damaged this combat (for Blood for Blood).
     * Note: The order of the draw pile is not currently randomized when sent to the client.
     * @return The combat state object
     */
    private static HashMap<String, Object> getCombatState() {
        HashMap<String, Object> state = new HashMap<>();
        ArrayList<Object> monsters = new ArrayList<>();
        for(AbstractMonster monster : AbstractDungeon.getCurrRoom().monsters.monsters) {
            monsters.add(convertMonsterToJson(monster));
        }
        state.put("monsters", monsters);
        ArrayList<Object> draw_pile = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.player.drawPile.group) {
            draw_pile.add(convertCardToJson(card));
        }
        ArrayList<Object> discard_pile = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.player.discardPile.group) {
            discard_pile.add(convertCardToJson(card));
        }
        ArrayList<Object> exhaust_pile = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.player.exhaustPile.group) {
            exhaust_pile.add(convertCardToJson(card));
        }
        ArrayList<Object> hand = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.player.hand.group) {
            hand.add(convertCardToJson(card));
        }
        ArrayList<Object> limbo = new ArrayList<>();
        for(AbstractCard card : AbstractDungeon.player.limbo.group) {
            limbo.add(convertCardToJson(card));
        }
        state.put("draw_pile", draw_pile);
        state.put("discard_pile", discard_pile);
        state.put("exhaust_pile", exhaust_pile);
        state.put("hand", hand);
        state.put("limbo", limbo);
        if (AbstractDungeon.player.cardInUse != null) {
            state.put("card_in_play", convertCardToJson(AbstractDungeon.player.cardInUse));
        }
        state.put("player", convertPlayerToJson(AbstractDungeon.player));
        state.put("turn", GameActionManager.turn);
        state.put("cards_discarded_this_turn", GameActionManager.totalDiscardedThisTurn);
        state.put("times_damaged", AbstractDungeon.player.damagedThisCombat);
        // Observed combat history, never future draw order or RNG state.
        state.put("cards_played_this_turn", AbstractDungeon.player.cardsPlayedThisTurn);
        GameActionManager manager = AbstractDungeon.actionManager;
        if (manager != null) {
            int attacks = 0, skills = 0, powers = 0, lightning = 0, frost = 0;
            for (AbstractCard card : manager.cardsPlayedThisTurn) {
                if (card.type == AbstractCard.CardType.ATTACK) attacks++;
                if (card.type == AbstractCard.CardType.SKILL) skills++;
            }
            for (AbstractCard card : manager.cardsPlayedThisCombat) {
                if (card.type == AbstractCard.CardType.POWER) powers++;
            }
            for (AbstractOrb orb : manager.orbsChanneledThisCombat) {
                if ("Lightning".equals(orb.ID)) lightning++;
                if ("Frost".equals(orb.ID)) frost++;
            }
            state.put("attacks_played_this_turn", attacks);
            state.put("skills_played_this_turn", skills);
            state.put("powers_played_this_combat", powers);
            state.put("lightning_channeled", lightning);
            state.put("frost_channeled", frost);
        }
        AbstractRelic chip = AbstractDungeon.player.getRelic("Emotion Chip");
        if (chip == null) {
            state.put("emotion_chip_pending", false);
        } else {
            try {
                state.put("emotion_chip_pending",
                    ReflectionHacks.getPrivate(chip, AbstractRelic.class, "pulse"));
            } catch (RuntimeException ignored) {
                // Missing evidence must remain absent, not be interpreted as false.
            }
        }
        AbstractRelic puzzle = AbstractDungeon.player.getRelic("Centennial Puzzle");
        state.put("centennial_puzzle_used_this_combat", puzzle != null && puzzle.grayscale);
        return state;
    }

    /**
     * Creates a GSON-compatible representation of the game map
     * The map object is a list of nodes, each of which with two extra fields:
     * "parents" (list): Not implemented
     * "children" (list): The nodes connected by an edge out of the node in question
     * @return A list of node objects
     */
    private static ArrayList<Object> getCachedMapJson() {
        ArrayList<ArrayList<MapRoomNode>> map = AbstractDungeon.map;
        int signature = mapSignature(map);
        if (cachedMapJson == null || cachedMapIdentity != map || cachedMapSignature != signature) {
            cachedMapIdentity = map;
            cachedMapSignature = signature;
            cachedMapJson = convertMapToJson();
        }
        return cachedMapJson;
    }

    /**
     * Cheap allocation-free fingerprint of every value used by map JSON.
     * This keeps the cache correct if a room or burning-elite marker changes,
     * while avoiding hundreds of HashMap/ArrayList allocations after each
     * card play in an otherwise unchanged act.
     */
    private static int mapSignature(ArrayList<ArrayList<MapRoomNode>> map) {
        int signature = AbstractDungeon.actNum;
        if (map == null) {
            return signature;
        }
        for (ArrayList<MapRoomNode> layer : map) {
            for (MapRoomNode node : layer) {
                if (!node.hasEdges()) {
                    continue;
                }
                signature = 31 * signature + node.x;
                signature = 31 * signature + node.y;
                signature = 31 * signature + (node.hasEmeraldKey ? 1 : 0);
                String symbol = node.getRoomSymbol(true);
                signature = 31 * signature + (symbol == null ? 0 : symbol.hashCode());
                signature = 31 * signature + node.getEdges().size();
                for (MapEdge edge : node.getEdges()) {
                    signature = 31 * signature + edge.srcX;
                    signature = 31 * signature + edge.srcY;
                    signature = 31 * signature + edge.dstX;
                    signature = 31 * signature + edge.dstY;
                }
            }
        }
        return signature;
    }

    private static ArrayList<Object> convertMapToJson() {
        ArrayList<ArrayList<MapRoomNode>> map = AbstractDungeon.map;
        ArrayList<Object> jsonMap = new ArrayList<>();
        for(ArrayList<MapRoomNode> layer : map) {
            for(MapRoomNode node : layer) {
                if(node.hasEdges()) {
                    HashMap<String, Object> json_node = convertMapRoomNodeToJson(node);
                    ArrayList<Object> json_children = new ArrayList<>();
                    ArrayList<Object> json_parents = new ArrayList<>();
                    for(MapEdge edge : node.getEdges()) {
                        if (edge.srcX == node.x && edge.srcY == node.y) {
                            json_children.add(convertCoordinatesToJson(edge.dstX, edge.dstY));
                        } else {
                            json_parents.add(convertCoordinatesToJson(edge.srcX, edge.srcY));
                        }
                    }

                    json_node.put("parents", json_parents);
                    json_node.put("children", json_children);
                    jsonMap.add(json_node);
                }
            }
        }
        return jsonMap;
    }

    private static HashMap<String, Object> convertCoordinatesToJson(int x, int y) {
        HashMap<String, Object> jsonNode = new HashMap<>();
        jsonNode.put("x", x);
        jsonNode.put("y", y);
        return jsonNode;
    }

    /**
     * Creates a GSON-compatible representation of the given node
     * The node object contains:
     * "x" (int): The node's x coordinate
     * "y" (int): The node's y coordinate
     * "symbol" (string, optional): The map symbol for the node (?, $, T, M, E, R)
     * "children" (list, optional): The nodes connected by an edge out of the provided node
     * Note: children are added by convertMapToJson()
     * @param node The node to convert
     * @return A node object
     */
    private static HashMap<String, Object> convertMapRoomNodeToJson(MapRoomNode node) {
        HashMap<String, Object> jsonNode = convertCoordinatesToJson(node.x, node.y);
        jsonNode.put("symbol", node.getRoomSymbol(true));
        jsonNode.put("has_emerald_key", node.hasEmeraldKey);
        return jsonNode;
    }

    /**
     * Creates a GSON-compatible representation of the given cards
     * The card object contains:
     * "name" (string): The name of the card, in the currently selected language
     * "uuid" (string): The unique identifier of the card
     * "misc" (int): The misc field for the card, used by cards like Ritual Dagger
     * "is_playable" (boolean): Whether the card can currently be played, though does not guarantee a target
     * "cost" (int): The current cost of the card. -2 is unplayable and -1 is X cost
     * "damage" (int): The card's current displayed damage after player modifiers
     * "block" (int): The card's current displayed block after player modifiers
     * "magic_number" (int): The card's current primary effect magnitude
     * "upgrades" (int): The number of times the card is upgraded
     * "id" (string): The id of the card
     * "type" (string): The name of the AbstractCard.CardType enum for the card
     * "rarity" (string): The name of the AbstractCard.CardRarity enum for the card
     * "has_target" (boolean): Whether the card requires a target to be played
     * "exhausts" (boolean): Whether the card exhausts when played
     * "ethereal" (boolean): Whether the card is ethereal
     * @param card The card to convert
     * @return A card object
     */
    private static HashMap<String, Object> convertCardToJson(AbstractCard card) {
        HashMap<String, Object> jsonCard = new HashMap<>();
        jsonCard.put("name", card.name);
        jsonCard.put("description", card.rawDescription);
        jsonCard.put("uuid", card.uuid.toString());
        jsonCard.put("misc", card.misc);
        if(AbstractDungeon.getMonsters() != null) {
            jsonCard.put("is_playable", card.canUse(AbstractDungeon.player, null));
        }
        jsonCard.put("cost", card.costForTurn);
        jsonCard.put("combat_cost", card.cost);
        jsonCard.put("free_to_play_once", card.freeToPlayOnce);
        jsonCard.put("retain", card.retain);
        jsonCard.put("damage", card.damage);
        jsonCard.put("base_damage", card.baseDamage);
        jsonCard.put("block", card.block);
        jsonCard.put("base_block", card.baseBlock);
        jsonCard.put("magic_number", card.magicNumber);
        jsonCard.put("upgrades", card.timesUpgraded);
        jsonCard.put("id", card.cardID);
        jsonCard.put("type", card.type.name());
        jsonCard.put("rarity", card.rarity.name());
        jsonCard.put("has_target", card.target== AbstractCard.CardTarget.SELF_AND_ENEMY || card.target == AbstractCard.CardTarget.ENEMY);
        jsonCard.put("exhausts", card.exhaust);
        jsonCard.put("ethereal", card.isEthereal);
        jsonCard.put("in_bottle_flame", card.inBottleFlame);
        jsonCard.put("in_bottle_lightning", card.inBottleLightning);
        jsonCard.put("in_bottle_tornado", card.inBottleTornado);
        return jsonCard;
    }

    /**
     * Creates a GSON-compatible representation of the given monster
     * The monster object contains:
     * "name" (string): The monster's name, in the currently selected language
     * "id" (string): The monster's id
     * "current_hp" (int): The monster's current hp
     * "max_hp" (int): The monster's maximum hp
     * "block" (int): The monster's current block
     * "intent" (string): The name of the AbstractMonster.Intent enum for the monster's current intent
     * "move_id" (int): The move id byte for the monster's current move
     * "move_base_damage" (int): The base damage for the monster's current attack
     * "move_adjusted_damage" (int): The damage number actually shown on the intent for the monster's current attack
     * "move_hits" (int): The number of hits done by the current attack
     * "last_move_id" (int): The move id byte for the monster's previous move
     * "second_last_move_id" (int): The move id byte from 2 moves ago
     * "half_dead" (boolean): Whether the monster is half dead
     * "is_gone" (boolean): Whether the monster is dead or has run away
     * "powers" (list): The monster's current powers
     * Note: If the player has Runic Dome, intent will always return NONE
     * @param monster The monster to convert
     * @return A monster object
     */
    private static HashMap<String, Object> convertMonsterToJson(AbstractMonster monster) {
        HashMap<String, Object> jsonMonster = new HashMap<>();
        jsonMonster.put("id", monster.id);
        jsonMonster.put("name", monster.name);
        jsonMonster.put("current_hp", monster.currentHealth);
        jsonMonster.put("max_hp", monster.maxHealth);
        if (AbstractDungeon.player.hasRelic(RunicDome.ID)) {
            jsonMonster.put("intent", AbstractMonster.Intent.NONE);
        } else {
            jsonMonster.put("intent", monster.intent.name());
            EnemyMoveInfo moveInfo = (EnemyMoveInfo)ReflectionHacks.getPrivate(monster, AbstractMonster.class, "move");
            if (moveInfo != null) {
                jsonMonster.put("move_id", moveInfo.nextMove);
                jsonMonster.put("move_base_damage", moveInfo.baseDamage);
                int intentDmg = (int)ReflectionHacks.getPrivate(monster, AbstractMonster.class, "intentDmg");
                if (moveInfo.baseDamage > 0) {
                    jsonMonster.put("move_adjusted_damage", intentDmg);
                } else {
                    jsonMonster.put("move_adjusted_damage", moveInfo.baseDamage);
                }
                int move_hits = moveInfo.multiplier;
                // If isMultiDamage is not set, the multiplier is probably 0, but there is really 1 attack.
                if (!moveInfo.isMultiDamage) {
                    move_hits = 1;
                }
                jsonMonster.put("move_hits", move_hits);
            }
        }
        if(monster.moveHistory.size() >= 2) {
            jsonMonster.put("last_move_id", monster.moveHistory.get(monster.moveHistory.size() - 2));
        }
        if(monster.moveHistory.size() >= 3) {
            jsonMonster.put("second_last_move_id", monster.moveHistory.get(monster.moveHistory.size() - 3));
        }
        if ("TimeEater".equals(monster.id)) {
            Object usedHaste = getFieldIfExists(monster, "usedHaste");
            if (usedHaste instanceof Boolean) jsonMonster.put("miscBool", usedHaste);
        }
        jsonMonster.put("half_dead", monster.halfDead);
        jsonMonster.put("is_gone", monster.isDeadOrEscaped());
        jsonMonster.put("block", monster.currentBlock);
        jsonMonster.put("powers", convertCreaturePowersToJson(monster));
        return jsonMonster;
    }

    /**
     * Creates a GSON-compatible representation of the given player
     * The player object contains:
     * "max_hp" (int): The player's maximum hp
     * "current_hp" (int): The player's current hp
     * "block" (int): The player's current block
     * "powers" (list): The player's current powers
     * "energy" (int): The player's current energy
     * "max_orbs" (int): The player's authoritative orb slot capacity
     * "orbs" (list): The player's current orb slots
     * Note: many other things, like draw pile and discard pile, are in the combat state
     * @param player The player to convert
     * @return A player object
     */
    private static HashMap<String, Object> convertPlayerToJson(AbstractPlayer player) {
        HashMap<String, Object> jsonPlayer = new HashMap<>();
        jsonPlayer.put("max_hp", player.maxHealth);
        jsonPlayer.put("current_hp", player.currentHealth);
        jsonPlayer.put("powers", convertCreaturePowersToJson(player));
        jsonPlayer.put("energy", EnergyPanel.totalCount);
        jsonPlayer.put("block", player.currentBlock);
        jsonPlayer.put("max_orbs", player.maxOrbs);
        jsonPlayer.put("orb_slots", player.maxOrbs);
        jsonPlayer.put("facing_left", player.flipHorizontal);
        ArrayList<Object> orbs = new ArrayList<>();
        for(AbstractOrb orb : player.orbs) {
            orbs.add(convertOrbToJson(orb));
        }
        jsonPlayer.put("orbs", orbs);
        return jsonPlayer;
    }

    /**
     * Checks whether the given object has the specified field. If so, returns the field's value. Else returns null.
     * @param object The object used to look for the specified field
     * @param fieldName The field that we want to access
     * @return The value of the field, if present, or else null.
     */
    private static Object getFieldIfExists(Object object, String fieldName) {
        Class<?> objectClass = object.getClass();
        Map<String, Field> fields = DECLARED_FIELDS.get(objectClass);
        if (fields == null) {
            HashMap<String, Field> discoveredFields = new HashMap<>();
            for (Field field : objectClass.getDeclaredFields()) {
                field.setAccessible(true);
                discoveredFields.put(field.getName(), field);
            }
            fields = Collections.unmodifiableMap(discoveredFields);
            DECLARED_FIELDS.put(objectClass, fields);
        }
        Field field = fields.get(fieldName);
        if (field == null) {
            return null;
        }
        try {
            return field.get(object);
        } catch(IllegalAccessException e) {
            e.printStackTrace();
            return null;
        }
    }

    /**
     * Creates a GSON-compatible representation of the given creature's powers
     * The power object contains:
     * "id" (string): The id of the power
     * "name" (string): The name of the power, in the currently selected language
     * "amount" (int): The amount of the power
     * "damage" (int, optional): The amount of damage the power does, if applicable
     * "card" (object, optional): The card associated with the power (for powers like Nightmare)
     * "misc" (int, optional): Contains misc values that don't fit elsewhere (such as the base value for Flight)
     * "just_applied" (boolean, optional): Used with many powers to prevent them from expiring immediately
     * @param creature The creature whose powers are to be converted
     * @return A list of power objects
     */
    private static ArrayList<Object> convertCreaturePowersToJson(AbstractCreature creature) {
        ArrayList<Object> powers = new ArrayList<>();
        for(AbstractPower power : creature.powers) {
            HashMap<String, Object> json_power = new HashMap<>();
            json_power.put("id", power.ID);
            json_power.put("name", power.name);
            json_power.put("amount", power.amount);
            Object damage = getFieldIfExists(power, "damage");
            if (damage != null) {
                json_power.put("damage", (int)damage);
            }
            Object card = getFieldIfExists(power, "card");
            if (card != null) {
                json_power.put("card", convertCardToJson((AbstractCard)card));
            }
            String[] miscFieldNames = {
                    "basePower", "maxAmt", "storedAmount", "hpLoss", "cardsDoubledThisTurn"
            };
            // basePower gives the base power for Malleable
            // maxAmt gives the max amount of damage per turn for Invincible
            // storedAmount gives the number of stacks per turn for Flight
            // hpLoss gives the amount of HP lost per turn with Combust
            // cardsDoubledThisTurn gives the number of cards already doubled with Echo Form
            Object misc = null;
            for (String fieldName : miscFieldNames) {
                misc = getFieldIfExists(power, fieldName);
                if (misc != null) {
                    json_power.put("misc", (int)misc);
                    break;
                }
            }

            String[] justAppliedNames = {
                    "justApplied", "skipFirst"
            };
            // justApplied is used with a variety of powers to prevent them from expiring immediately (cast from bool)
            // skipFirst is the same as justApplied, for the Ritual power
            Object justApplied = null;
            for (String fieldName : justAppliedNames) {
                justApplied = getFieldIfExists(power, fieldName);
                if (justApplied != null) {
                    json_power.put("just_applied", (boolean)justApplied);
                    break;
                }
            }

            powers.add(json_power);
        }
        return powers;
    }

    /**
     * Creates a GSON-compatible representation of the given relic
     * The relic object contains:
     * "id" (string): The id of the relic
     * "name" (string): The name of the relic, in the currently selected language
     * "tier" (string): The base-game relic tier
     * "counter" (int): The counter on the relic
     * @param relic The relic to convert
     * @return A relic object
     */
    private static HashMap<String, Object> convertRelicToJson(AbstractRelic relic) {
        HashMap<String, Object> jsonRelic = new HashMap<>();
        jsonRelic.put("id", relic.relicId);
        jsonRelic.put("name", relic.name);
        jsonRelic.put("description", relic.description);
        jsonRelic.put("tier", relic.tier.name());
        jsonRelic.put("counter", relic.counter);
        return jsonRelic;
    }

    /**
     * Creates a GSON-compatible representation of the given potion
     * The potion object contains:
     * "id" (string): The id of the potion
     * "name" (string): The name of the potion, in the currently selected language
     * "can_use" (boolean): Whether the potion can currently be used
     * "can_discard" (boolean): Whether the potion can currently be discarded
     * "requires_target" (boolean): Whether the potion must be used with a target
     * @param potion The potion to convert
     * @return A potion object
     */
    private static HashMap<String, Object> convertPotionToJson(AbstractPotion potion) {
        HashMap<String, Object> jsonPotion = new HashMap<>();
        jsonPotion.put("id", potion.ID);
        jsonPotion.put("name", potion.name);
        jsonPotion.put("description", potion.description);
        boolean canUse = potion.canUse();
        boolean canDiscard = potion.canDiscard();
        if (potion instanceof PotionSlot) {
            canDiscard = canUse = false;
        }
        jsonPotion.put("can_use", canUse);
        jsonPotion.put("can_discard", canDiscard);
        jsonPotion.put("requires_target", potion.isThrown);
        return jsonPotion;
    }

    /**
     * Creates a GSON-compatible representation of the given orb
     * The orb object contains:
     * "id" (string): The id of the orb
     * "name" (string): The name of the orb, in the currently selected language
     * "evoke_amount" (int): The evoke amount of the orb
     * "passive_amount" (int): The passive amount of the orb
     * @param orb The orb to convert
     * @return An orb object
     */
    private static HashMap<String, Object> convertOrbToJson(AbstractOrb orb) {
        HashMap<String, Object> jsonOrb =  new HashMap<>();
        jsonOrb.put("id", orb.ID);
        jsonOrb.put("name", orb.name);
        jsonOrb.put("evoke_amount", orb.evokeAmount);
        jsonOrb.put("passive_amount", orb.passiveAmount);
        return jsonOrb;
    }

}
