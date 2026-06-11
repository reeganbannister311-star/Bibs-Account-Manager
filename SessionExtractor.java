package org.bibs.dreambot;

import org.dreambot.api.script.AbstractScript;
import org.dreambot.api.script.ScriptManifest;
import org.dreambot.api.script.Category;
import java.io.File;
import java.io.FileWriter;
import java.lang.reflect.Method;
import java.lang.reflect.Field;
import java.lang.reflect.Constructor;
import java.util.*;

@ScriptManifest(
    name = "Session Extractor",
    author = "Bibs",
    version = 1.0,
    description = "Extracts session IDs from DreamBot AccountManager",
    category = Category.MISC
)
public class SessionExtractor extends AbstractScript {
    private static final File OUTPUT = new File(
        System.getProperty("user.home"),
        "DreamBot\\BotData\\sessions.json"
    );

    @Override
    public void onStart() {
        log("Session Extractor starting...");
        try {
            List<Map<String, String>> accounts = extractSessions();
            String json = toJson(accounts);
            try (FileWriter fw = new FileWriter(OUTPUT)) {
                fw.write(json);
            }
            log("Extracted " + accounts.size() + " account(s) to: " + OUTPUT.getAbsolutePath());
        } catch (Exception e) {
            log("ERROR: " + e.getMessage());
            e.printStackTrace();
        }
        stop();
    }

    @Override
    public int onLoop() {
        return 1000;
    }

    private String toJson(List<Map<String, String>> list) {
        StringBuilder sb = new StringBuilder();
        sb.append("[\n");
        for (int i = 0; i < list.size(); i++) {
            Map<String, String> map = list.get(i);
            sb.append("  {\n");
            int j = 0;
            for (Map.Entry<String, String> e : map.entrySet()) {
                sb.append("    \"").append(escapeJson(e.getKey())).append("\": \"").append(escapeJson(e.getValue())).append("\"");
                if (++j < map.size()) sb.append(",");
                sb.append("\n");
            }
            sb.append("  }");
            if (i + 1 < list.size()) sb.append(",");
            sb.append("\n");
        }
        sb.append("]");
        return sb.toString();
    }

    private String escapeJson(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");
    }

    private List<Map<String, String>> extractSessions() throws Exception {
        List<Map<String, String>> result = new ArrayList<>();
        Object am = resolveAM();
        if (am == null) {
            log("AccountManager not found");
            return result;
        }
        log("AccountManager resolved: " + am.getClass().getName());

        // Try getAll()
        Collection<?> all = null;
        try {
            Method getAll = am.getClass().getMethod("getAll");
            all = (Collection<?>) getAll.invoke(am);
        } catch (Exception e) {
            log("getAll() failed: " + e.getMessage());
        }

        if (all == null || all.isEmpty()) {
            log("No accounts returned by getAll(), trying field inspection...");
            inspectAndExtract(am, result);
            return result;
        }

        log("Found " + all.size() + " account(s) via getAll()");
        for (Object acc : all) {
            Map<String, String> data = extractAccount(acc);
            if (data != null && !data.isEmpty()) {
                result.add(data);
            }
        }
        return result;
    }

    private void inspectAndExtract(Object am, List<Map<String, String>> result) throws Exception {
        Field[] fields = am.getClass().getDeclaredFields();
        for (Field f : fields) {
            f.setAccessible(true);
            Object val = f.get(am);
            if (val == null) continue;
            if (val instanceof Map) {
                Map<?, ?> map = (Map<?, ?>) val;
                log("Field " + f.getName() + " is Map size=" + map.size());
                for (Map.Entry<?, ?> e : map.entrySet()) {
                    Map<String, String> data = extractAccount(e.getValue());
                    if (data != null && !data.isEmpty()) result.add(data);
                }
            } else if (val instanceof Collection) {
                Collection<?> col = (Collection<?>) val;
                log("Field " + f.getName() + " is Collection size=" + col.size());
                for (Object o : col) {
                    Map<String, String> data = extractAccount(o);
                    if (data != null && !data.isEmpty()) result.add(data);
                }
            } else if (val.getClass().isArray()) {
                int len = java.lang.reflect.Array.getLength(val);
                log("Field " + f.getName() + " is Array length=" + len);
                for (int i = 0; i < len; i++) {
                    Object o = java.lang.reflect.Array.get(val, i);
                    Map<String, String> data = extractAccount(o);
                    if (data != null && !data.isEmpty()) result.add(data);
                }
            }
        }
    }

    private Map<String, String> extractAccount(Object acc) {
        if (acc == null) return null;
        Map<String, String> data = new LinkedHashMap<>();
        Class<?> cls = acc.getClass();

        // Try common getter methods
        String[] getters = {"getUsername","getEmail","getName","getPassword",
            "getBankPin","getPin","getTotp","getToken",
            "getSessionId","getCharacterId","getCharacterID",
            "getDisplayName","getRefreshToken","getAccessToken",
            "getNickname","getType"};
        for (String name : getters) {
            try {
                Method m = cls.getMethod(name);
                Object v = m.invoke(acc);
                if (v != null) {
                    String key = name.replaceFirst("^get", "");
                    key = Character.toLowerCase(key.charAt(0)) + key.substring(1);
                    data.put(key, v.toString());
                }
            } catch (Exception ignored) {}
        }

        // Also try direct field access for common names
        String[] fields = {"username","email","name","password","pin","totp",
            "token","sessionId","characterId","characterID","displayName",
            "refreshToken","accessToken","nickname","type"};
        for (String name : fields) {
            if (data.containsKey(name)) continue;
            try {
                Field f = cls.getDeclaredField(name);
                f.setAccessible(true);
                Object v = f.get(acc);
                if (v != null) {
                    data.put(name, v.toString());
                }
            } catch (Exception ignored) {}
        }

        if (data.isEmpty()) return null;
        return data;
    }

    private Object resolveAM() {
        String[] candidates = {
            "org.dreambot.api.utilities.AccountManager",
            "org.dreambot.api.script.AccountManager",
            "org.dreambot.api.methods.account.AccountManager"
        };
        for (String cn : candidates) {
            try {
                Class<?> cls = Class.forName(cn);
                try {
                    Method getInstance = cls.getMethod("getInstance");
                    return getInstance.invoke(null);
                } catch (Exception e) {
                    try {
                        Method getAccountManager = cls.getMethod("getAccountManager");
                        return getAccountManager.invoke(null);
                    } catch (Exception e2) {
                        Constructor<?> ctor = cls.getDeclaredConstructor();
                        return ctor.newInstance();
                    }
                }
            } catch (Exception ignored) {}
        }
        return null;
    }
}
