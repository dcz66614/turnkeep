// Entry point (T030). Hash routing: #/ overview, #/current, #/history,
// #/impact (impact view lands in T031; until then it shows a placeholder
// that says so — never fake content).
import { loadConfig, saveConfig } from "./api";
import { renderOverview } from "./views/Overview";
import { renderHistory } from "./views/History";
import { renderImpact } from "./views/Impact";
import { renderConflicts } from "./views/Conflicts";
import { renderRecordEditor } from "./views/RecordEditor";
import { renderHandoff } from "./views/Handoff";
import { renderRecovery } from "./views/Recovery";
import { nextGen } from "./guard";

const app = document.getElementById("app")!;

function connectForm(): string {
  return `<section><h2>连接本地工作台</h2>
    <p>输入 <code>turnkeep serve</code> 启动时打印的地址与会话令牌（仅本机使用）。</p>
    <form id="connect-form">
      <label>本地 API 地址 <input name="baseUrl" value="http://127.0.0.1:5390" required /></label>
      <label>会话令牌 <input name="token" type="password" required /></label>
      <button type="submit">连接</button>
    </form></section>`;
}

async function render(): Promise<void> {
  const cfg = loadConfig();
  if (!cfg) {
    app.innerHTML = connectForm();
    document.getElementById("connect-form")!.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target as HTMLFormElement);
      saveConfig({ baseUrl: String(fd.get("baseUrl")), token: String(fd.get("token")) });
      void render();
    });
    return;
  }
  const gen = nextGen();
  const route = location.hash.replace(/^#\/?/, "") || "overview";
  const [path, query] = route.split("?");
  const params = new URLSearchParams(query ?? "");
  if (path === "edit" && params.get("id")) {
    await renderRecordEditor(app, cfg, params.get("id")!, gen);
    return;
  }
  if (path === "handoff") {
    await renderHandoff(app, cfg, gen);
    return;
  }
  if (path === "recovery") {
    await renderRecovery(app, cfg, gen);
    return;
  }
  if (path === "conflicts") {
    await renderConflicts(app, cfg, gen);
    return;
  }
  if (path === "history") {
    await renderHistory(app, cfg, gen);
  } else if (path === "impact") {
    await renderImpact(app, cfg, gen);
  } else if (path === "current") {
    await renderOverview(app, cfg, gen);
  } else {
    await renderOverview(app, cfg, gen);
  }
}

window.addEventListener("hashchange", () => void render());
void render();
