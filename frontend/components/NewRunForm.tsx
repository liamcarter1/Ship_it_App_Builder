'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { createRun } from '@/lib/api';

// Five roles in the pipeline; each can take an optional model override.
// Empty string = SDK default. Accepts both aliases ("sonnet", "haiku") and
// full model IDs ("claude-haiku-4-5-20251001").
const MODEL_ROLES = [
  ['planner_model',    'Planner',    'think-only — cheapest role'],
  ['scaffolder_model', 'Scaffolder', 'writes config files, runs npm install'],
  ['coder_model',      'Coder',      'implements the spec'],
  ['reviewer_model',   'Reviewer',   'runs lint/tsc/build'],
  ['deployer_model',   'Deployer',   'invokes the Vercel CLI'],
] as const;

type ModelKey = (typeof MODEL_ROLES)[number][0];

export function NewRunForm() {
  const router = useRouter();
  const [idea, setIdea] = useState('');
  const [maxRounds, setMaxRounds] = useState(3);
  const [deploy, setDeploy] = useState(false);
  const [models, setModels] = useState<Record<ModelKey, string>>({
    planner_model: '',
    scaffolder_model: '',
    coder_model: '',
    reviewer_model: '',
    deployer_model: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      // Drop empty model fields so the server uses SDK defaults.
      const modelOverrides = Object.fromEntries(
        Object.entries(models).filter(([, v]) => v.trim().length > 0)
      );
      const { run_id } = await createRun({
        idea: idea.trim(),
        max_rounds: maxRounds,
        deploy,
        ...modelOverrides,
      });
      router.push(`/runs/${run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} className="rounded border border-zinc-800 bg-zinc-900/40 p-4 space-y-3">
      <label className="block text-xs uppercase tracking-wide text-zinc-500">App idea</label>
      <textarea
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
        placeholder="a calm one-page landing site for a tiny indie note-taking app"
        rows={2}
        minLength={3}
        maxLength={500}
        className="w-full rounded bg-zinc-950 border border-zinc-800 px-3 py-2 text-zinc-100
                   focus:outline-none focus:border-cyan-700"
        required
      />
      <div className="flex items-center gap-6 text-sm">
        <label className="flex items-center gap-2 text-zinc-300">
          Max review rounds
          <input
            type="number"
            min={1}
            max={10}
            value={maxRounds}
            onChange={(e) => setMaxRounds(parseInt(e.target.value, 10) || 3)}
            className="w-16 rounded bg-zinc-950 border border-zinc-800 px-2 py-1 text-right"
          />
        </label>
        <label className="flex items-center gap-2 text-zinc-300">
          <input
            type="checkbox"
            checked={deploy}
            onChange={(e) => setDeploy(e.target.checked)}
          />
          Deploy to Vercel
        </label>
        <button
          type="submit"
          disabled={submitting || idea.trim().length < 3}
          className="ml-auto rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 disabled:cursor-not-allowed
                     px-4 py-1.5 text-zinc-950 font-semibold"
        >
          {submitting ? 'Starting…' : 'Ship it'}
        </button>
      </div>

      <details className="border-t border-zinc-800 pt-3">
        <summary className="cursor-pointer text-xs uppercase tracking-wide text-zinc-500 hover:text-zinc-300">
          Advanced — per-worker model overrides
        </summary>
        <p className="mt-2 text-xs text-zinc-500">
          Leave blank for the SDK default. Cheaper models on Planner/Reviewer are the main cost lever.
          Accepts aliases (<code>sonnet</code>, <code>haiku</code>) or full IDs.
        </p>
        <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-2">
          {MODEL_ROLES.map(([key, label, hint]) => (
            <label key={key} className="flex flex-col gap-1 text-sm">
              <span className="text-zinc-300">
                {label} <span className="text-zinc-600 text-xs">({hint})</span>
              </span>
              <input
                type="text"
                value={models[key]}
                onChange={(e) => setModels((m) => ({ ...m, [key]: e.target.value }))}
                placeholder="default"
                maxLength={100}
                className="rounded bg-zinc-950 border border-zinc-800 px-2 py-1 text-zinc-100
                           focus:outline-none focus:border-cyan-700"
              />
            </label>
          ))}
        </div>
      </details>

      {error && <p className="text-sm text-rose-300">Error: {error}</p>}
    </form>
  );
}
