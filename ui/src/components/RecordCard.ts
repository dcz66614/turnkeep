// Shared components (T030). Accessible HTML, no framework.
// State is always shown as text as well as colour.
import type { MemoryRecord } from "../types";

export function statusBadge(state: string): string {
  const label =
    state === "active" ? "当前可用" :
    state === "conflicted" ? "冲突" :
    state === "superseded" ? "已被取代" :
    state === "retracted" ? "已撤回" : state;
  return `<span class="badge ${state}" role="status">${label}</span>`;
}

export function recordCard(r: MemoryRecord): string {
  const sources = (r.source_refs ?? []).map((s) => `${s.id}:${s.type}`).join(", ") || "无来源";
  return `<article class="card" data-record-id="${r.id}">
    <h3>${escapeHtml(r.key)}</h3>
    <p>${escapeHtml(r.value)}</p>
    <p>${statusBadge(r.state)} <span>第 ${r.revision} 版</span></p>
    <p><small>来源：${escapeHtml(sources)}</small></p>
    <p><a href="#/edit?id=${r.id}">编辑</a></p>
  </article>`;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);
}
