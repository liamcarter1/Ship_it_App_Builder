'use client';

import { use, useEffect, useState } from 'react';
import Link from 'next/link';
import { ActivityStream } from '@/components/ActivityStream';
import { getRun } from '@/lib/api';
import type { RunDTO } from '@/lib/types';

interface PageProps {
  params: Promise<{ id: string }>;
}

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

export default function RunPage({ params }: PageProps) {
  const { id } = use(params);
  const runId = Number(id);
  const [run, setRun] = useState<RunDTO | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Refresh the run row alongside the SSE stream — it carries final totals
  // (cost, deploy URL, status) that we can show even before the stream ends.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await getRun(runId);
        if (alive) {
          setRun(r);
          setError(null);
        }
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    };
    tick();
    // Slower refresh once the run is no longer live.
    const handle = setInterval(tick, run?.live ? 1500 : 5000);
    return () => {
      alive = false;
      clearInterval(handle);
    };
  }, [runId, run?.live]);

  if (Number.isNaN(runId)) return <p>Invalid run id.</p>;
  if (error)
    return (
      <div className="rounded border border-rose-700 bg-rose-950/40 p-4 text-sm text-rose-200">
        {error}
        <div className="mt-2">
          <Link href="/" className="text-cyan-400 underline">← back</Link>
        </div>
      </div>
    );
  if (!run) return <p className="text-zinc-500">Loading run #{runId}…</p>;

  const finished = !run.live && run.status !== 'running';

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <Link href="/" className="text-sm text-zinc-500 hover:text-zinc-300">← all runs</Link>
        <span className="text-xs text-zinc-500">run #{run.id}</span>
      </div>

      <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4 space-y-2">
        <h1 className="text-lg text-zinc-100">{run.idea}</h1>
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <span className={STATUS_COLOUR[run.status] ?? 'text-zinc-300'}>
            {run.status}
            {run.live && <span className="ml-1 text-cyan-400">● live</span>}
          </span>
          {run.total_cost_usd != null && (
            <span className="text-zinc-400">
              cost <span className="text-zinc-200 tabular-nums">${run.total_cost_usd.toFixed(4)}</span>
            </span>
          )}
          {run.deploy_url && (
            <a
              href={run.deploy_url}
              target="_blank"
              rel="noreferrer"
              className="text-purple-300 underline"
            >
              {run.deploy_url}
            </a>
          )}
        </div>
        {run.error && (
          <p className="text-xs text-rose-300">error: {run.error}</p>
        )}
        <p className="text-xs text-zinc-500">workspace: {run.workspace}</p>
      </div>

      <ActivityStream runId={runId} alreadyFinished={finished} />
    </div>
  );
}
