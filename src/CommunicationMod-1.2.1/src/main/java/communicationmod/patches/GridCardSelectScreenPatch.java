package communicationmod.patches;

import basemod.ReflectionHacks;
import com.evacipated.cardcrawl.modthespire.lib.SpirePatch;
import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.screens.select.GridCardSelectScreen;
import communicationmod.GameStateListener;

@SpirePatch(
        clz = GridCardSelectScreen.class,
        method = "updateCardPositionsAndHoverLogic"
)
public class GridCardSelectScreenPatch {

    public static AbstractCard hoverCard;
    public static boolean replaceHoverCard = false;

    public static void Postfix(GridCardSelectScreen _instance) {
        if(replaceHoverCard) {
            ReflectionHacks.setPrivate(_instance, GridCardSelectScreen.class, "hoveredCard", hoverCard);
            hoverCard.hb.hovered = true;
            hoverCard.hb.clicked = true;
            replaceHoverCard = false;
            // Give the base game two full updates to consume the synthetic
            // click before a new ready-for-command state may be emitted.
            GameStateListener.setTimeout(2);
            GameStateListener.resumeStateUpdate();
        }
    }
}
