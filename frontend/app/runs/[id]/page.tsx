'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ActivityStream } from '@/components/ActivityStream';
import { PreviewPanel } from '@/components/PreviewPanel';
import { cancelRun, getRun } from '@/lib/api';
import { statusColour } from '@/lib/status';
import type { RunDTO } from '@/lib/types';

interface PageProps {
  // Next.js 14: route params are a plain synchronous object (Next 15 makes
  // them a Promise consumed via React.use(); we're pinned to 14).
  params: { id: string };
}

export default function RunPage({ params }: PageProps) {
  const runId = Number(params.id);
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

  async function handleCancel() {
    if (!run?.live) return;
    if (!window.confirm('Cancel this run? Pending gates will be rejected and the run will end as `rejected_at_<gate>`.')) return;
    try {
      await cancelRun(runId);
    } catch (e) {
      setError((e as Error).message);
    }
  }

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
  const previewable = ['built', 'deployed', 'deploy_failed'].includes(run.status);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <Link href="/" className="text-sm text-zinc-500 hover:text-zinc-300">← all runs</Link>
        <div className="flex items-center gap-3">
          {run.live && (
            <button
              type="button"
              onClick={handleCancel}
              className="rounded border border-rose-700 hover:bg-rose-950/40 px-2 py-0.5 text-xs text-rose-300"
            >
              Cancel run
            </button>
          )}
          <span className="text-xs text-zinc-500">run #{run.id}</span>
        </div>
      </div>

      <div className="rounded-xl border border-zinc-800 bg-gradient-to-br from-zinc-900/60 to-zinc-950/40 p-5 space-y-3 shadow-lg shadow-black/20">
        <h1 className="text-xl font-medium text-zinc-100 leading-snug">{run.idea}</h1>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border border-current/30 bg-current/5 px-2.5 py-0.5 text-xs font-medium ${statusColour(run.status)}`}
          >
            {run.live && <span className="inline-block h-1.5 w-1.5 rounded-full bg-current animate-pulse" />}
            {run.status}
          </span>
          {run.total_cost_usd != null && (
            <span className="rounded-full bg-zinc-800/60 px-2.5 py-0.5 text-xs text-zinc-300">
              cost <span className="text-zinc-100 tabular-nums">${run.total_cost_usd.toFixed(4)}</span>
            </span>
          )}
          {run.deploy_url && (
            <a
              href={run.deploy_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 rounded-full border border-purple-700/60 bg-purple-950/30 px-2.5 py-0.5 text-xs text-purple-200 hover:bg-purple-900/40 transition"
            >
              ▲ {run.deploy_url}
            </a>
          )}
        </div>
        {run.error && (
          <p className="rounded-md border border-rose-700/60 bg-rose-950/30 px-3 py-2 text-xs text-rose-200">
            <span className="font-semibold">error:</span> {run.error}
          </p>
        )}
        <p className="text-[11px] text-zinc-500 break-all">workspace: <code>{run.workspace}</code></p>
      </div>

      {previewable && (
        <PreviewPanel
          runId={runId}
          runStatus={run.status}
          deployUrl={run.deploy_url}
        />
      )}

      <ActivityStream
        runId={runId}
        alreadyFinished={finished}
        liveOnBackend={run.live}
        runStatus={run.status}
      />
    </div>
  );
}
