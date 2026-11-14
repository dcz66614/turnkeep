import { defineConfig } from "vite";

export default defineConfig({
  // Relative asset paths so the production build works from any same-origin
  // static deployment (T035); no external CDN is referenced.
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:5390",
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
