// Record editor (T032): correct / retract with CAS. The form binds the
// revision fetched when the page opened; the submit carries that
// expected_revision. If someone else updated the record meanwhile, the
// server returns 409 and the UI refreshes the diff instead of overwriting.
import { api, type ApiConfig } from "../api";
import { isCurrent } from "../guard";
import type { MemoryRecord } from "../types";
import { escapeHtml } from "../components/RecordCard";

export async function renderRecordEditor(el: HTMLElement, cfg: ApiConfig, recordId: string, gen: number): Promise<void> {
  el.innerHTML = "<p>加载中…</p>";
  const load = async (): Promise<MemoryRecord | null> => {
    const recs = await api<{ records: MemoryRecord[] }>(cfg, "/api/v1/recall?query=&history=1");
    return recs.records.find((r) => r.id === recordId) ?? null;
  };
  try {
    const rec = await load();
    if (!rec) {
    if (!isCurrent(gen)) return;
      el.innerHTML = `<section><h2>未找到记录</h2><p>记录 ${escapeHtml(recordId)} 不存在或已被擦除。</p></section>`;
      return;
    }
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section><h2>编辑记录</h2>
      <p><strong>${escapeHtml(rec.key)}</strong> — 当前值：${escapeHtml(rec.value)}</p>
      <p>第 ${rec.revision} 版 · 状态 ${escapeHtml(rec.state)} ·
         来源 ${escapeHtml((rec.source_refs ?? []).map((s) => `${s.id}:${s.type}`).join(", "))}</p>
      <form id="correct-form">
        <label>新值 <input name="value" value="${escapeHtml(rec.value)}" required /></label>
        <button type="submit">纠错（携带第 ${rec.revision} 版期望）</button>
      </form>
      <form id="retract-form">
        <label>撤回原因 <input name="reason" placeholder="为什么撤回" required /></label>
        <button type="submit">撤回此记录</button>
      </form>
      <p><small>撤回保留历史，不删除记录。彻底擦除在 T038 单独入口。</small></p>
      <div id="editor-result" aria-live="polite"></div>
      <p><a href="#/impact">查看影响与恢复</a></p>
    </section>`;
    const result = el.querySelector<HTMLElement>("#editor-result")!;
    const showConflict = async (msg: string) => {
      const fresh = await load();
      result.innerHTML = `<p class="error">冲突：${escapeHtml(msg)}。页面已刷新为最新值（第 ${fresh?.revision} 版），
        你的输入未被覆盖，请确认后再提交。</p>`;
    };
    el.querySelector("#correct-form")!.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target as HTMLFormElement);
      try {
        const out = await api<MemoryRecord>(cfg, "/api/v1/correct", {
          method: "POST",
          body: JSON.stringify({
            record_id: rec.id, expected_revision: rec.revision,
            new_value: String(fd.get("value")),
          }),
        });
        result.innerHTML = `<p>已纠错：新值 ${escapeHtml(out.value)}（第 ${out.revision} 版）。
          <a href="#/impact">查看受影响的结果</a></p>`;
      } catch (e) {
        const err = e as Error & { code?: string };
        if (err.code === "VERSION_CONFLICT") await showConflict(err.message);
        else result.innerHTML = `<p class="error">${escapeHtml(err.code ?? "ERROR")}：${escapeHtml(err.message)}</p>`;
      }
    });
    el.querySelector("#retract-form")!.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target as HTMLFormElement);
      try {
        const out = await api<MemoryRecord>(cfg, "/api/v1/retract", {
          method: "POST",
          body: JSON.stringify({
            record_id: rec.id, expected_revision: rec.revision,
            reason: String(fd.get("reason")),
          }),
        });
        result.innerHTML = `<p>已撤回（第 ${out.revision} 版）。历史保留，可在“历史”中查看。</p>`;
      } catch (e) {
        const err = e as Error & { code?: string };
        if (err.code === "VERSION_CONFLICT") await showConflict(err.message);
        else result.innerHTML = `<p class="error">${escapeHtml(err.code ?? "ERROR")}：${escapeHtml(err.message)}</p>`;
      }
    });
  } catch (e) {
    const err = e as Error & { code?: string };
    if (!isCurrent(gen)) return;
    el.innerHTML = `<section class="card error"><h2>加载失败</h2>
      <p>${err.code ?? "ERROR"}：${err.message}</p>
      <p>下一步：检查本地服务是否在运行，然后重试。</p></section>`;
  }
}
