import { defineConfig } from "playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  use: {
    baseURL: "http://localhost:1431",
    ignoreHTTPSErrors: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --port 1431",
    url: "http://localhost:1431",
    env: { FDP_UI_TEST: "1" },
    ignoreHTTPSErrors: true,
    reuseExistingServer: !process.env.CI,
  },
});
