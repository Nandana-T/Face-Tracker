import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const isWin = process.platform === "win32";
const root = path.resolve(__dirname, "..", "..");
const backend = path.join(root, "backend");
export const python =
  process.env.KATOMARAN_PYTHON ??
  path.join(root, ".venv", isWin ? "Scripts/python.exe" : "bin/python");

/** Test-only credentials for the throw-away E2E stack (not secrets: they protect nothing real). */
export const E2E_PASSWORD = "e2e-admin-password-123";
const E2E_AUTH_SECRET = "e2e-auth-secret-0123456789-0123456789-abcdef";

export interface E2eEnvironment {
  DATABASE_URL: string;
  CONFIG_FILE: string;
  ADMIN_PASSWORD: string;
  AUTH_SECRET: string;
}

/**
 * Prepares everything the real-stack E2E tests need, idempotently:
 *  - an embedded local PostgreSQL (so tests never touch the hosted database),
 *  - migrations,
 *  - small deterministic sample videos (a public-domain portrait, panned/zoomed) + a broken file.
 * The result is cached in process.env so Playwright's worker processes do not repeat the work.
 */
export function prepareE2e(): E2eEnvironment {
  if (process.env.KATOMARAN_E2E_DATABASE_URL) {
    return {
      DATABASE_URL: process.env.KATOMARAN_E2E_DATABASE_URL,
      CONFIG_FILE: "config/config.e2e.json",
      ADMIN_PASSWORD: E2E_PASSWORD,
      AUTH_SECRET: E2E_AUTH_SECRET,
    };
  }
  const run = (args: string[], cwd: string, env: NodeJS.ProcessEnv = {}) =>
    execFileSync(python, args, { cwd, env: { ...process.env, ...env }, encoding: "utf8" }).trim();

  const databaseUrl = run([path.join(root, "scripts", "local_pg.py"), "start"], root);
  run(["-m", "alembic", "upgrade", "head"], backend, { DATABASE_URL: databaseUrl });

  const dir = path.join(root, "datasets", "e2e");
  fs.mkdirSync(dir, { recursive: true });
  const video = (file: string, seconds: string, extra: string[] = []) => {
    const out = path.join(dir, file);
    if (!fs.existsSync(out)) {
      run(["-m", "app.vision.sample_video", "--out", out, "--seconds", seconds, "--fps", "25", ...extra], backend);
    }
  };
  video("alignment.mp4", "3");
  video("long.mp4", "40");
  fs.writeFileSync(path.join(dir, "broken.mp4"), Buffer.from("this is not a video file".repeat(100)));

  process.env.KATOMARAN_E2E_DATABASE_URL = databaseUrl;
  return {
    DATABASE_URL: databaseUrl,
    CONFIG_FILE: "config/config.e2e.json",
    ADMIN_PASSWORD: E2E_PASSWORD,
    AUTH_SECRET: E2E_AUTH_SECRET,
  };
}
