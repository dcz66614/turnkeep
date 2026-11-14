// History view (T030): event timeline from recall?history=1.
// Times are ISO 8601 with the local timezone labelled.
import { api, type ApiConfig } from "../api";
import { isCurrent } from "../guard";
import type { MemoryRecord } from "../types";
import { recordCard, escapeHtml } from "../components/RecordCard";

export async function renderHistory(el: HTMLElement, cfg: ApiConfig, gen: number): Promise<void> {
  el.innerHTML = "<p>加载中…</p>";
  try {
    const recs = await api<{ records: MemoryRecord[] }>(cfg, "/api/v1/recall?query=&history=1");
    const sorted = [...recs.records].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section><h2>历史</h2>
      <p><small>时间统一为 ISO 8601（本地时区：${Intl.DateTimeFormat().resolvedOptions().timeZone}）。</small></p>
      ${sorted.length ? sorted.map((r) => `<div>
          <time datetime="${r.updated_at}">${escapeHtml(r.updated_at)}</time>
          ${recordCard(r)}</div>`).join("") : "<p>暂无历史记录。</p>"}
    </section>`;
  } catch (e) {
    const err = e as Error & { code?: string };
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section class="card error"><h2>加载失败</h2>
      <p>${err.code ?? "ERROR"}：${err.message}</p>
      <p>下一步：检查本地服务是否在运行，然后重试。</p>
      <button type="button" onclick="location.reload()">重试</button></section>`;
  }
}
