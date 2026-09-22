package communicationmod.patches;

import com.megacrit.cardcrawl.rewards.RewardItem;
import com.megacrit.cardcrawl.screens.CombatRewardScreen;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.evacipated.cardcrawl.modthespire.lib.*;
import com.evacipated.cardcrawl.modthespire.patcher.PatchingException;
import communicationmod.GameStateListener;
import javassist.CannotCompileException;
import javassist.CtBehavior;

import java.util.ArrayList;

@SpirePatch(
        clz= CombatRewardScreen.class,
        method="rewardViewUpdate"
)
public class CombatRewardScreenPatch {

    private static RewardItem pendingReward = null;
    private static int pendingFrames = 0;

    public static void waitForReward(RewardItem reward) {
        pendingReward = reward;
        pendingFrames = 0;
    }


    @SpireInsertPatch(
            locator=Locator.class
    )
    public static void Insert(CombatRewardScreen _instance) {
        // This will deal with linked relics / keys
        for(RewardItem reward : _instance.rewards) {
            if (reward.isDone) {
                return;
            }
        }
        GameStateListener.registerStateChange();
    }

    @SpirePostfixPatch
    public static void Postfix(CombatRewardScreen _instance) {
        if (pendingReward == null) {
            return;
        }
        pendingFrames += 1;
        // CARD rewards remain in CombatRewardScreen.rewards while the newly
        // opened CardRewardScreen is active.  rewardViewUpdate then stops
        // running, so waiting only for list removal deadlocks state output.
        // A transition away from COMBAT_REWARD is equally authoritative: the
        // exact bound RewardItem was consumed to open that next screen.
        boolean consumed = !_instance.rewards.contains(pendingReward)
                || AbstractDungeon.screen != AbstractDungeon.CurrentScreen.COMBAT_REWARD;
        boolean fallback = pendingFrames >= 120;
        if (consumed || fallback) {
            pendingReward = null;
            pendingFrames = 0;
            GameStateListener.resumeStateUpdate();
            GameStateListener.registerStateChange();
            // Let any newly opened card/relic screen finish its own update
            // before publishing the completion frame.
            GameStateListener.setTimeout(1);
        }
    }

    private static class Locator extends SpireInsertLocator {
        public int[] Locate(CtBehavior ctMethodToPatch) throws CannotCompileException, PatchingException {
            Matcher matcher = new Matcher.MethodCallMatcher(CombatRewardScreen.class, "setLabel");
            return LineFinder.findInOrder(ctMethodToPatch, new ArrayList<Matcher>(), matcher);
        }
    }
}
