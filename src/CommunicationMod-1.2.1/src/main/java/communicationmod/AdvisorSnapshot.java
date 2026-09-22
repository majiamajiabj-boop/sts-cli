package communicationmod;

import com.google.gson.Gson;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.HashMap;
import java.util.UUID;

/** Passive observer. No command executor, input reader or external process. */
public final class AdvisorSnapshot {
    private static final boolean ENABLED = "advisor".equals(System.getProperty("sts.assistant.mode"));
    private static final String SESSION = UUID.randomUUID().toString();
    private static long lastWrite = 0;
    private static long sequence = 0;
    private static String previous = "";
    private static final Gson GSON = new Gson();
    public static boolean enabled() { return ENABLED; }

    public static void update() {
        long now = System.currentTimeMillis();
        if (!ENABLED || now - lastWrite < 100) return;
        lastWrite = now;
        try {
            String raw = GameStateConverter.getCommunicationState();
            boolean ready = GameStateListener.isAdvisorReady();
            String signature = ready + raw;
            if (!signature.equals(previous)) { sequence++; previous = signature; }
            HashMap<String, Object> envelope = new HashMap<>();
            envelope.put("schema_version", 1);
            envelope.put("mode", "advisor");
            envelope.put("session", SESSION);
            envelope.put("state_seq", sequence);
            envelope.put("observed_at", now / 1000.0);
            envelope.put("ready", ready);
            envelope.put("state", GSON.fromJson(raw, Object.class));
            Path target = Paths.get(System.getProperty("sts.assistant.state"));
            Path temp = target.resolveSibling(target.getFileName() + ".tmp");
            Files.write(temp, GSON.toJson(envelope).getBytes(StandardCharsets.UTF_8));
            try {
                Files.move(temp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException ex) {
                Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (Exception ex) {
            System.err.println("Advisor snapshot unavailable: " + ex.getClass().getSimpleName());
        }
    }
}
