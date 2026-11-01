import { defineConfig } from "playwright/test";

// A dedicated built preview avoids development-server reconnects reloading a test page mid-action.
const preview = process.env.FDP_UI_TEST_PREVIEW === "1";
const port = preview ? 1432 : 1431;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  use: {
    baseURL: `http://localhost:${port}`,
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: preview ? `npm run build && npx vite preview --strictPort --port ${port}` : `npm run dev -- --port ${port}`,
    url: `http://localhost:${port}`,
    env: { FDP_UI_TEST: "1" },
    ignoreHTTPSErrors: true,
    reuseExistingServer: !preview && !process.env.CI,
  },
});
