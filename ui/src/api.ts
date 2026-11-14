// Minimal API client for the local workbench API (T030).
// The session token is entered once in the connect form and kept in
// localStorage (same machine only). Nothing is sent anywhere else.

export interface ApiConfig {
  baseUrl: string;
  token: string;
}

const KEY = "turnkeep.api";

export function loadConfig(): ApiConfig | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as ApiConfig) : null;
  } catch {
    return null;
  }
}

export function saveConfig(cfg: ApiConfig): void {
  localStorage.setItem(KEY, JSON.stringify(cfg));
}

export async function api<T>(cfg: ApiConfig, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${cfg.baseUrl.replace(/\/$/, "")}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${cfg.token}`,
      ...(init?.headers ?? {}),
    },
  });
  const data = await res.json();
  if (res.status === 401) {
    // The session token changed (server restarted): forget it so the next
    // render shows the connect form instead of a stale error.
    try {
      localStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
  }
  if (!res.ok) {
    const err = new Error(data?.error?.message ?? `HTTP ${res.status}`) as Error & {
      code?: string;
      details?: unknown;
    };
    err.code = data?.error?.code;
    err.details = data?.error?.details;
    throw err;
  }
  return data as T;
}
