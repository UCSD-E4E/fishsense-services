import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
  test: {
    environment: "node",
    // `app/**` as well as `lib/**`: server actions live under app/ and are
    // real logic, not rendering — `app/portal/calibration/actions.ts` re-checks the
    // session on every call because server actions are public endpoints.
    // While this pattern was lib-only, a test placed next to such a module
    // was silently never collected. `tests/integration/**` stays out; it has
    // its own config and needs a running container.
    include: ["lib/**/*.test.ts", "app/**/*.test.ts"],
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
  },
});
