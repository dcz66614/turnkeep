// Recovery view (T033): runs, resume diffs, rebase attempts, quarantine.
// The workbench only SHOWS the diff and the new candidate — re-execution
// is done by an external client (Muse/Claude Code/Codex). While no one has
// done the rework, the run is shown as "等待执行", never as done.
import { api, type ApiConfig } from "../api";
import { isCurrent } from "../guard";
import { escapeHtml } from "../components/RecordCard";

interface RunRow {
  id: string;
  task: string;
  status: string;
  created_at: string;
}

export async function renderRecovery(el: HTMLElement, cfg: ApiConfig, gen: number): Promise<void> {
  el.innerHTML = "<p>加载中…</p>";
  try {
    const runs = await api<{ runs: RunRow[] }>(cfg, "/api/v1/runs");
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section><h2>恢复操作</h2>
      <p>重新执行由外部客户端完成；工作台只显示差异与新候选，不伪造任务已重做。
         旧的 candidate 不会自动进入 active 召回。</p>
      ${runs.runs.length ? runs.runs.map((r) => `<div class="card" data-run-id="${r.id}">
          <p><strong>${escapeHtml(r.task)}</strong> — ${escapeHtml(r.status)}
          ${r.status === "open" ? ' · <span class="badge">等待执行</span>' : ""}</p>
          <p><small>${escapeHtml(r.created_at)}</small></p>
          ${r.status === "open" ? `<button type="button" data-resume-run="${r.id}">恢复（查看差异）</button>
          <button type="button" data-rebase-run="${r.id}">重定基</button>` : ""}
          <div data-recovery-result="${r.id}"></div>
        </div>`).join("") : "<p>暂无运行记录。</p>"}
    </section>`;
    const bind = (attr: string, endpoint: string, render: (d: Record<string, unknown>) => string) => {
      el.querySelectorAll(`[${attr}]`).forEach((btn) => {
        btn.addEventListener("click", async () => {
          const runId = (btn as HTMLElement).dataset[attr === "data-resume-run" ? "resumeRun" : "rebaseRun"]!;
          const out = el.querySelector(`[data-recovery-result="${runId}"]`)!;
          try {
            const d = await api<Record<string, unknown>>(cfg, endpoint, {
              method: "POST", body: JSON.stringify({ run_id: runId }),
            });
            out.innerHTML = render(d);
          } catch (e) {
            out.innerHTML = `<p class="error">${escapeHtml((e as Error).message)}</p>`;
          }
        });
      });
    };
    bind("data-resume-run", "/api/v1/resume", (d) => {
      const keys = (d.changed_keys as string[] | undefined) ?? [];
      const changed = (d.changed as { key: string; from_state: string; to_state: string }[] | undefined) ?? [];
      const cand = d.candidate_snapshot as { run_id?: string } | undefined;
      return `<p>差异：变更的键 ${keys.map(escapeHtml).join(", ") || "无"}</p>
        <ul>${changed.map((c) => `<li>${escapeHtml(c.key)}：${escapeHtml(c.from_state)} → ${escapeHtml(c.to_state)}</li>`).join("")}</ul>
        <p>新候选 run：${escapeHtml(cand?.run_id ?? "无")}。旧结果仍然有效：${d.old_result_still_valid ? "是" : "否"}。
        下一步由外部客户端重算并提交。</p>`;
    });
    bind("data-rebase-run", "/api/v1/rebase", (d) => {
      if (d.status === "quarantined") {
        return `<p class="error">已隔离：${escapeHtml(String(d.reason ?? ""))}（已尝试 ${String(d.attempts ?? 2)} 次）。
          请开独立任务继续，不要自动重试。</p>`;
      }
      const diff = d.diff as { changed_keys?: string[] } | undefined;
      return `<p>已重定基（第 ${String(d.attempts ?? "?")} 次）：新 run ${escapeHtml(String(d.run_id ?? ""))}。
        差异键：${(diff?.changed_keys ?? []).map(escapeHtml).join(", ") || "无"}。最多 2 次自动重定基。</p>`;
    });
  } catch (e) {
    const err = e as Error & { code?: string };
    el.innerHTML = `<section class="card error"><h2>加载失败</h2>
      <p>${err.code ?? "ERROR"}：${err.message}</p>
      <p>下一步：检查本地服务是否在运行，然后重试。</p></section>`;
  }
}
