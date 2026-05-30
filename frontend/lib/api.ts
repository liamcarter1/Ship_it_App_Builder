// Thin typed wrapper over fetch. The dashboard talks to its own origin at
// `/api/*`, and next.config.mjs rewrites those calls to the FastAPI backend
// (default http://127.0.0.1:8000). Keeping a single base path here means
// switching to a different deployment topology is a one-line change.
import type { RunDTO } from './types';

const API = '/api';

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: 'no-store' });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export async function listRuns(): Promise<RunDTO[]> {
  return getJson<RunDTO[]>('/runs');
}

export async function getRun(id: number): Promise<RunDTO> {
  return getJson<RunDTO>(`/runs/${id}`);
}

export interface NewRunInput {
  idea: string;
  max_rounds?: number;
  deploy?: boolean;
}

export async function createRun(input: NewRunInput): Promise<{ run_id: number }> {
  const res = await fetch(`${API}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as { run_id: number };
}

export function streamUrl(runId: number): string {
  return `${API}/runs/${runId}/events/stream`;
}
