// Overview view (T030): projects, current decisions, rework queue,
// recent changes. Empty state says so; errors give a next step.
import { api, type ApiConfig } from "../api";
import { isCurrent } from "../guard";
import type { MemoryRecord } from "../types";
import { recordCard } from "../components/RecordCard";

export async function renderOverview(el: HTMLElement, cfg: ApiConfig, gen: number): Promise<void> {
  el.innerHTML = "<p>加载中…</p>";
  try {
    const scopes = await api<{ scopes: { id: string; display_name: string; epoch: number }[] }>(
      cfg, "/api/v1/scopes",
    );
    const scope = scopes.scopes[0]?.id ?? "";
    if (!scope) {
      el.innerHTML = `<section><h2>项目总览</h2><p>暂无记录。</p>
        <p>先在终端记下第一条决定：<code>turnkeep init --scope myproject</code></p></section>`;
      return;
    }
    const recs = await api<{ records: MemoryRecord[] }>(cfg, `/api/v1/recall?query=`);
    const active = recs.records.filter((r) => r.state === "active");
    // recall without history returns active records only, so conflicted
    // records never appear here — count them via /api/v1/conflicts.
    const conf = await api<{ conflicts: MemoryRecord[] }>(cfg, "/api/v1/conflicts");
    const conflicted = conf.conflicts;
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section>
      <h2>项目总览</h2>
      <p>项目：<strong>${scope}</strong>（共 ${scopes.scopes.length} 个项目）</p>
      <h3>可依赖的决定（${active.length}）</h3>
      ${active.length ? active.map(recordCard).join("") : "<p>暂无记录。</p>"}
      <h3>需要重做的工作（${conflicted.length}）</h3>
      ${conflicted.length ? conflicted.map(recordCard).join("") : "<p>没有冲突需要处理。</p>"}
      <h3>最近变更</h3>
      <p>最近一次记录更新：${recs.records[0]?.updated_at ?? "无"}</p>
    </section>`;
  } catch (e) {
    const err = e as Error & { code?: string };
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section class="card error"><h2>加载失败</h2>
      <p>${err.code ?? "ERROR"}：${err.message}</p>
      <p>下一步：检查本地服务 <code>turnkeep serve</code> 是否在运行，确认令牌正确，然后重试。</p>
      <button type="button" onclick="location.reload()">重试</button></section>`;
  }
}
