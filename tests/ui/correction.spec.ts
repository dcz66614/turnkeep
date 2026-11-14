// T032 e2e: RecordEditor CAS + Conflicts resolve against a real serve.
// A concurrent CLI update between page load and submit must surface as a
// 409 conflict, never a silent overwrite.
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

test("editor corrects with CAS; concurrent update surfaces 409, no silent overwrite", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  cli(db, ["init", "--scope", "demo"]);
  const rec = JSON.parse(cli(db, ["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.11"]));
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await connect(page, port, token);
    await page.getByRole("link", { name: "编辑" }).first().click();
    await expect(page.getByText("第 1 版 · 状态 active")).toBeVisible();
    // Someone else updates the record while the editor is open.
    cli(db, ["correct", "--scope", "demo", "--id", rec.id, "--expected-revision", "1", "--value", "3.12"]);
    await page.locator("#correct-form input[name='value']").fill("3.13");
    await page.locator("#correct-form button[type='submit']").click();
    await expect(page.locator("#editor-result")).toContainText("冲突");
    // The concurrent value survived; nothing was silently overwritten.
    await page.goto("/#/current");
    await expect(page.getByText("3.12")).toBeVisible();
  } finally {
    proc.kill();
  }
});

test("conflicts view resolves the full candidate set with new evidence", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  cli(db, ["init", "--scope", "demo"]);
  cli(db, ["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.11"]);
  cli(db, ["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.12"]);
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await connect(page, port, token);
    await page.getByRole("link", { name: "冲突处理" }).click();
    await expect(page.getByText("3.11")).toBeVisible();
    await expect(page.getByText("3.12")).toBeVisible();
    await page.locator("[data-resolve-form] input[name='value']").fill("3.12");
    await page.locator("[data-resolve-form] input[name='source_id']").fill("doc:9");
    await page.locator("[data-resolve-form] button[type='submit']").click();
    await expect(page.locator("[data-resolve-result]")).toContainText("已解决");
    await page.goto("/#/current");
    await expect(page.getByText("3.12").first()).toBeVisible();
  } finally {
    proc.kill();
  }
});
