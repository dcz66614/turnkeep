// Typed view models for the Turnkeep workbench (T029).
// Mirrors the local app API (specs/local-app-api.json) and the core
// record/event shapes. English field names are stable; UI copy is Chinese.

export type RecordState = "active" | "superseded" | "retracted" | "conflicted";
export type ViewState =
  | "empty"
  | "loading"
  | "ready"
  | "error"
  | "conflict"
  | "stale"
  | "recovery"
  | "scripted";

export interface SourceRef {
  id: string;
  type: string;
}

export interface MemoryRecord {
  id: string;
  key: string;
  value: string;
  kind: string;
  state: RecordState;
  revision: number;
  /** API field name (the core returns source_refs, not sources). */
  source_refs: SourceRef[];
  parent_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface ChangeDiff {
  changed_keys: string[];
  added: string[];
  removed: string[];
}

export interface StaleRejection {
  code: "STALE_SNAPSHOT";
  changed_keys: string[];
  changed_ids: string[];
  next_step: "resume";
}

export interface ImpactPath {
  record_id: string;
  depends_on: string[];
  tainted: boolean;
  reason: string;
}

export interface ConflictCandidate {
  record_id: string;
  revision: number;
  value: string;
  state: RecordState;
}

export interface HandoffBrief {
  goal: string;
  decisions: MemoryRecord[];
  next_steps: string[];
  evidence_record_ids: string[];
  unknown_fields: string[];
}

export interface ViewModel<T> {
  state: ViewState;
  data: T | null;
  error?: { code: string; message: string; next_step: string };
  /** True only for labelled scripted simulations. */
  scripted?: boolean;
}

export interface OverviewModel {
  projects: { scope_id: string; display_name: string; epoch: number }[];
  current_scope: string;
  decisions: MemoryRecord[];
  rework: { run_id: string; reason: string }[];
  recent_changes: { record_id: string; summary: string; at: string }[];
}
