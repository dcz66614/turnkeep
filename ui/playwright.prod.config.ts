import { defineConfig } from "@playwright/test";

// Production-build config (T035): serves ui/dist via `vite preview`,
// never the dev server. Same tests, same testDir.
export default defineConfig({
  testDir: "../tests/ui",
  timeout: 30_000,
  retries: 0,
  use: { baseURL: "http://127.0.0.1:5173", trace: "retain-on-failure" },
  webServer: {
    command: "npm run preview -- --port 5173 --strictPort",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
