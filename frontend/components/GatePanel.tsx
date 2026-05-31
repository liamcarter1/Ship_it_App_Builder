'use client';

import { useState } from 'react';
import { resolveGate } from '@/lib/api';
import type { GateName } from '@/lib/types';

const GATE_TITLES: Record<GateName, string> = {
  spec: 'Spec gate — review what the Planner produced',
  code: 'Code gate — review the diff before deploying',
  deploy: 'Deploy gate — confirm before pushing to Vercel',
};

const GATE_HINTS: Record<GateName, string> = {
  spec: 'Cheapest place to course-correct. Notes here flow into the Coder\'s brief.',
  code: 'Lint, typecheck, and build are already green. Reject here if the diff drifted from the spec.',
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

      <div className="mt-3">
        <GateBody name={name} payload={payload} />
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

// --- per-gate body renderers ------------------------------------------------

function GateBody({ name, payload }: { name: GateName; payload: Record<string, unknown> }) {
  if (name === 'spec') return <SpecView spec={(payload.spec ?? payload) as SpecShape} />;
  if (name === 'code') {
    return (
      <CodeView
        workspace={String(payload.workspace ?? '')}
        verdict={(payload.verdict ?? {}) as VerdictShape}
        diff={typeof payload.diff === 'string' ? payload.diff : null}
      />
    );
  }
  if (name === 'deploy') return <DeployView workspace={String(payload.workspace ?? '')} />;
  // Unknown gate: fall back to raw JSON.
  return <PreJSON value={payload} />;
}

interface SpecShape {
  title?: string;
  one_liner?: string;
  audience?: string;
  sections?: Array<{ name?: string; purpose?: string; key_content?: string }>;
  primary_cta?: { label?: string; intent?: string };
  visual_tone?: string;
  notes_for_coder?: string;
}

function SpecView({ spec }: { spec: SpecShape }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 p-3 space-y-2 text-sm">
      {spec.title && <h4 className="text-zinc-100 font-semibold">{spec.title}</h4>}
      {spec.one_liner && <p className="text-zinc-300 italic">{spec.one_liner}</p>}
      {spec.audience && (
        <p className="text-xs text-zinc-400">
          <span className="text-zinc-500">audience:</span> {spec.audience}
        </p>
      )}
      {spec.visual_tone && (
        <p className="text-xs text-zinc-400">
          <span className="text-zinc-500">tone:</span> {spec.visual_tone}
        </p>
      )}
      {spec.sections && spec.sections.length > 0 && (
        <div className="mt-2">
          <div className="text-[11px] uppercase tracking-wide text-zinc-500 mb-1">sections</div>
          <ul className="space-y-1">
            {spec.sections.map((s, i) => (
              <li key={i} className="border-l border-zinc-800 pl-2">
                <div className="text-zinc-200">{s.name}</div>
                {s.purpose && <div className="text-xs text-zinc-500">{s.purpose}</div>}
                {s.key_content && <div className="text-xs text-zinc-400">{s.key_content}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {spec.primary_cta && (
        <p className="text-xs text-zinc-400">
          <span className="text-zinc-500">CTA:</span> {spec.primary_cta.label}
          {spec.primary_cta.intent && <span className="text-zinc-500"> — {spec.primary_cta.intent}</span>}
        </p>
      )}
      {spec.notes_for_coder && (
        <p className="text-xs text-amber-300/80">
          <span className="text-amber-500">notes for coder:</span> {spec.notes_for_coder}
        </p>
      )}
      <details className="mt-2">
        <summary className="cursor-pointer text-[11px] text-zinc-600 hover:text-zinc-400">raw JSON</summary>
        <PreJSON value={spec} />
      </details>
    </div>
  );
}

interface VerdictShape {
  verdict?: string;
  issues?: Array<{ tool?: string; summary?: string; evidence?: string }>;
}

function CodeView({
  workspace,
  verdict,
  diff,
}: {
  workspace: string;
  verdict: VerdictShape;
  diff: string | null;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3 text-xs text-zinc-400">
        <span className="text-emerald-300">verdict: {verdict.verdict ?? '?'}</span>
        <span className="text-zinc-600">·</span>
        <span className="truncate">{workspace}</span>
      </div>
      {diff ? (
        <DiffView diff={diff} />
      ) : (
        <p className="text-xs text-zinc-500 italic rounded border border-zinc-800 bg-zinc-950 p-3">
          No diff available (workspace isn&apos;t a git repo, or no changes vs. scaffold).
        </p>
      )}
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split('\n');
  return (
    <pre className="max-h-96 overflow-auto rounded border border-zinc-800 bg-zinc-950 p-3 text-xs leading-relaxed">
      {lines.map((line, i) => {
        let cls = 'text-zinc-400';
        if (line.startsWith('+++') || line.startsWith('---')) cls = 'text-zinc-500 font-semibold';
        else if (line.startsWith('@@')) cls = 'text-cyan-400';
        else if (line.startsWith('+')) cls = 'text-emerald-400';
        else if (line.startsWith('-')) cls = 'text-rose-400';
        else if (line.startsWith('diff --git')) cls = 'text-zinc-300 font-semibold mt-2';
        return (
          <span key={i} className={`block ${cls} whitespace-pre`}>
            {line || ' '}
          </span>
        );
      })}
    </pre>
  );
}

function DeployView({ workspace }: { workspace: string }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 p-3 text-sm space-y-1">
      <p className="text-zinc-200">Ready to push this workspace to Vercel production:</p>
      <p className="text-xs text-zinc-400 break-all">{workspace}</p>
    </div>
  );
}

function PreJSON({ value }: { value: unknown }) {
  return (
    <pre className="mt-1 max-h-72 overflow-y-auto rounded bg-zinc-950 border border-zinc-800 p-2 text-xs text-zinc-300 whitespace-pre-wrap break-words">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
