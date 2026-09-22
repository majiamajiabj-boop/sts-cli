package communicationmod;

import com.megacrit.cardcrawl.events.exordium.GoopPuddle;
import com.megacrit.cardcrawl.events.AbstractEvent;
import com.megacrit.cardcrawl.events.city.CursedTome;
import com.megacrit.cardcrawl.events.city.KnowingSkull;
import com.megacrit.cardcrawl.events.city.TheMausoleum;
import com.megacrit.cardcrawl.events.city.Vampires;
import com.megacrit.cardcrawl.events.exordium.Cleric;
import com.megacrit.cardcrawl.events.exordium.DeadAdventurer;
import com.megacrit.cardcrawl.events.exordium.ScrapOoze;
import com.megacrit.cardcrawl.events.exordium.Sssserpent;
import com.megacrit.cardcrawl.events.shrines.Designer;
import com.megacrit.cardcrawl.events.shrines.Bonfire;
import com.megacrit.cardcrawl.events.shrines.Duplicator;
import com.megacrit.cardcrawl.events.shrines.FaceTrader;
import com.megacrit.cardcrawl.events.shrines.GoldShrine;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;

/** Plain-Java contract test; run with assertions enabled after mvn test-compile. */
public final class GameStateConverterEventContractTest {
    private static Field requirePrivateField(
            Class<?> owner, String name, Class<?> type)
            throws ReflectiveOperationException {
        Field field = owner.getDeclaredField(name);
        assert field.getType().equals(type) : owner.getName() + "." + name;
        assert Modifier.isPrivate(field.getModifiers()) : owner.getName() + "." + name;
        return field;
    }

    private static ArrayList<HashMap<String, Object>> contracts(
            String eventClassName,
            String screenClassName,
            String screenName,
            int buttonCount,
            boolean fieldsArePrivate,
            Class<?> damageType,
            Object damage,
            Class<?> goldType,
            Object gold,
            Class<?> goldLossType,
            Object goldLoss) {
        return GameStateConverter.buildWorldOfGoopContracts(
                eventClassName,
                screenClassName,
                screenName,
                buttonCount,
                fieldsArePrivate,
                damageType,
                damage,
                goldType,
                gold,
                goldLossType,
                goldLoss);
    }

    private static ArrayList<String> pool(String... relicIds) {
        return new ArrayList<String>(Arrays.asList(relicIds));
    }

    private static void requireEnumNames(Class<?> enumClass, String... names) {
        Object[] constants = enumClass.getEnumConstants();
        assert constants != null;
        assert constants.length == names.length;
        for (int index = 0; index < names.length; index += 1) {
            assert names[index].equals(((Enum<?>) constants[index]).name());
        }
    }

