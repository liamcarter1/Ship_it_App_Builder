'use client';

import { useEffect, useState } from 'react';
import { getPreview, startPreview, stopPreview } from '@/lib/api';
import type { PreviewDTO } from '@/lib/types';

export function PreviewPanel({ runId }: { runId: number }) {
  const [preview, setPreview] = useState<PreviewDTO>({ active: false });
  const [busy, setBusy] = useState(false);
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

  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm text-zinc-300">Local preview</span>
        {preview.active ? (
          <button
            type="button"
            onClick={handleStop}
            disabled={busy}
            className="rounded border border-rose-700 hover:bg-rose-950/40 px-2 py-0.5 text-xs text-rose-300 disabled:opacity-50"
          >
            Stop preview
          </button>
        ) : (
          <button
            type="button"
            onClick={handleStart}
            disabled={busy}
            className="rounded border border-cyan-700 hover:bg-cyan-950/40 px-2 py-0.5 text-xs text-cyan-300 disabled:opacity-50"
          >
            {busy ? 'Starting preview…' : 'Preview locally'}
          </button>
        )}
      </div>

      {preview.active && preview.url && (
        <a
          href={preview.url}
          target="_blank"
          rel="noreferrer"
          className="text-cyan-400 underline text-sm"
        >
          {preview.url}
        </a>
      )}

      {!preview.active && !busy && (
        <p className="text-xs text-zinc-500">
          Launches the generated app with <code>npm run dev</code> and gives you a
          link. Stops itself after 30 minutes idle.
        </p>
      )}

      {error && <p className="text-xs text-rose-300">{error}</p>}
    </div>
  );
}
