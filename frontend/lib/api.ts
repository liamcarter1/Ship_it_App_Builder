// Thin typed wrapper over fetch. The dashboard talks to its own origin at
// `/api/*`, and next.config.mjs rewrites those calls to the FastAPI backend
// (default http://127.0.0.1:8000). Keeping a single base path here means
// switching to a different deployment topology is a one-line change.
import type { PreviewDTO, RunDTO } from './types';

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
  planner_model?: string;
  scaffolder_model?: string;
  coder_model?: string;
  reviewer_model?: string;
  deployer_model?: string;
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

export async function resolveGate(
  runId: number,
  name: string,
  approve: boolean,
  notes?: string,
): Promise<void> {
  const res = await fetch(`${API}/runs/${runId}/gate/${name}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      decision: approve ? 'approve' : 'reject',
      notes: notes && notes.length > 0 ? notes : undefined,
    }),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
}

export async function cancelRun(runId: number): Promise<{ cancelled_gates: string[] }> {
  const res = await fetch(`${API}/runs/${runId}/cancel`, { method: 'POST' });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as { cancelled_gates: string[] };
}

export async function getPreview(runId: number): Promise<PreviewDTO> {
  return getJson<PreviewDTO>(`/runs/${runId}/preview`);
}

export async function startPreview(runId: number): Promise<PreviewDTO> {
  const res = await fetch(`${API}/runs/${runId}/preview`, { method: 'POST' });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as PreviewDTO;
}

export async function stopPreview(runId: number): Promise<{ stopped: boolean }> {
  const res = await fetch(`${API}/runs/${runId}/preview`, { method: 'DELETE' });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as { stopped: boolean };
}
