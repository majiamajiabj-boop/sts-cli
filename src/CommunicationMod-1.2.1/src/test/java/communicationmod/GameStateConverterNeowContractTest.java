package communicationmod;

import com.megacrit.cardcrawl.neow.NeowEvent;
import com.megacrit.cardcrawl.neow.NeowReward;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.HashMap;

/** Plain-Java contract test; run with assertions enabled after mvn test-compile. */
public final class GameStateConverterNeowContractTest {
    private static HashMap<String, Object> reward(
            String label, String rewardKind, String drawbackKind) {
        HashMap<String, Object> parameters = new HashMap<>();
        parameters.put("hp_bonus", 0);
        parameters.put("cursed", "CURSE".equals(drawbackKind));
        parameters.put("drawback_def_kind", "NONE".equals(drawbackKind) ? null : drawbackKind);

        HashMap<String, Object> contract = new HashMap<>();
        contract.put("contract_version", 1);
        contract.put("contract_kind", "NEOW_REWARD");
        contract.put("reward_kind", rewardKind);
        contract.put("drawback_kind", drawbackKind);
        contract.put("parameters", parameters);

        HashMap<String, Object> evidence = new HashMap<>();
        evidence.put("option_label", label);
        evidence.put("contract", contract);
        return evidence;
    }

    private static ArrayList<String> labels(String... values) {
        ArrayList<String> result = new ArrayList<>();
        for (String value : values) {
            result.add(value);
        }
        return result;
    }

    @SafeVarargs
    private static ArrayList<HashMap<String, Object>> rewards(
            HashMap<String, Object>... values) {
        ArrayList<HashMap<String, Object>> result = new ArrayList<>();
        for (HashMap<String, Object> value : values) {
            result.add(value);
        }
        return result;
    }

    private static void requirePrivateField(Class<?> owner, String name, Class<?> type)
            throws ReflectiveOperationException {
        Field field = owner.getDeclaredField(name);
        assert field.getType().equals(type) : owner.getName() + "." + name;
        assert Modifier.isPrivate(field.getModifiers()) : owner.getName() + "." + name;
    }

    public static void main(String[] args) throws Exception {
        requirePrivateField(NeowEvent.class, "screenNum", int.class);
        requirePrivateField(NeowEvent.class, "rewards", ArrayList.class);
        requirePrivateField(NeowReward.class, "hp_bonus", int.class);
        requirePrivateField(
                NeowReward.class, "drawbackDef",
                NeowReward.NeowRewardDrawbackDef.class);
        assert NeowReward.class.getField("optionLabel").getType().equals(String.class);
        assert NeowReward.class.getField("type").getType().equals(
                NeowReward.NeowRewardType.class);
        assert NeowReward.class.getField("drawback").getType().equals(
                NeowReward.NeowRewardDrawback.class);

        HashMap<String, Object> gold = reward("gold-label", "HUNDRED_GOLD", "NONE");
        HashMap<String, Object> transform = reward(
                "transform-label", "TRANSFORM_CARD", "CURSE");

        ArrayList<HashMap<String, Object>> reordered =
                GameStateConverter.alignNeowContracts(
                        3,
                        labels("transform-label", "gold-label"),
                        rewards(gold, transform));
        assert reordered.size() == 2;
        assert "TRANSFORM_CARD".equals(reordered.get(0).get("reward_kind"));
        assert "HUNDRED_GOLD".equals(reordered.get(1).get("reward_kind"));

        assert GameStateConverter.alignNeowContracts(
                3, labels("gold-label"), rewards(gold, transform)).isEmpty();
        assert GameStateConverter.alignNeowContracts(
                3, labels("wrong-label", "transform-label"),
                rewards(gold, transform)).isEmpty();
        assert GameStateConverter.alignNeowContracts(
                3, labels("gold-label", "gold-label"),
                rewards(gold, transform)).isEmpty();
        assert GameStateConverter.alignNeowContracts(
                3, labels("gold-label"), new ArrayList<HashMap<String, Object>>()).isEmpty();

        // Rewards may already exist while screen 0/1/2/10 is still the one-
        // button intro, and screen 99 is the one-button dismissal.  Neither is
        // a reward activation surface, even if a single reward happens to fit.
        for (int screenNum : new int[]{0, 1, 2, 10, 99}) {
            ArrayList<HashMap<String, Object>> dialog =
                    GameStateConverter.alignNeowContracts(
                            screenNum, labels("gold-label"), rewards(gold));
            assert dialog.size() == 1;
            assert "NEOW_DIALOG_ADVANCE".equals(dialog.get(0).get("contract_kind"));
            assert "NONE".equals(dialog.get(0).get("resource_effect"));
            assert !dialog.get(0).containsKey("reward_kind");
        }
        assert GameStateConverter.alignNeowContracts(
                1, labels("one", "two"), rewards(gold, transform)).isEmpty();

        // A stale private drawback definition must not poison an otherwise
        // authoritative NONE reward, while a real drawback must match.
        assert GameStateConverter.canonicalNeowDrawbackDefKind(
                "NONE", "CURSE") == null;
        assert "CURSE".equals(
                GameStateConverter.canonicalNeowDrawbackDefKind(
                        "CURSE", "CURSE"));
        boolean mismatchRejected = false;
        try {
            GameStateConverter.canonicalNeowDrawbackDefKind(
                    "CURSE", "PERCENT_DAMAGE");
        } catch (IllegalArgumentException expected) {
            mismatchRejected = true;
        }
        assert mismatchRejected;
    }
}