    private static void testCleric() throws Exception {
        Field screenNum = AbstractEvent.class.getDeclaredField("screenNum");
        assert screenNum.getType().equals(int.class);
        assert Modifier.isProtected(screenNum.getModifiers());
        requirePrivateField(Cleric.class, "purifyCost", int.class);
        requirePrivateField(Cleric.class, "healAmt", int.class);

        ArrayList<HashMap<String, Object>> main =
                GameStateConverter.buildClericContracts(
                        Cleric.class.getName(),
                        screenNum.getDeclaringClass().getName(),
                        3, true, true,
                        int.class, 0,
                        int.class, 50,
                        int.class, 20);
        assert main.size() == 3;
        assert "MAIN".equals(main.get(0).get("event_stage"));
        assert "HEAL".equals(main.get(0).get("option_kind"));
        assert "PURIFY".equals(main.get(1).get("option_kind"));
        assert "LEAVE".equals(main.get(2).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> heal =
                (HashMap<String, Object>) main.get(0).get("parameters");
        assert Integer.valueOf(20).equals(heal.get("heal_amount"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> purify =
                (HashMap<String, Object>) main.get(1).get("parameters");
        assert Integer.valueOf(50).equals(
                purify.get("gold_cost_if_purgeable"));

        ArrayList<HashMap<String, Object>> result =
                GameStateConverter.buildClericContracts(
                        Cleric.class.getName(),
                        screenNum.getDeclaringClass().getName(),
                        1, true, true,
                        int.class, 99,
                        int.class, 75,
                        int.class, 20);
        assert result.size() == 1;
        assert "RESULT".equals(result.get(0).get("event_stage"));
        assert "CONTINUE".equals(result.get(0).get("option_kind"));
        assert ((HashMap<?, ?>) result.get(0).get("parameters")).isEmpty();

        assert GameStateConverter.buildClericContracts(
                "modded.Cleric", screenNum.getDeclaringClass().getName(),
                3, true, true, int.class, 0, int.class, 50,
                int.class, 20).isEmpty();
        assert GameStateConverter.buildClericContracts(
                Cleric.class.getName(), "modded.AbstractEvent",
                3, true, true, int.class, 0, int.class, 50,
                int.class, 20).isEmpty();
        assert GameStateConverter.buildClericContracts(
                Cleric.class.getName(), screenNum.getDeclaringClass().getName(),
                3, false, true, int.class, 0, int.class, 50,
                int.class, 20).isEmpty();
        assert GameStateConverter.buildClericContracts(
                Cleric.class.getName(), screenNum.getDeclaringClass().getName(),
                3, true, true, long.class, 0L, int.class, 50,
                int.class, 20).isEmpty();
        assert GameStateConverter.buildClericContracts(
                Cleric.class.getName(), screenNum.getDeclaringClass().getName(),
                3, true, true, int.class, 0, int.class, 60,
                int.class, 20).isEmpty();
    }

    private static ArrayList<HashMap<String, Object>> designer(
            String stage, int buttonCount, boolean adjustment,
            boolean cleanUp, int adjustCost, int cleanUpCost,
            int fullServiceCost, int hpLoss) {
        return GameStateConverter.buildDesignerContracts(
                Designer.class.getName(),
                "com.megacrit.cardcrawl.events.shrines.Designer$CurrentScreen",
                stage,
                "com.megacrit.cardcrawl.events.shrines.Designer$OptionChosen",
                buttonCount, true,
                boolean.class, adjustment,
                boolean.class, cleanUp,
                int.class, adjustCost,
                int.class, cleanUpCost,
                int.class, fullServiceCost,
                int.class, hpLoss);
    }

    private static void testDesigner() throws Exception {
        Field screen = requirePrivateField(
                Designer.class, "curScreen",
                Class.forName(
                        "com.megacrit.cardcrawl.events.shrines.Designer$CurrentScreen"));
        Field option = requirePrivateField(
                Designer.class, "option",
                Class.forName(
                        "com.megacrit.cardcrawl.events.shrines.Designer$OptionChosen"));
        requireEnumNames(screen.getType(), "INTRO", "MAIN", "DONE");
        requireEnumNames(
                option.getType(), "UPGRADE", "REMOVE", "REMOVE_AND_UPGRADE",
                "TRANSFORM", "NONE");
        requirePrivateField(Designer.class, "adjustmentUpgradesOne", boolean.class);
        requirePrivateField(Designer.class, "cleanUpRemovesCards", boolean.class);
        requirePrivateField(Designer.class, "adjustCost", int.class);
        requirePrivateField(Designer.class, "cleanUpCost", int.class);
        requirePrivateField(Designer.class, "fullServiceCost", int.class);
        requirePrivateField(Designer.class, "hpLoss", int.class);

        ArrayList<HashMap<String, Object>> main = designer(
                "MAIN", 4, true, false, 40, 60, 90, 3);
        assert main.size() == 4;
        assert "ADJUSTMENT_GRID_UPGRADE".equals(
                main.get(0).get("option_kind"));
        assert "CLEAN_UP_GRID_TRANSFORM".equals(
                main.get(1).get("option_kind"));
        assert "FULL_SERVICE".equals(main.get(2).get("option_kind"));
        assert "PUNCH_AND_LEAVE".equals(main.get(3).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> cleanUp =
                (HashMap<String, Object>) main.get(1).get("parameters");
        assert Integer.valueOf(2).equals(
                cleanUp.get("transform_select_count"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> instance =
                (HashMap<String, Object>) main.get(0).get("instance_parameters");
        assert Boolean.TRUE.equals(instance.get("adjustment_upgrades_one"));
        assert Boolean.FALSE.equals(instance.get("clean_up_removes_cards"));

        ArrayList<HashMap<String, Object>> alternate = designer(
                "MAIN", 4, false, true, 50, 75, 110, 5);
        assert "ADJUSTMENT_RANDOM_UPGRADE".equals(
                alternate.get(0).get("option_kind"));
        assert "CLEAN_UP_GRID_PURGE".equals(
                alternate.get(1).get("option_kind"));
        assert designer("INTRO", 1, true, true, 40, 60, 90, 3).size() == 1;
        ArrayList<HashMap<String, Object>> done = designer(
                "DONE", 1, true, true, 40, 60, 90, 3);
        assert done.size() == 1;
        assert "CONTINUE".equals(done.get(0).get("option_kind"));
        assert ((HashMap<?, ?>) done.get(0).get("parameters")).isEmpty();

        assert designer("MAIN", 3, true, true, 40, 60, 90, 3).isEmpty();
        assert designer("UNKNOWN", 1, true, true, 40, 60, 90, 3).isEmpty();
        assert designer("MAIN", 4, true, true, 41, 60, 90, 3).isEmpty();
        assert GameStateConverter.buildDesignerContracts(
                Designer.class.getName(), screen.getType().getName(), "MAIN",
                option.getType().getName(), 4, false,
                boolean.class, true, boolean.class, true,
                int.class, 40, int.class, 60, int.class, 90,
                int.class, 3).isEmpty();
    }

    private static ArrayList<HashMap<String, Object>> cursedTome(
            String stage, int buttonCount, int finalDmg, int damageTaken,
            ArrayList<String> randomBookPool) {
        return GameStateConverter.buildCursedTomeContracts(
                CursedTome.class.getName(),
                "com.megacrit.cardcrawl.events.city.CursedTome$CurScreen",
                stage, buttonCount, true,
                int.class, finalDmg,
                int.class, damageTaken,
                randomBookPool);
    }

    private static void testCursedTome() throws Exception {
        Field screen = requirePrivateField(
                CursedTome.class, "screen",
                Class.forName(
                        "com.megacrit.cardcrawl.events.city.CursedTome$CurScreen"));
        requireEnumNames(
                screen.getType(), "INTRO", "PAGE_1", "PAGE_2", "PAGE_3",
                "LAST_PAGE", "END");
        requirePrivateField(CursedTome.class, "finalDmg", int.class);
        requirePrivateField(CursedTome.class, "damageTaken", int.class);
        ArrayList<String> allBooks = pool(
                "Necronomicon", "Enchiridion", "Nilry's Codex");

        ArrayList<HashMap<String, Object>> intro = cursedTome(
                "INTRO", 2, 10, 0, allBooks);
        assert intro.size() == 2;
        assert "ENTER_RANDOM_BOOK_CHAIN".equals(
                intro.get(0).get("option_kind"));
        assert "LEAVE".equals(intro.get(1).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> enter =
                (HashMap<String, Object>) intro.get(0).get("parameters");
        assert Integer.valueOf(16).equals(
                enter.get("future_hp_loss_to_complete"));
        assert cursedTome("PAGE_1", 1, 10, 0, allBooks).size() == 1;
        assert cursedTome("PAGE_2", 1, 10, 1, allBooks).size() == 1;
        assert cursedTome("PAGE_3", 1, 10, 3, allBooks).size() == 1;
        ArrayList<HashMap<String, Object>> last = cursedTome(
                "LAST_PAGE", 2, 10, 6, allBooks);
        assert last.size() == 2;
        assert "COMPLETE_RANDOM_BOOK".equals(last.get(0).get("option_kind"));
        assert "STOP".equals(last.get(1).get("option_kind"));
        ArrayList<HashMap<String, Object>> end = cursedTome(
                "END", 1, 10, 16, allBooks);
        assert end.size() == 1;
        assert "PROCEED".equals(end.get(0).get("option_kind"));
        assert ((HashMap<?, ?>) end.get(0).get("parameters")).isEmpty();
        assert cursedTome(
                "LAST_PAGE", 2, 15, 6,
                pool("Necronomicon", "Nilry's Codex")).size() == 2;
        assert cursedTome("END", 1, 10, 9, pool("Circlet")).size() == 1;

        assert cursedTome("INTRO", 2, 11, 0, allBooks).isEmpty();
        assert cursedTome("PAGE_2", 1, 10, 0, allBooks).isEmpty();
        assert cursedTome("LAST_PAGE", 1, 10, 6, allBooks).isEmpty();
        assert cursedTome("END", 1, 10, 7, allBooks).isEmpty();
        assert cursedTome("INTRO", 2, 10, 0, pool()).isEmpty();
        assert cursedTome(
                "INTRO", 2, 10, 0,
                pool("Enchiridion", "Necronomicon")).isEmpty();
        assert GameStateConverter.buildCursedTomeContracts(
                CursedTome.class.getName(), screen.getType().getName(),
                "INTRO", 2, false, int.class, 10, int.class, 0,
                allBooks).isEmpty();
    }

    private static void testMausoleum() throws Exception {
        Field screen = requirePrivateField(
                TheMausoleum.class, "screen",
                Class.forName(
                        "com.megacrit.cardcrawl.events.city.TheMausoleum$CurScreen"));
        requireEnumNames(screen.getType(), "INTRO", "RESULT");
        requirePrivateField(TheMausoleum.class, "percent", int.class);

        ArrayList<HashMap<String, Object>> intro =
                GameStateConverter.buildMausoleumContracts(
                        TheMausoleum.class.getName(),
                        screen.getType().getName(), "INTRO", 2, true,
                        int.class, 50);
        assert intro.size() == 2;
        assert "OPEN".equals(intro.get(0).get("option_kind"));
        assert "LEAVE".equals(intro.get(1).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> open =
                (HashMap<String, Object>) intro.get(0).get("parameters");
        assert "Writhe".equals(open.get("curse_card_id"));
        assert Integer.valueOf(50).equals(
                open.get("curse_probability_percent"));
        assert Integer.valueOf(1).equals(open.get("random_relic_count"));

        ArrayList<HashMap<String, Object>> result =
                GameStateConverter.buildMausoleumContracts(
                        TheMausoleum.class.getName(),
                        screen.getType().getName(), "RESULT", 1, true,
                        int.class, 100);
        assert result.size() == 1;
        assert "CONTINUE".equals(result.get(0).get("option_kind"));
        assert ((HashMap<?, ?>) result.get(0).get("parameters")).isEmpty();

        assert GameStateConverter.buildMausoleumContracts(
                "modded.TheMausoleum", screen.getType().getName(),
                "INTRO", 2, true, int.class, 50).isEmpty();
        assert GameStateConverter.buildMausoleumContracts(
                TheMausoleum.class.getName(), "modded.Screen",
                "INTRO", 2, true, int.class, 50).isEmpty();
        assert GameStateConverter.buildMausoleumContracts(
                TheMausoleum.class.getName(), screen.getType().getName(),
                "INTRO", 2, false, int.class, 50).isEmpty();
        assert GameStateConverter.buildMausoleumContracts(
                TheMausoleum.class.getName(), screen.getType().getName(),
                "INTRO", 2, true, long.class, 50L).isEmpty();
        assert GameStateConverter.buildMausoleumContracts(
                TheMausoleum.class.getName(), screen.getType().getName(),
                "INTRO", 2, true, int.class, 75).isEmpty();
        assert GameStateConverter.buildMausoleumContracts(
                TheMausoleum.class.getName(), screen.getType().getName(),
                "RESULT", 2, true, int.class, 50).isEmpty();
    }

    private static ArrayList<HashMap<String, Object>> knowingSkull(
            String stage, int buttonCount, int potionCost, int cardCost,
            int goldCost, int leaveCost) {
        return GameStateConverter.buildKnowingSkullContracts(
                KnowingSkull.class.getName(),
                "com.megacrit.cardcrawl.events.city.KnowingSkull$CurScreen",
                stage, buttonCount, true,
                int.class, potionCost,
                int.class, cardCost,
                int.class, goldCost,
                int.class, leaveCost);
    }

    private static void testKnowingSkull() throws Exception {
        Field screen = requirePrivateField(
                KnowingSkull.class, "screen",
                Class.forName(
                        "com.megacrit.cardcrawl.events.city.KnowingSkull$CurScreen"));
        requireEnumNames(screen.getType(), "INTRO_1", "ASK", "COMPLETE");
        requirePrivateField(KnowingSkull.class, "potionCost", int.class);
        requirePrivateField(KnowingSkull.class, "cardCost", int.class);
        requirePrivateField(KnowingSkull.class, "goldCost", int.class);
        requirePrivateField(KnowingSkull.class, "leaveCost", int.class);
        Field goldReward = requirePrivateField(
                KnowingSkull.class, "GOLD_REWARD", int.class);
        goldReward.setAccessible(true);
        assert goldReward.getInt(null) == 90;

        ArrayList<HashMap<String, Object>> ask = knowingSkull(
                "ASK", 4, 6, 8, 9, 6);
        assert ask.size() == 4;
        assert "TAKE_POTION".equals(ask.get(0).get("option_kind"));
        assert "TAKE_GOLD".equals(ask.get(1).get("option_kind"));
        assert "TAKE_CARD".equals(ask.get(2).get("option_kind"));
        assert "LEAVE".equals(ask.get(3).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> instance =
                (HashMap<String, Object>) ask.get(0).get("instance_parameters");
        assert Integer.valueOf(6).equals(instance.get("potion_cost"));
        assert Integer.valueOf(8).equals(instance.get("card_cost"));
        assert Integer.valueOf(9).equals(instance.get("gold_cost"));
        assert Integer.valueOf(6).equals(instance.get("leave_cost"));
        assert Integer.valueOf(90).equals(instance.get("gold_reward"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> gold =
                (HashMap<String, Object>) ask.get(1).get("parameters");
        assert Integer.valueOf(9).equals(gold.get("hp_loss"));
        assert Integer.valueOf(90).equals(gold.get("gold_gain"));

        ArrayList<HashMap<String, Object>> intro = knowingSkull(
                "INTRO_1", 1, 6, 6, 6, 6);
        assert intro.size() == 1;
        assert "OPEN_QUESTIONS".equals(intro.get(0).get("option_kind"));
        assert ((HashMap<?, ?>) intro.get(0).get("parameters")).isEmpty();
        ArrayList<HashMap<String, Object>> complete = knowingSkull(
                "COMPLETE", 1, 7, 6, 10, 6);
        assert complete.size() == 1;
        assert "CONTINUE".equals(complete.get(0).get("option_kind"));

        assert GameStateConverter.buildKnowingSkullContracts(
                "modded.KnowingSkull", screen.getType().getName(),
                "ASK", 4, true,
                int.class, 6, int.class, 6,
                int.class, 6, int.class, 6).isEmpty();
        assert GameStateConverter.buildKnowingSkullContracts(
                KnowingSkull.class.getName(), "modded.Screen",
                "ASK", 4, true,
                int.class, 6, int.class, 6,
                int.class, 6, int.class, 6).isEmpty();
        assert GameStateConverter.buildKnowingSkullContracts(
                KnowingSkull.class.getName(), screen.getType().getName(),
                "ASK", 4, false,
                int.class, 6, int.class, 6,
                int.class, 6, int.class, 6).isEmpty();
        assert GameStateConverter.buildKnowingSkullContracts(
                KnowingSkull.class.getName(), screen.getType().getName(),
                "ASK", 4, true,
                long.class, 6L, int.class, 6,
                int.class, 6, int.class, 6).isEmpty();
        assert knowingSkull("ASK", 3, 6, 6, 6, 6).isEmpty();
        assert knowingSkull("ASK", 4, 6, 6, 6, 0).isEmpty();
        assert knowingSkull("UNKNOWN", 1, 6, 6, 6, 6).isEmpty();
    }

    private static void testDeadAdventurer() throws Exception {
        Field screen = requirePrivateField(
                DeadAdventurer.class, "screen",
                Class.forName(DeadAdventurer.class.getName() + "$CUR_SCREEN"));
        requirePrivateField(DeadAdventurer.class, "numRewards", int.class);
        requirePrivateField(DeadAdventurer.class, "encounterChance", int.class);
        requirePrivateField(DeadAdventurer.class, "rewards", ArrayList.class);
        requirePrivateField(DeadAdventurer.class, "enemy", int.class);
        ArrayList<String> rewards = new ArrayList<>(
                Arrays.asList("GOLD", "RELIC", "NOTHING"));
        ArrayList<HashMap<String, Object>> intro =
                GameStateConverter.buildDeadAdventurerContracts(
                        DeadAdventurer.class.getName(), screen.getType().getName(),
                        "INTRO", 2, true,
                        int.class, 0, int.class, 25,
                        ArrayList.class, rewards, int.class, 1);
        assert intro.size() == 2;
        assert "SEARCH".equals(intro.get(0).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> search =
                (HashMap<String, Object>) intro.get(0).get("parameters");
        assert "GOLD".equals(search.get("success_reward_kind"));
        assert "Gremlin Nob".equals(search.get("encounter_id"));
        assert Integer.valueOf(30).equals(search.get("success_gold_gain"));

        ArrayList<HashMap<String, Object>> fight =
                GameStateConverter.buildDeadAdventurerContracts(
                        DeadAdventurer.class.getName(), screen.getType().getName(),
                        "FAIL", 1, true,
                        int.class, 0, int.class, 25,
                        ArrayList.class, rewards, int.class, 1);
        assert fight.size() == 1;
        assert "FIGHT".equals(fight.get(0).get("option_kind"));
        assert GameStateConverter.buildDeadAdventurerContracts(
                DeadAdventurer.class.getName(), screen.getType().getName(),
                "INTRO", 2, true,
                int.class, 1, int.class, 25,
                ArrayList.class, rewards, int.class, 1).isEmpty();
    }

    private static void testScrapOoze() throws Exception {
        requirePrivateField(ScrapOoze.class, "relicObtainChance", int.class);
        requirePrivateField(ScrapOoze.class, "dmg", int.class);
        requirePrivateField(ScrapOoze.class, "totalDamageDealt", int.class);
        requirePrivateField(ScrapOoze.class, "screenNum", int.class);
        ArrayList<HashMap<String, Object>> main =
                GameStateConverter.buildScrapOozeContracts(
                        ScrapOoze.class.getName(), 2, true,
                        int.class, 45, int.class, 5,
                        int.class, 7, int.class, 0);
        assert main.size() == 2;
        assert "REACH_INSIDE".equals(main.get(0).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> reach =
                (HashMap<String, Object>) main.get(0).get("parameters");
        assert Integer.valueOf(54).equals(
                reach.get("success_roll_min_inclusive"));
        ArrayList<HashMap<String, Object>> guaranteed =
                GameStateConverter.buildScrapOozeContracts(
                        ScrapOoze.class.getName(), 2, true,
                        int.class, 105, int.class, 11,
                        int.class, 52, int.class, 0);
        assert guaranteed.size() == 2;
        @SuppressWarnings("unchecked")
        HashMap<String, Object> guaranteedReach =
                (HashMap<String, Object>) guaranteed.get(0).get("parameters");
        assert Integer.valueOf(-6).equals(
                guaranteedReach.get("success_roll_min_inclusive"));
        assert GameStateConverter.buildScrapOozeContracts(
                ScrapOoze.class.getName(), 2, true,
                int.class, 44, int.class, 5,
                int.class, 7, int.class, 0).isEmpty();
    }

    private static void testFaceTrader() throws Exception {
        Field screen = requirePrivateField(
                FaceTrader.class, "screen",
                Class.forName(FaceTrader.class.getName() + "$CurScreen"));
        Field gold = requirePrivateField(FaceTrader.class, "goldReward", int.class);
        Field damage = requirePrivateField(FaceTrader.class, "damage", int.class);
        assert Modifier.isStatic(gold.getModifiers());
        assert Modifier.isStatic(damage.getModifiers());
        ArrayList<String> pool = new ArrayList<>(Arrays.asList(
                "CultistMask", "FaceOfCleric", "GremlinMask",
                "NlothsMask", "SsserpentHead"));
        ArrayList<HashMap<String, Object>> main =
                GameStateConverter.buildFaceTraderContracts(
                        FaceTrader.class.getName(), screen.getType().getName(),
                        "MAIN", 3, true, true,
                        int.class, 75, int.class, 8, pool);
        assert main.size() == 3;
        assert "TOUCH".equals(main.get(0).get("option_kind"));
        assert "TRADE".equals(main.get(1).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> trade =
                (HashMap<String, Object>) main.get(1).get("parameters");
        assert pool.equals(trade.get("random_relic_pool"));
        assert GameStateConverter.buildFaceTraderContracts(
                FaceTrader.class.getName(), screen.getType().getName(),
                "MAIN", 3, true, false,
                int.class, 75, int.class, 8, pool).isEmpty();
    }

    private static void testDuplicator() throws Exception {
        requirePrivateField(Duplicator.class, "screenNum", int.class);
        ArrayList<HashMap<String, Object>> main =
                GameStateConverter.buildDuplicatorContracts(
                        Duplicator.class.getName(), 2, true,
                        int.class, 0);
        assert main.size() == 2;
        assert "DUPLICATE".equals(main.get(0).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> duplicate =
                (HashMap<String, Object>) main.get(0).get("parameters");
        assert Integer.valueOf(1).equals(
                duplicate.get("duplicate_select_count"));
        assert GameStateConverter.buildDuplicatorContracts(
                Duplicator.class.getName(), 1, true,
                int.class, 1).isEmpty();
    }

    private static void testBonfire() throws Exception {
        Field screen = requirePrivateField(
                Bonfire.class, "screen",
                Class.forName(Bonfire.class.getName() + "$CUR_SCREEN"));
        requirePrivateField(Bonfire.class, "cardSelect", boolean.class);

        ArrayList<HashMap<String, Object>> intro =
                GameStateConverter.buildBonfireContracts(
                        Bonfire.class.getName(), screen.getType().getName(),
                        "INTRO", 1, true, boolean.class, false);
        assert intro.size() == 1;
        assert "CONTINUE".equals(intro.get(0).get("option_kind"));

        ArrayList<HashMap<String, Object>> choose =
                GameStateConverter.buildBonfireContracts(
                        Bonfire.class.getName(), screen.getType().getName(),
                        "CHOOSE", 1, true, boolean.class, false);
        assert choose.size() == 1;
        assert "OFFER_CARD".equals(choose.get(0).get("option_kind"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> offer =
                (HashMap<String, Object>) choose.get(0).get("parameters");
        assert Integer.valueOf(1).equals(offer.get("offer_select_count"));
        assert "PLAYER_SELECT_PURGEABLE_UNBOTTLED_CURRENT_DECK".equals(
                offer.get("selection_mode"));

        ArrayList<HashMap<String, Object>> complete =
                GameStateConverter.buildBonfireContracts(
                        Bonfire.class.getName(), screen.getType().getName(),
                        "COMPLETE", 1, true, boolean.class, false);
        assert complete.size() == 1;
        assert "CONTINUE".equals(complete.get(0).get("option_kind"));

        assert GameStateConverter.buildBonfireContracts(
                "modded.Bonfire", screen.getType().getName(), "INTRO",
                1, true, boolean.class, false).isEmpty();
        assert GameStateConverter.buildBonfireContracts(
                Bonfire.class.getName(), "modded.Screen", "INTRO",
                1, true, boolean.class, false).isEmpty();
        assert GameStateConverter.buildBonfireContracts(
                Bonfire.class.getName(), screen.getType().getName(),
                "UNKNOWN", 1, true, boolean.class, false).isEmpty();
        assert GameStateConverter.buildBonfireContracts(
                Bonfire.class.getName(), screen.getType().getName(),
                "CHOOSE", 1, true, boolean.class, true).isEmpty();
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && "bonfire".equals(args[0])) {
            testBonfire();
            return;
        }
        Field screen = GoopPuddle.class.getDeclaredField("screen");
        assert Modifier.isPrivate(screen.getModifiers());
        assert screen.getType().getName().equals(
                "com.megacrit.cardcrawl.events.exordium.GoopPuddle$CurScreen");
        requirePrivateField(GoopPuddle.class, "damage", int.class);
        requirePrivateField(GoopPuddle.class, "gold", int.class);
        requirePrivateField(GoopPuddle.class, "goldLoss", int.class);
        Field serpentScreen = Sssserpent.class.getDeclaredField("screen");
        assert Modifier.isPrivate(serpentScreen.getModifiers());
        assert serpentScreen.getType().isEnum();
        requirePrivateField(Vampires.class, "screenNum", int.class);
        Field shrineScreen = GoldShrine.class.getDeclaredField("screen");
        assert Modifier.isPrivate(shrineScreen.getModifiers());
        assert shrineScreen.getType().isEnum();

        ArrayList<HashMap<String, Object>> valid = contracts(
                GoopPuddle.class.getName(),
                screen.getType().getName(),
                "INTRO",
                2,
                true,
                int.class,
                11,
                int.class,
                75,
                int.class,
                27);
        assert valid.size() == 2;
        HashMap<String, Object> gather = valid.get(0);
        HashMap<String, Object> leave = valid.get(1);
        assert "BASE_GAME_EVENT_OPTION".equals(gather.get("contract_kind"));
        assert "World of Goop".equals(gather.get("event_id"));
        assert "GATHER".equals(gather.get("option_kind"));
        assert Integer.valueOf(0).equals(gather.get("original_button_index"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> gatherParameters =
                (HashMap<String, Object>) gather.get("parameters");
        assert Integer.valueOf(11).equals(gatherParameters.get("hp_damage"));
        assert Integer.valueOf(75).equals(gatherParameters.get("gold_gain"));
        assert "LEAVE".equals(leave.get("option_kind"));
        assert Integer.valueOf(1).equals(leave.get("original_button_index"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> leaveParameters =
                (HashMap<String, Object>) leave.get("parameters");
        assert Integer.valueOf(27).equals(leaveParameters.get("gold_loss"));

        // Every reflection/shape drift path omits the whole contract rather
        // than deriving effects from labels or button container order.
        assert contracts(
                "modded.GoopPuddle", screen.getType().getName(), "INTRO",
                2, true, int.class, 11, int.class, 75, int.class, 27).isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), "modded.Screen", "INTRO",
                2, true, int.class, 11, int.class, 75, int.class, 27).isEmpty();
        ArrayList<HashMap<String, Object>> result = contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "RESULT",
                1, true, int.class, 11, int.class, 75, int.class, 27);
        assert result.size() == 1;
        assert "CONTINUE".equals(result.get(0).get("option_kind"));
        assert Integer.valueOf(0).equals(
                result.get(0).get("original_button_index"));
        @SuppressWarnings("unchecked")
        HashMap<String, Object> resultParameters =
                (HashMap<String, Object>) result.get(0).get("parameters");
        assert resultParameters.isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "RESULT",
                2, true, int.class, 11, int.class, 75, int.class, 27).isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "UNKNOWN",
                1, true, int.class, 11, int.class, 75, int.class, 27).isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "INTRO",
                3, true, int.class, 11, int.class, 75, int.class, 27).isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "INTRO",
                2, false, int.class, 11, int.class, 75, int.class, 27).isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "INTRO",
                2, true, long.class, 11L, int.class, 75, int.class, 27).isEmpty();
        assert contracts(
                GoopPuddle.class.getName(), screen.getType().getName(), "INTRO",
                2, true, int.class, 11, int.class, 75, int.class, null).isEmpty();
        testCleric();
        testDesigner();
        testCursedTome();
        testMausoleum();
        testKnowingSkull();
        testDeadAdventurer();
        testScrapOoze();
        testFaceTrader();
        testDuplicator();
        testBonfire();
    }
}
