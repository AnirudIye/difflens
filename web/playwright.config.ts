import { defineConfig, devices } from "@playwright/test";

// One end to end test, on the demo path only. Never the OAuth path: that
// would need real GitHub credentials in CI, and a test that signs in to a
// third party is a test that fails for reasons that are not about this code.
//
// The demo path is the one flow in this app that is deterministic by
// construction (fixed sample, recorded AI response, no network), which is
// exactly what makes it the only honest candidate for a browser test.
export default defineConfig({
  testDir: "./e2e",
  // A demo review runs the real queue, worker, and analyzers, so the
  // assertions wait on real work rather than on a stub.
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? "list" : "line",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
