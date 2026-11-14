// Impact path component (T031): renders one dependency path as
// key → key → key with a state badge per step. No invented relationships:
// every step comes from GET /api/v1/impact; keys/states resolve through
// the id->record map built from recall (the API returns them).
import { statusBadge, escapeHtml } from "./RecordCard";
import type { MemoryRecord } from "../types";

export interface ImpactStep {
  record_id: string;
  key: string;
  state: string;
  dependency_path: string[];
}

export function impactPath(path: ImpactStep, byId: Map<string, MemoryRecord>): string {
  const steps = path.dependency_path
    .map((id) => {
      const rec = byId.get(id);
      const key = rec?.key ?? (id === path.record_id ? path.key : id.slice(0, 8));
      const state = rec?.state ?? path.state;
      return `<span>${escapeHtml(key)} ${statusBadge(state)}</span>`;
    })
    .join(" → ");
  return `<li class="card" data-impact-record="${path.record_id}">${steps}</li>`;
}
