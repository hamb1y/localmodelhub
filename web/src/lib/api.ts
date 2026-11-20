export type Profile = {
  id: string; device: string; state: string; port?: number | null; message?: string | null;
  host?: string; controllable?: boolean; start_on_boot?: boolean;
};
export type Model = { id: string; name: string; profiles: Profile[]; default_profile?: string | null; description?: string | null };
export type Job = {
  id: string; action: string; model: string; profile: string; state: string;
  message?: string | null; created_at: string | number; updated_at: string | number; source?: string | null;
};
export type Hardware = {
  cpu: string; ram_total_bytes: number | null; ram_available_bytes: number | null;
  gpus: { id: string; name: string; total_bytes: number | null; free_bytes: number | null }[];
};
export type MemorySample = { timestamp: number; ram_used_bytes: number | null; vram_used_bytes: number | null };
export type JobRollup = { model: string; finished: number; failed: number; start_seconds: number | null };
export type TokenSample = {
  timestamp: number; prompt_tokens: number | null; generation_tokens: number | null;
  prompt_tokens_per_second?: number | null; generation_tokens_per_second?: number | null;
};
export type InferenceProfile = Target & {
  timestamp: number; tokens: number | null; prompt_tokens: number | null;
  generation_tokens: number | null; samples: TokenSample[];
};
export type Stats = {
  samples?: MemorySample[];
  jobs_by_model?: JobRollup[];
  metrics?: Record<string, unknown>;
  profiles?: InferenceProfile[];
} & Record<string, unknown>;
export type Status = {
  models: Model[]; jobs: Job[]; hardware: Hardware; boot: { state: string; message?: string | null };
  stats?: Stats; catalog_staged?: boolean;
};
export type Target = { model: string; profile: string };
export type Action =
  | ({ action: 'start' | 'stop' } & Target)
  | ({ action: 'switch'; from_model: string; from_profile: string } & Target);
export type Preview = { catalog: unknown; summary: unknown };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function api(token = '', request: typeof fetch = fetch) {
  async function json<T>(path: string, options: RequestInit = {}, allowEmpty = false): Promise<T> {
    const headers = new Headers({ Accept: 'application/json' });
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (options.body) headers.set('Content-Type', 'application/json');
    const response = await request(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    const data = await response.json().catch(() => undefined);
    if (!response.ok) {
      const detail = data?.detail ?? data?.message ?? data?.error;
      throw new ApiError(response.status, typeof detail === 'string' ? detail : `Request failed (HTTP ${response.status}).`);
    }
    if (data === undefined && !allowEmpty) {
      throw new ApiError(response.status, 'The service did not return JSON.');
    }
    return data as T;
  }

  return {
    status: (signal?: AbortSignal) => json<Status>('/api/status', { signal }),
    stats: (signal?: AbortSignal) => json<Stats>('/api/stats', { signal }),
    action: (data: Action) => json<unknown>('/api/actions', { method: 'POST', body: JSON.stringify(data) }, true),
    logs: (target: Target, signal?: AbortSignal) =>
      json<{ lines: string[] }>(`/api/logs?${new URLSearchParams(target)}`, { signal }),
    preview: (data: { url: string } | { catalog: Record<string, unknown> }) =>
      json<Preview>('/api/catalog/preview', { method: 'POST', body: JSON.stringify(data) }),
    stage: (catalog: unknown) => json<{ state: 'staged'; message: string }>('/api/catalog/stage', {
      method: 'POST', body: JSON.stringify({ catalog })
    })
  };
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError && [401, 403].includes(error.status)) {
    return `${error.message} Check the bearer token in Settings.`;
  }
  return error instanceof Error ? error.message : 'Unexpected service error.';
}
