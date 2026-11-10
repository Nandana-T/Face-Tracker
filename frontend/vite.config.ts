import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Env files live at the repository root (shared with the backend).
  const rootDir = path.resolve(import.meta.dirname, "..");
  const env = loadEnv(mode, rootDir, "VITE_");
  const proxyTarget = env.VITE_DEV_PROXY_TARGET ?? "http://127.0.0.1:8000";

  return {
    plugins: [react(), tailwindcss()],
    envDir: rootDir,
    resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
    server: {
      port: 5173,
      strictPort: true,
      // Dev only: the browser talks to /api on the Vite origin, so no CORS is involved.
      proxy: { "/api": { target: proxyTarget, changeOrigin: true } },
    },
    build: { sourcemap: false, chunkSizeWarningLimit: 600 },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
    },
  };
});
