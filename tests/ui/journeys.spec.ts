// T035 journeys on the production build (or dev server under the default
// config): J01 screen-vs-DB consistency and J04 restart recovery.
// Every step checks DB (CLI) / API / screen together.
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

function cli(db: string, args: string[]): string {
  return execFileSync(VENV_PY, ["-m", "turnkeep", "--db", db, ...args], { env: ENV, stdio: "pipe" }).toString();
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

async function connect(page: import("@playwright/test").Page, port: number, token: string): Promise<void> {
  await page.goto("/");
  await page.getByLabel("本地 API 地址").fill(`http://127.0.0.1:${port}`);
  await page.getByLabel("会话令牌").fill(token);
  await page.getByRole("button", { name: "连接" }).click();
}

test("J01: screen matches DB after a correction", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  cli(db, ["init", "--scope", "demo"]);
  const rec = JSON.parse(cli(db, ["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.11"]));
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await connect(page, port, token);
    await expect(page.getByText("3.11")).toBeVisible();
    // DB change via the real CLI; API + screen must agree afterwards.
    cli(db, ["correct", "--scope", "demo", "--id", rec.id, "--expected-revision", "1", "--value", "3.12"]);
    await page.reload();
    await expect(page.getByText("3.12")).toBeVisible();
    await expect(page.getByText("3.11")).toHaveCount(0);
    await page.getByRole("link", { name: "历史" }).click();
    await expect(page.getByText("3.12").first()).toBeVisible();
    await expect(page.getByText("3.11").first()).toBeVisible();
  } finally {
    proc.kill();
  }
});

test("J04: restart recovers runs and records (DB, not localStorage)", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  cli(db, ["init", "--scope", "demo"]);
  cli(db, ["remember", "--scope", "demo", "--key", "export.format", "--value", "zip", "--kind", "decision"]);
  const run = JSON.parse(cli(db, ["begin", "--scope", "demo", "--task", "produce export manifest"]));
  const port = await freePort();
  let srv = await startServe(db, port);
  try {
    await connect(page, port, srv.token);
    await expect(page.getByText("export.format")).toBeVisible();
    // Stop the service completely, then start it again on the same port.
    srv.proc.kill();
    await srv.proc.wait?.() ?? undefined;
    await new Promise((r) => setTimeout(r, 500));
    srv = await startServe(db, port);
    await page.reload();
    // The token changed on restart: the app shows the error card, clears
    // the stale token, and a retry brings back the connect form.
    await expect(page.getByText("加载失败")).toBeVisible();
    await page.getByRole("button", { name: "重试" }).click();
    await page.getByLabel("本地 API 地址").fill(`http://127.0.0.1:${port}`);
    await page.getByLabel("会话令牌").fill(srv.token);
    await page.getByRole("button", { name: "连接" }).click();
    await expect(page.getByText("export.format")).toBeVisible();
    await page.getByRole("link", { name: "恢复", exact: true }).click();
    await expect(page.locator(`[data-run-id="${run.run_id}"]`)).toContainText("produce export manifest");
    await expect(page.getByText("等待执行")).toBeVisible();
  } finally {
    srv.proc.kill();
  }
});
