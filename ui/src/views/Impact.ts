// Impact & Recovery view (T031, J03): what changed, why it matters,
// what to redo. Old value / new value / source / affected results / reason
// all come from the API; nothing is guessed.
import { api, type ApiConfig } from "../api";
import { isCurrent } from "../guard";
import type { MemoryRecord } from "../types";
import { recordCard, escapeHtml } from "../components/RecordCard";
import { impactPath, type ImpactStep } from "../components/ImpactPath";

interface RunRow {
  id: string;
  task: string;
  status: string;
  created_at: string;
}

export async function renderImpact(el: HTMLElement, cfg: ApiConfig, gen: number): Promise<void> {
  el.innerHTML = "<p>加载中…</p>";
  try {
    const runs = await api<{ runs: RunRow[] }>(cfg, "/api/v1/runs");
    const recs = await api<{ records: MemoryRecord[] }>(cfg, "/api/v1/recall?query=&history=1");
    const byKey = new Map<string, MemoryRecord[]>();
    for (const r of recs.records) {
      const list = byKey.get(r.key) ?? [];
      list.push(r);
      byKey.set(r.key, list);
    }
    const changed: { key: string; old: MemoryRecord; next: MemoryRecord }[] = [];
    for (const [key, list] of byKey) {
      const old = list.find((r) => r.state === "superseded");
      const next = list.find((r) => r.state === "active");
      if (old && next && old.value !== next.value) changed.push({ key, old, next });
    }
    let impactHtml = "<p>没有检测到需求变更。</p>";
    if (changed.length) {
      const parts: string[] = [];
      for (const c of changed) {
        // Children depend on the OLD (superseded) record id — that is the
        // parent_id stored in dependencies. Querying the new id finds nothing.
        const byId = new Map(recs.records.map((r) => [r.id, r]));
        const imp = await api<{ paths: ImpactStep[] }>(cfg, `/api/v1/impact?record_id=${c.old.id}`);
        parts.push(`<section class="card" data-change-key="${escapeHtml(c.key)}">
          <h3>${escapeHtml(c.key)}</h3>
          <p>旧值：${escapeHtml(c.old.value)}（第 ${c.old.revision} 版，已被取代）</p>
          <p>新值：${escapeHtml(c.next.value)}（第 ${c.next.revision} 版，当前可用）</p>
          <p>来源：${escapeHtml((c.next.source_refs ?? []).map((s) => `${s.id}:${s.type}`).join(", ") || "无来源")}</p>
          <h4>受影响的结果</h4>
          ${imp.paths.length ? `<ul>${imp.paths.map((p) => impactPath(p, byId)).join("")}</ul>` : "<p>没有下游结果依赖它。</p>"}
          <p>原因：父项变更后，AND 语义要求所有依赖它的结果重新验证；旧的 finish 已被拒绝（STALE_SNAPSHOT）。</p>
        </section>`);
      }
      impactHtml = parts.join("");
    }
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section><h2>影响与恢复</h2>
      ${impactHtml}
      <h3>运行记录</h3>
      ${runs.runs.length ? runs.runs.map((r) => `<div class="card" data-run-id="${r.id}">
          <p><strong>${escapeHtml(r.task)}</strong> — ${escapeHtml(r.status)}</p>
          <p><small>${escapeHtml(r.created_at)}</small></p>
          ${r.status === "open" ? `<button type="button" data-resume-run="${r.id}">恢复（查看差异）</button>
          <button type="button" data-rebase-run="${r.id}">重定基</button>` : ""}
          <div data-resume-result="${r.id}"></div>
        </div>`).join("") : "<p>暂无运行记录。</p>"}
    </section>`;
    el.querySelectorAll("[data-resume-run]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const runId = (btn as HTMLElement).dataset.resumeRun!;
        const out = el.querySelector(`[data-resume-result="${runId}"]`)!;
        try {
          const diff = await api<{ changed_keys: string[]; changed: { key: string; from_state: string; to_state: string }[] }>(
            cfg, "/api/v1/resume", { method: "POST", body: JSON.stringify({ run_id: runId }) },
          );
          out.innerHTML = `<p>差异：变更的键 ${diff.changed_keys.map(escapeHtml).join(", ")}</p>
            <ul>${diff.changed.map((c) => `<li>${escapeHtml(c.key)}：${escapeHtml(c.from_state)} → ${escapeHtml(c.to_state)}</li>`).join("")}</ul>
            <p>下一步：用新值重算，再提交新的 finish。</p>`;
        } catch (e) {
          out.innerHTML = `<p class="error">恢复失败：${escapeHtml((e as Error).message)}</p>`;
        }
      });
    });
    el.querySelectorAll("[data-rebase-run]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const runId = (btn as HTMLElement).dataset.rebaseRun!;
        const out = el.querySelector(`[data-resume-result="${runId}"]`)!;
        try {
          const res = await api<{ status?: string; message?: string }>(
            cfg, "/api/v1/rebase", { method: "POST", body: JSON.stringify({ run_id: runId }) },
          );
          out.innerHTML = `<p>重定基结果：${escapeHtml(JSON.stringify(res))}</p>`;
        } catch (e) {
          out.innerHTML = `<p class="error">重定基失败：${escapeHtml((e as Error).message)}</p>`;
        }
      });
    });
  } catch (e) {
    const err = e as Error & { code?: string };
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section class="card error"><h2>加载失败</h2>
      <p>${err.code ?? "ERROR"}：${err.message}</p>
      <p>下一步：检查本地服务是否在运行，然后重试。</p>
      <button type="button" onclick="location.reload()">重试</button></section>`;
  }
}
