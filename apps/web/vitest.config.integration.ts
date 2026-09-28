import { defineConfig } from "vitest/config";

// Separate config from vitest.config.ts so `npm test` (lib/ unit tests)
// stays mock-only and `npm run test:integration` exercises the running
// fishsense-lite-web container against the local stack. Tests assume the
// container is already up at FISHSENSE_WEB_URL (default localhost:3000)
// — same shape as the Python integration tests, which assume their
// services are already on localhost ports.
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/integration/**/*.test.ts"],
    testTimeout: 30_000,
    hookTimeout: 90_000,
  },
});
