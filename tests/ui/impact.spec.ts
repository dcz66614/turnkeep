// T031 e2e: Impact view against a real `turnkeep serve` on a temp DB
// seeded through the real CLI: requirement 3.11 -> result depends on it
// -> begin -> correct to 3.12 -> stale finish -> UI shows the change,
// the impact path, and resume reveals the diff.
import { test, expect } from "@playwright/test";
import { spawn, execFileSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createServer } from "node:net";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ROOT = join(__dirname, "..", "..");
const VENV_PY = join(ROOT, ".venv", "bin", "python");
const ENV = { ...process.env, PYTHONPATH: join(ROOT, "src") };

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const s = createServer();
    s.listen(0, "127.0.0.1", () => {
      const port = (s.address() as { port: number }).port;
      s.close(() => resolve(port));
    });
  });
}

function seed(db: string): { reqId: string; runId: string } {
  const run = (args: string[]): string =>
    execFileSync(VENV_PY, ["-m", "turnkeep", "--db", db, ...args], { env: ENV, stdio: "pipe" }).toString();
  run(["init", "--scope", "demo", "--display-name", "Demo"]);
  const req = JSON.parse(run(["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.11"]));
  run(["remember", "--scope", "demo", "--key", "release", "--value", "Python 3.11", "--parent", req.id]);
  const begun = JSON.parse(run(["begin", "--scope", "demo", "--task", "prepare release"]));
  run(["correct", "--scope", "demo", "--id", req.id, "--expected-revision", "1", "--value", "3.12"]);
  return { reqId: req.id, runId: begun.run_id };
}

async function startServe(db: string, port: number): Promise<{ proc: ChildProcessWithoutNullStreams; token: string }> {
  const proc = spawn(VENV_PY, ["-m", "turnkeep", "--db", db, "serve", "--scope", "demo", "--port", String(port)], {
    env: ENV,
  });
  const token = await new Promise<string>((resolve, reject) => {
    let buf = "";
    proc.stdout.on("data", (d: Buffer) => {
      buf += d.toString();
      try {
        resolve(JSON.parse(buf.split("\n")[0]).token as string);
      } catch {
        /* wait for the full line */
      }
    });
    setTimeout(() => reject(new Error("serve did not print its token in time")), 10_000);
  });
  return { proc, token };
}

test("impact view shows old/new, the path, and resume diff", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  const { runId } = seed(db);
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await page.goto("/");
    await page.getByLabel("本地 API 地址").fill(`http://127.0.0.1:${port}`);
    await page.getByLabel("会话令牌").fill(token);
    await page.getByRole("button", { name: "连接" }).click();
    await page.getByRole("link", { name: "影响与恢复" }).click();
    await expect(page.getByRole("heading", { name: "build.python_version" })).toBeVisible();
    await expect(page.getByText("旧值：3.11")).toBeVisible();
    await expect(page.getByText("新值：3.12")).toBeVisible();
    await expect(page.getByText("release").first()).toBeVisible();
    await page.getByRole("button", { name: "恢复（查看差异）" }).click();
    await expect(page.getByText("build.python_version", { exact: false }).first()).toBeVisible();
    await expect(page.locator(`[data-resume-result="${runId}"]`)).toContainText("build.python_version");
  } finally {
    proc.kill();
  }
});
