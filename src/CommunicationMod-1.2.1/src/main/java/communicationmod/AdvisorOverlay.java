package communicationmod;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.badlogic.gdx.graphics.Color;
import com.badlogic.gdx.graphics.g2d.SpriteBatch;
import com.badlogic.gdx.graphics.g2d.BitmapFont;
import com.badlogic.gdx.utils.Align;
import com.megacrit.cardcrawl.core.Settings;
import com.megacrit.cardcrawl.helpers.FontHelper;
import com.megacrit.cardcrawl.helpers.ImageMaster;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;

/** Paints advice inside the game, including exclusive fullscreen. No input hooks. */
public final class AdvisorOverlay {
    private static long lastRead;
    private static JsonObject cached;

    public static boolean fresh(JsonObject value, long now) {
        try {
            double age = now / 1000.0 - value.get("published_at").getAsDouble();
            return value.get("schema_version").getAsInt() == 1
                && value.get("visible").getAsBoolean() && age >= 0 && age <= 2.5;
        } catch (Exception ex) { return false; }
    }

    private static String text(JsonObject value, String key, int max) {
        String result = value.get(key).getAsString().replace('\n', ' ');
        return result.length() <= max ? result : result.substring(0, max) + "…";
    }

    public static void render(SpriteBatch sb) {
        if (!AdvisorSnapshot.enabled()) return;
        Color old = new Color(sb.getColor());
        try {
            long now = System.currentTimeMillis();
            if (now - lastRead >= 100) {
                lastRead = now;
                cached = null;
                Path state = Paths.get(System.getProperty("sts.assistant.state"));
                Path path = state.resolveSibling("advisor-overlay.json");
                if (Files.size(path) <= 16384) {
                    cached = new JsonParser().parse(new String(Files.readAllBytes(path), StandardCharsets.UTF_8)).getAsJsonObject();
                }
            }
            if (cached == null || !fresh(cached, now)
                || !AdvisorSnapshot.matches(cached.get("session").getAsString(), cached.get("state_seq").getAsLong())) return;
            float s = Settings.scale;
            float width = Math.min(500 * s, Settings.WIDTH * .42f);
            float x = Settings.WIDTH - width - 24 * s, top = Settings.HEIGHT - 110 * s;
            BitmapFont font = FontHelper.tipBodyFont;
            String[] lines = {"实时顾问 · " + text(cached, "scene", 16),
                text(cached, "action", 48), "目标：" + text(cached, "target", 48), text(cached, "reason", 100)};
            com.badlogic.gdx.graphics.g2d.GlyphLayout[] layouts = new com.badlogic.gdx.graphics.g2d.GlyphLayout[lines.length];
            float height = 32 * s;
            for (int i = 0; i < lines.length; i++) {
                layouts[i] = new com.badlogic.gdx.graphics.g2d.GlyphLayout(font, lines[i],
                    i == 1 ? Color.WHITE : new Color(.76f, .81f, .87f, 1f), width - 36 * s, Align.left, true);
                height += layouts[i].height + 16 * s;
            }
            sb.setColor(.07f, .08f, .10f, .94f);
            sb.draw(ImageMaster.WHITE_SQUARE_IMG, x, top - height, width, height);
            sb.setColor(.60f, .76f, .89f, 1f);
            sb.draw(ImageMaster.WHITE_SQUARE_IMG, x, top - height, 3 * s, height);
            sb.setColor(Color.WHITE);
            float y = top - 20 * s;
            for (com.badlogic.gdx.graphics.g2d.GlyphLayout layout : layouts) {
                font.draw(sb, layout, x + 18 * s, y);
                y -= layout.height + 16 * s;
            }
        } catch (Exception ex) {
            cached = null; // Missing, incomplete or outdated data always hides advice.
        } finally { sb.setColor(old); }
    }
}
