// T035 accessibility: keyboard-only flow, narrow viewport, long text.
// Automated checks only — they do not replace human usability validation
// (UX_SPEC says so explicitly).
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

test("keyboard-only: connect form and nav work without a mouse", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  cli(db, ["init", "--scope", "demo"]);
  cli(db, ["remember", "--scope", "demo", "--key", "build.python_version", "--value", "3.12"]);
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await page.goto("/");
    // Tab through the nav links to the API address field, type, Tab to
    // token, type, Tab to submit, Enter. (Nav comes first in tab order.)
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      if (await page.getByLabel("本地 API 地址").evaluate((el) => el === document.activeElement)) break;
    }
    await expect(page.getByLabel("本地 API 地址")).toBeFocused();
    await page.keyboard.type(`http://127.0.0.1:${port}`);
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("会话令牌")).toBeFocused();
    await page.keyboard.type(token);
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "连接" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByText("build.python_version")).toBeVisible();
    // Keyboard nav: Tab to the History link and activate it.
    await page.getByRole("link", { name: "历史" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: "历史" })).toBeVisible();
  } finally {
    proc.kill();
  }
});

test("narrow viewport: no horizontal overflow, buttons stay reachable", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "tk-ui-"));
  const db = join(dir, "ui.sqlite3");
  cli(db, ["init", "--scope", "demo"]);
  const longValue = "这是一个非常长的中文决定值，用来检查窄屏下文字是否会溢出覆盖按钮或者导致横向滚动条出现。" + "重复。".repeat(20);
  const longSource = "https://example.invalid/" + "very-long-source-path/".repeat(10);
  cli(db, ["remember", "--scope", "demo", "--key", "long.text", "--value", longValue]);
  const port = await freePort();
  const { proc, token } = await startServe(db, port);
  try {
    await page.setViewportSize({ width: 375, height: 667 });
    await page.goto("/");
    await page.getByLabel("本地 API 地址").fill(`http://127.0.0.1:${port}`);
    await page.getByLabel("会话令牌").fill(token);
    await page.getByRole("button", { name: "连接" }).click();
    await expect(page.getByText("long.text")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBe(0);
    const connectBox = await page.getByRole("link", { name: "项目总览" }).boundingBox();
    expect(connectBox).not.toBeNull();
  } finally {
    proc.kill();
  }
});

test("every input has a label and every button has an accessible name", async ({ page }) => {
  await page.goto("/");
  const unlabeled = await page.evaluate(() => {
    const bad: string[] = [];
    for (const input of document.querySelectorAll("input, select, textarea")) {
      const el = input as HTMLInputElement;
      const labelled = el.labels?.length || el.getAttribute("aria-label") || el.getAttribute("aria-labelledby");
      if (!labelled) bad.push(el.outerHTML.slice(0, 80));
    }
    for (const btn of document.querySelectorAll("button")) {
      if (!btn.textContent?.trim() && !btn.getAttribute("aria-label")) bad.push(btn.outerHTML.slice(0, 80));
    }
    return bad;
  });
  expect(unlabeled).toEqual([]);
});
