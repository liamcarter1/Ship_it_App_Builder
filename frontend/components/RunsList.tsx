'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { listRuns } from '@/lib/api';
import type { RunDTO } from '@/lib/types';

const STATUS_COLOUR: Record<string, string> = {
  running: 'text-cyan-300 animate-pulse',
  built: 'text-emerald-300',
  deployed: 'text-emerald-400',
  failed_review: 'text-rose-300',
  failed_planner: 'text-rose-300',
  failed_scaffold: 'text-rose-300',
  deploy_failed: 'text-amber-300',
  errored: 'text-rose-400',
};

function fmtCost(c: number | null): string {
  return c == null ? '—' : `$${c.toFixed(4)}`;
}

function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

export function RunsList() {
  const [runs, setRuns] = useState<RunDTO[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Refresh every 2s so live runs surface their progress without a hard reload.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await listRuns();
        if (alive) {
          setRuns(r);
          setError(null);
        }
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    };
    tick();
    const handle = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(handle);
    };
  }, []);

  if (error) {
    return (
      <div className="rounded border border-rose-700 bg-rose-950/40 p-4 text-sm text-rose-200">
        Backend not reachable: {error}
        <div className="mt-2 text-zinc-400 text-xs">
          Is the FastAPI server running? <code>cd backend &amp;&amp; uvicorn app.server:app --port 8000</code>
        </div>
      </div>
    );
  }
  if (runs == null) return <p className="text-zinc-500">Loading runs…</p>;
  if (runs.length === 0) return <p className="text-zinc-500">No runs yet. Try one above.</p>;

  return (
    <div className="overflow-hidden rounded border border-zinc-800">
      <table className="w-full text-sm">
        <thead className="bg-zinc-900 text-zinc-400">
          <tr>
            <th className="px-3 py-2 text-left">#</th>
            <th className="px-3 py-2 text-left">Idea</th>
            <th className="px-3 py-2 text-left">Status</th>
            <th className="px-3 py-2 text-right">Cost</th>
            <th className="px-3 py-2 text-left">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="border-t border-zinc-800 hover:bg-zinc-900/60">
              <td className="px-3 py-2 text-zinc-500">{r.id}</td>
              <td className="px-3 py-2">
                <Link href={`/runs/${r.id}`} className="text-zinc-100 hover:underline">
                  {r.idea}
                </Link>
              </td>
              <td className={`px-3 py-2 ${STATUS_COLOUR[r.status] ?? 'text-zinc-300'}`}>
                {r.status}
                {r.live && <span className="ml-1 text-cyan-400">● live</span>}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-zinc-300">
                {fmtCost(r.total_cost_usd)}
              </td>
              <td className="px-3 py-2 text-zinc-400">{fmtTime(r.started_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
