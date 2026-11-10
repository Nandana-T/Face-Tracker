import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { prepareE2e, python } from "./prepare";

const root = path.resolve(__dirname, "..", "..");
export const PID_FILE = path.join(root, "temp", "e2e-stack.pid");
export const VITE_PID_FILE = path.join(root, "temp", "e2e-vite.pid");
const WEB = "http://127.0.0.1:5173";
const API = "http://127.0.0.1:8000/api/v1";

async function until(what: string, check: () => Promise<boolean>, timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`Timed out waiting for ${what}`);
}

/**
 * Starts the real stack for the @live tests: API + video worker (two processes under one
 * supervisor), an embedded local PostgreSQL, the real YOLO model and generated sample videos.
 * Output goes to a log file, not a pipe, so the runner never blocks on it at shutdown.
 */
export default async function globalSetup() {
  const alive = await fetch(`${API}/health`).then((r) => r.ok, () => false);
  if (alive) throw new Error("Port 8000 is already serving an API; stop it before running the E2E suite.");

  const env = prepareE2e();
  fs.mkdirSync(path.join(root, "temp"), { recursive: true });
  fs.mkdirSync(path.join(root, "logs"), { recursive: true });
  const log = fs.openSync(path.join(root, "logs", "e2e-stack.log"), "w");
  const child = spawn(python, ["scripts/dev_stack.py", "--exit-with-parent"], {
    cwd: root,
    env: { ...process.env, ...env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", log, log],
    windowsHide: true,
  });
  child.unref();
  fs.writeFileSync(PID_FILE, String(child.pid));

  // The dev server (serves the app and proxies /api, including the WebSocket, to the API).
  const webAlive = await fetch(WEB).then((r) => r.ok, () => false);
  if (webAlive) throw new Error("Port 5173 is already serving; stop the dev server before running E2E.");
  const vite = spawn(
    process.execPath,
    [path.join(root, "frontend", "node_modules", "vite", "bin", "vite.js"), "--host", "127.0.0.1", "--port", "5173", "--strictPort"],
    {
      cwd: path.join(root, "frontend"),
      // Vite exits when stdin is closed unless it believes it runs in CI.
      env: { ...process.env, CI: "true" },
      stdio: ["ignore", fs.openSync(path.join(root, "logs", "e2e-vite.log"), "w"), fs.openSync(path.join(root, "logs", "e2e-vite.log"), "a")],
      windowsHide: true,
    },
  );
  vite.unref();
  fs.writeFileSync(VITE_PID_FILE, String(vite.pid));
  await until("the dev server", async () => (await fetch(WEB)).ok, 60_000);

  await until("the API", async () => (await fetch(`${API}/health`)).ok, 90_000);
  await until(
    "the video worker to connect",
    async () => ((await (await fetch(`${API}/sources`)).json()) as { worker: { connected: boolean } }).worker.connected,
    60_000,
  );
}

export function stopStack() {
  for (const file of [PID_FILE, VITE_PID_FILE]) {
    if (!fs.existsSync(file)) continue;
    const pid = fs.readFileSync(file, "utf8").trim();
    try {
      execFileSync(python, [path.join(root, "scripts", "stop_stack.py"), pid], { stdio: "ignore" });
    } finally {
      fs.rmSync(file, { force: true });
    }
  }
}
