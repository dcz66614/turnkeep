// Conflicts view (T032): full candidate set + source for each conflicted
// key; resolve requires a new external evidence source. If the candidate
// set changed since the page loaded, the server rejects the whole resolve
// (CANDIDATE_SET_CHANGED) and the UI re-fetches instead of guessing.
import { api, type ApiConfig } from "../api";
import { isCurrent } from "../guard";
import type { MemoryRecord } from "../types";
import { escapeHtml } from "../components/RecordCard";

export async function renderConflicts(el: HTMLElement, cfg: ApiConfig, gen: number): Promise<void> {
  el.innerHTML = "<p>加载中…</p>";
  try {
    const data = await api<{ conflicts: MemoryRecord[] }>(cfg, "/api/v1/conflicts");
    const byKey = new Map<string, MemoryRecord[]>();
    for (const r of data.conflicts) {
      const list = byKey.get(r.key) ?? [];
      list.push(r);
      byKey.set(r.key, list);
    }
    if (!byKey.size) {
    if (!isCurrent(gen)) return;
      if (!isCurrent(gen)) return;
    el.innerHTML = `<section><h2>冲突处理</h2><p>没有未解决的冲突。</p></section>`;
      return;
    }
    el.innerHTML = `<section><h2>冲突处理</h2>
      ${[...byKey].map(([key, cands]) => `<article class="card" data-conflict-key="${escapeHtml(key)}">
        <h3>${escapeHtml(key)}</h3>
        <ul>${cands.map((c) => `<li>${escapeHtml(c.value)}（第 ${c.revision} 版，
          来源 ${escapeHtml((c.source_refs ?? []).map((s) => `${s.id}:${s.type}`).join(", "))}）</li>`).join("")}</ul>
        <form data-resolve-form="${escapeHtml(key)}">
          <label>决议值 <input name="value" required /></label>
          <label>新依据来源 id <input name="source_id" placeholder="例如 doc:9" required /></label>
          <label>来源类型 <select name="source_type">
            <option value="user_statement">用户陈述</option>
            <option value="tool_observation">工具观测</option>
            <option value="agent_inference">模型推断</option>
            <option value="imported_text">导入文本</option>
          </select></label>
          <button type="submit">解决冲突</button>
        </form>
        <div data-resolve-result="${escapeHtml(key)}"></div>
      </article>`).join("")}
      <p><small>没有可靠新依据时不强制裁决：保留冲突，继续做独立任务。</small></p>
    </section>`;
    el.querySelectorAll("[data-resolve-form]").forEach((form) => {
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const key = (form as HTMLElement).dataset.resolveForm!;
        const fd = new FormData(form as HTMLFormElement);
        const out = el.querySelector(`[data-resolve-result="${key}"]`)!;
        try {
          // Re-fetch the full current candidate set at submit time.
          const fresh = await api<{ conflicts: MemoryRecord[] }>(cfg, `/api/v1/conflicts?key=${encodeURIComponent(key)}`);
          const candidates = fresh.conflicts.map((c) => ({ id: c.id, revision: c.revision }));
          const res = await api<MemoryRecord>(cfg, "/api/v1/resolve_conflict", {
            method: "POST",
            body: JSON.stringify({
              key, value: String(fd.get("value")), candidates,
              sources: [{ id: String(fd.get("source_id")), type: String(fd.get("source_type")) }],
            }),
          });
          out.innerHTML = `<p>已解决：${escapeHtml(res.key)} = ${escapeHtml(res.value)}（第 ${res.revision} 版）。</p>`;
        } catch (e) {
          const err = e as Error & { code?: string };
          if (err.code === "CANDIDATE_SET_CHANGED") {
            out.innerHTML = `<p class="error">候选集合已变化，整笔解决被拒绝。已刷新候选列表，请重新确认后再提交。</p>`;
            await renderConflicts(el, cfg, gen);
          } else {
            out.innerHTML = `<p class="error">${escapeHtml(err.code ?? "ERROR")}：${escapeHtml(err.message)}</p>`;
          }
        }
      });
    });
  } catch (e) {
    const err = e as Error & { code?: string };
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section class="card error"><h2>加载失败</h2>
      <p>${err.code ?? "ERROR"}：${err.message}</p>
      <p>下一步：检查本地服务是否在运行，然后重试。</p></section>`;
  }
}
