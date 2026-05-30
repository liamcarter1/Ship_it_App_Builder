'use client';

import { useState } from 'react';
import { resolveGate } from '@/lib/api';
import type { GateName } from '@/lib/types';

const GATE_TITLES: Record<GateName, string> = {
  spec: 'Spec gate — review what the Planner produced',
  code: 'Code gate — review the green build before deploying',
  deploy: 'Deploy gate — confirm before pushing to Vercel',
};

const GATE_HINTS: Record<GateName, string> = {
  spec: 'Cheapest place to course-correct. Notes here flow into the Coder\'s brief.',
  code: 'Lint, typecheck, and build are already green. Reject here if the implementation drifted from the spec.',
  deploy: 'Last stop before this app goes live on Vercel.',
};

interface Props {
  runId: number;
  name: GateName;
  payload: Record<string, unknown>;
  /** Called after the API accepts the decision; parent typically clears the
   *  open-gate entry for this name (the gate_decision SSE event also clears
   *  it, but the optimistic update keeps the UI snappy). */
  onResolved: (approved: boolean) => void;
}

export function GatePanel({ runId, name, payload, onResolved }: Props) {
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      await resolveGate(runId, name, approve, notes.trim() || undefined);
      onResolved(approve);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="mb-3 rounded border border-amber-600/70 bg-amber-950/30 p-4">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-amber-200">
          <span className="mr-1">⏸</span>
          {GATE_TITLES[name]}
        </h3>
        <span className="text-[10px] uppercase tracking-wider text-amber-400">
          awaiting your decision
        </span>
      </div>
      <p className="mt-1 text-xs text-amber-300/70">{GATE_HINTS[name]}</p>

      <div className="mt-3 max-h-72 overflow-y-auto rounded bg-zinc-950 border border-zinc-800 p-2">
        <pre className="text-xs text-zinc-300 whitespace-pre-wrap break-words">
          {JSON.stringify(payload, null, 2)}
        </pre>
      </div>

      <label className="mt-3 block text-[11px] uppercase tracking-wide text-zinc-500">
        Notes {name === 'spec' && '(passed to the Coder as a hard requirement)'}
      </label>
      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        placeholder={
          name === 'spec'
            ? 'e.g. "use a muted palette, no emoji, headline must mention indie"'
            : 'Optional rationale for the record'
        }
        rows={2}
        maxLength={2000}
        className="mt-1 w-full rounded bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm text-zinc-100
                   focus:outline-none focus:border-amber-700"
      />

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => submit(true)}
          className="rounded bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50
                     px-3 py-1.5 text-zinc-950 text-sm font-semibold"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => submit(false)}
          className="rounded bg-rose-700 hover:bg-rose-600 disabled:opacity-50
                     px-3 py-1.5 text-zinc-50 text-sm"
        >
          Reject
        </button>
        {busy && <span className="text-xs text-zinc-400">submitting…</span>}
        {error && <span className="ml-2 text-xs text-rose-300">{error}</span>}
      </div>
    </div>
  );
}
