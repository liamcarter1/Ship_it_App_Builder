'use client';

import { useEffect, useState } from 'react';
import { deployRun, getPreview, startPreview, stopPreview } from '@/lib/api';
import type { PreviewDTO } from '@/lib/types';

interface Props {
  runId: number;
  /** Current run status. Drives which actions are offered:
   *  - 'built'    → Preview + Deploy
   *  - 'deployed' → Preview only (Deploy URL surfaced as a link) */
  runStatus: string;
  /** If the run has already been deployed, the Vercel URL goes here. */
  deployUrl: string | null;
}

export function PreviewPanel({ runId, runStatus, deployUrl }: Props) {
  const [preview, setPreview] = useState<PreviewDTO>({ active: false });
  const [busy, setBusy] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Poll preview status so the panel reflects external changes (idle auto-stop,
  // another run taking over) and so each poll keeps this preview's idle timer warm.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const p = await getPreview(runId);
        if (alive) setPreview(p);
      } catch {
        /* transient poll errors are non-fatal */
      }
    };
    tick();
    const handle = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(handle);
    };
  }, [runId]);

  async function handleStart() {
    setBusy(true);
    setError(null);
    try {
      setPreview(await startPreview(runId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    setError(null);
    try {
      await stopPreview(runId);
      setPreview({ active: false });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeploy() {
    if (!window.confirm(
      'Deploy this build to Vercel production?\n\n' +
      'Uses your VERCEL_TOKEN from backend/.env. A new Vercel project will be created for this workspace.'
    )) return;
    setDeploying(true);
    setError(null);
    try {
      await deployRun(runId);
      // The run row's status flips to 'running' on the backend; the parent
      // page's 1.5s getRun poll will pick that up and propagate to the
      // status pill, live indicator, etc. We just hold a local "deploying"
      // hint until the status leaves 'built'.
    } catch (e) {
      setError((e as Error).message);
      setDeploying(false);
    }
  }

  // Once the parent's runStatus leaves 'built' (deploy kicked off → 'running',
  // then 'deployed'/'deploy_failed'), clear our local optimistic flag.
  useEffect(() => {
    if (runStatus !== 'built') setDeploying(false);
  }, [runStatus]);

  const canDeploy = runStatus === 'built';

  return (
    <div className="rounded-xl border border-zinc-800 bg-gradient-to-br from-zinc-900/60 to-zinc-950/40 p-5 space-y-3 shadow-lg shadow-black/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span aria-hidden="true" className="text-base">🛠</span>
          <span className="text-sm font-semibold text-zinc-200">Try it out</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {preview.active ? (
            <button
              type="button"
              onClick={handleStop}
              disabled={busy}
              className="rounded-md border border-rose-700/60 bg-rose-950/30 hover:bg-rose-900/40
                         px-3 py-1.5 text-xs font-medium text-rose-200 disabled:opacity-50 transition"
            >
              Stop preview
            </button>
          ) : (
            <button
              type="button"
              onClick={handleStart}
              disabled={busy || deploying}
              className="rounded-md border border-cyan-700/60 bg-cyan-950/30 hover:bg-cyan-900/40
                         px-3 py-1.5 text-xs font-medium text-cyan-200 disabled:opacity-50 transition"
            >
              {busy ? 'Starting preview…' : 'Preview locally'}
            </button>
          )}
          {canDeploy && (
            <button
              type="button"
              onClick={handleDeploy}
              disabled={deploying || busy}
              className="rounded-md bg-gradient-to-r from-fuchsia-600 to-purple-600 hover:from-fuchsia-500 hover:to-purple-500
                         px-3 py-1.5 text-xs font-semibold text-white shadow-md shadow-purple-900/40
                         disabled:opacity-50 disabled:cursor-not-allowed transition"
            >
              {deploying ? 'Deploying…' : 'Deploy to Vercel ▲'}
            </button>
          )}
        </div>
      </div>

      {preview.active && preview.url && (
        <a
          href={preview.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 text-sm text-cyan-300 hover:text-cyan-200 underline"
        >
          ▸ {preview.url}
        </a>
      )}

      {deployUrl && !preview.active && (
        <a
          href={deployUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 text-sm text-purple-300 hover:text-purple-200 underline"
        >
          ▸ Live on Vercel: {deployUrl}
        </a>
      )}

      {!preview.active && !busy && !deployUrl && (
        <p className="text-xs text-zinc-500 leading-relaxed">
          <strong className="text-zinc-400">Preview locally</strong> runs{' '}
          <code className="text-zinc-400">npm run dev</code> in the workspace and gives you a link (auto-stops after 30 min idle).
          {canDeploy && (
            <>
              {' '}<strong className="text-zinc-400">Deploy to Vercel</strong> pushes the green build to a fresh Vercel project on your account.
            </>
          )}
        </p>
      )}

      {error && (
        <p className="rounded-md border border-rose-700/60 bg-rose-950/30 px-3 py-2 text-xs text-rose-200">
          {error}
        </p>
      )}
    </div>
  );
}
