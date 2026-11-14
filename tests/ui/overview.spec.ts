// T030 e2e: Overview + History against a real `turnkeep serve` on a
// temp DB seeded through the real CLI. No mocks of the API.
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

function seed(db: string): void {
  const run = (args: string[]) =>
    execFileSync(VENV_PY, ["-m", "turnkeep", "--db", db, ...args], { env: ENV, stdio: "pipe" });
  run(["init", "--scope", "demo", "--display-name", "Demo"]);
  run(["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.12"]);
}

async function startServe(db: string, port: number): Promise<{ proc: ChildProcessWithoutNullStreams; token: string }> {
  const proc = spawn(VENV_PY, ["-m", "turnkeep", "--db", db, "serve", "--scope", "demo", "--port", String(port)], {
    env: ENV,
  });
  const token = await new Promise<string>((resolve, reject) => {
    let buf = "";
    proc.stdout.on("data", (d: Buffer) => {
      buf += d.toString();
      const line = buf.split("\n")[0];
      try {
        resolve(JSON.parse(line).token as string);
      } catch {
        /* wait for the full line */
      }
    });
    proc.stderr.on("data", () => undefined);
    setTimeout(() => reject(new Error("serve did not print its token in time")), 10_000);
  });
  return { proc, token };
}

test("overview shows seeded decision and history lists it", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  seed(db);
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await page.goto("/");
    await page.getByLabel("本地 API 地址").fill(`http://127.0.0.1:${port}`);
    await page.getByLabel("会话令牌").fill(token);
    await page.getByRole("button", { name: "连接" }).click();
    await expect(page.getByText("build.python_version")).toBeVisible();
    await expect(page.getByText("3.12")).toBeVisible();
    await expect(page.getByText("当前可用")).toBeVisible();
    await page.getByRole("link", { name: "历史" }).click();
    await expect(page.getByText("build.python_version").first()).toBeVisible();
  } finally {
    proc.kill();
  }
});

test("wrong token shows an actionable error, not fake data", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  seed(db);
  const port = await freePort();
  const { proc } = await startServe(db, port);
  try {
    await page.goto("/");
    await page.getByLabel("本地 API 地址").fill(`http://127.0.0.1:${port}`);
    await page.getByLabel("会话令牌").fill("wrong-token");
    await page.getByRole("button", { name: "连接" }).click();
    await expect(page.getByText("加载失败")).toBeVisible();
    await expect(page.getByText("检查本地服务")).toBeVisible();
    await expect(page.getByText("build.python_version")).toHaveCount(0);
  } finally {
    proc.kill();
  }
});
