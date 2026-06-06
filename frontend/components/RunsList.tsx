'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { listRuns } from '@/lib/api';
import { statusColour } from '@/lib/status';
import type { RunDTO } from '@/lib/types';

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
      <div className="rounded-xl border border-rose-700/60 bg-rose-950/30 p-4 text-sm text-rose-200">
        Backend not reachable: {error}
        <div className="mt-2 text-zinc-400 text-xs">
          Is the FastAPI server running? <code className="text-zinc-300">cd backend &amp;&amp; python run_server.py</code>
        </div>
      </div>
    );
  }
  if (runs == null) return <p className="text-zinc-500">Loading runs…</p>;
  if (runs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-900/20 p-8 text-center">
        <p className="text-sm text-zinc-400">No runs yet.</p>
        <p className="mt-1 text-xs text-zinc-500">Type an idea above and click <span className="text-cyan-400">Ship it</span>.</p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-zinc-800 bg-gradient-to-br from-zinc-900/40 to-zinc-950/40 shadow-lg shadow-black/20">
      <table className="w-full text-sm">
        <thead className="bg-zinc-900/60 text-zinc-400 text-[11px] uppercase tracking-wider">
          <tr>
            <th className="px-4 py-2.5 text-left font-medium">#</th>
            <th className="px-4 py-2.5 text-left font-medium">Idea</th>
            <th className="px-4 py-2.5 text-left font-medium">Status</th>
            <th className="px-4 py-2.5 text-right font-medium">Cost</th>
            <th className="px-4 py-2.5 text-left font-medium">Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr
              key={r.id}
              className="border-t border-zinc-800/80 hover:bg-zinc-900/60 transition-colors"
            >
              <td className="px-4 py-3 text-zinc-500 tabular-nums">{r.id}</td>
              <td className="px-4 py-3">
                <Link
                  href={`/runs/${r.id}`}
                  className="text-zinc-100 hover:text-white hover:underline decoration-zinc-600 underline-offset-2"
                >
                  {r.idea}
                </Link>
              </td>
              <td className="px-4 py-3">
                <span
                  className={`inline-flex items-center gap-1.5 rounded-full border border-current/30 bg-current/5 px-2 py-0.5 text-[11px] font-medium ${statusColour(r.status)}`}
                >
                  {r.live && <span className="inline-block h-1.5 w-1.5 rounded-full bg-current animate-pulse" />}
                  {r.status}
                </span>
              </td>
              <td className="px-4 py-3 text-right tabular-nums text-zinc-300">
                {fmtCost(r.total_cost_usd)}
              </td>
              <td className="px-4 py-3 text-zinc-400 text-xs">{fmtTime(r.started_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
