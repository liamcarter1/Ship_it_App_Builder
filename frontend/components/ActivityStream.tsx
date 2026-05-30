'use client';

import { useEffect, useRef, useState } from 'react';
import { streamUrl } from '@/lib/api';
import type { EventKind, EventSource, PipelineEventDTO } from '@/lib/types';

// Tailwind classes per event source. The base name before any `-r<N>` suffix
// determines colour (so `coder-r2` looks like `coder`).
function sourceClasses(source: EventSource): string {
  const base = source.split('-')[0];
  switch (base) {
    case 'orchestrator': return 'text-cyan-400';
    case 'planner':      return 'text-fuchsia-400';
    case 'scaffolder':   return 'text-blue-400';
    case 'coder':        return 'text-emerald-400';
    case 'reviewer':     return 'text-rose-400';
    case 'deployer':     return 'text-purple-400';
    case 'tool':         return 'text-amber-300';
    case 'system':       return 'text-zinc-500';
    default:             return 'text-zinc-300';
  }
}

interface Props {
  runId: number;
  /** When the parent says the run has finished, we close the stream and stop
   *  showing "live". The stream itself also closes on `pipeline_end`. */
  alreadyFinished: boolean;
}

export function ActivityStream({ runId, alreadyFinished }: Props) {
  const [events, setEvents] = useState<PipelineEventDTO[]>([]);
  const [connected, setConnected] = useState(false);
  const [streamEnded, setStreamEnded] = useState(alreadyFinished);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // EventSource auto-reconnects on transient drops, which matches the
    // backend's poll-based stream: a reconnection re-sends history (the
    // dashboard de-dupes by event.id).
    const es = new globalThis.EventSource(streamUrl(runId));
    es.onopen = () => setConnected(true);
    es.onerror = () => {
      // Fired both on transient drops AND on permanent close after the
      // server-side stream returns. We only treat it as a real error if we
      // haven't already observed pipeline_end / stream_end.
      setConnected(false);
    };

    // Each PipelineEvent kind comes through as its own SSE event name (set
    // by `event: <kind>` in server.py). For consistency we add one listener
    // per kind we render; unknown kinds fall through to the default 'message'.
    const allKinds: EventKind[] = [
      'pipeline_start', 'stage_start', 'stage_end', 'agent_text',
      'tool_use', 'tool_result', 'system', 'review_verdict', 'deploy_url',
      'pipeline_end', 'stream_end',
    ];
    const handlers: Array<[string, EventListener]> = [];

    function addHandler(name: string, fn: (data: PipelineEventDTO) => void) {
      const handler: EventListener = (rawEvent) => {
        try {
          const parsed = JSON.parse((rawEvent as MessageEvent).data);
          fn(parsed);
        } catch (e) {
          // Server may emit non-PipelineEvent shapes (e.g. stream_end)
          fn({ ts: 0, kind: 'system', source: 'system', text: null, meta: {} });
        }
      };
      es.addEventListener(name, handler);
      handlers.push([name, handler]);
    }

    for (const kind of allKinds) {
      addHandler(kind, (parsed) => {
        if (kind === 'stream_end') {
          setStreamEnded(true);
          es.close();
          return;
        }
        setEvents((prev) => {
          // Dedupe by id (in case of EventSource auto-reconnect replay).
          if (parsed.id != null && prev.some((p) => p.id === parsed.id)) return prev;
          return [...prev, { ...parsed, kind: parsed.kind ?? (kind as EventKind) }];
        });
        if (parsed.kind === 'pipeline_end') {
          setStreamEnded(true);
          es.close();
        }
      });
    }

    return () => {
      handlers.forEach(([name, fn]) => es.removeEventListener(name, fn));
      es.close();
    };
  }, [runId]);

  // Auto-scroll to bottom on each new event while live.
  useEffect(() => {
    if (!streamEnded) bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [events.length, streamEnded]);

  return (
    <div className="rounded border border-zinc-800 bg-zinc-950">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2 text-xs">
        <div className="text-zinc-400">
          {events.length} event{events.length === 1 ? '' : 's'}
        </div>
        <div className="text-zinc-400">
          {streamEnded ? (
            <span className="text-zinc-500">stream ended</span>
          ) : connected ? (
            <span className="text-cyan-400">● live</span>
          ) : (
            <span className="text-amber-400">reconnecting…</span>
          )}
        </div>
      </div>
      <div className="max-h-[60vh] overflow-y-auto p-3 text-sm leading-relaxed">
        {events.length === 0 ? (
          <p className="text-zinc-600 italic">Waiting for the orchestrator to emit its first event…</p>
        ) : (
          events.map((ev, i) => <EventRow key={ev.id ?? `${ev.ts}-${i}`} ev={ev} />)
        )}
        <div ref={bottomRef} />
      </div>
      {error && <p className="px-3 py-2 text-xs text-rose-300">SSE error: {error}</p>}
    </div>
  );
}

function EventRow({ ev }: { ev: PipelineEventDTO }) {
  const cls = sourceClasses(ev.source);
  switch (ev.kind) {
    case 'stage_start':
      return (
        <div className={`${cls} text-xs uppercase tracking-wide mt-3`}>
          ── {ev.source} ── start ──
        </div>
      );
    case 'stage_end':
      return (
        <div className={`${cls} text-xs uppercase tracking-wide`}>
          ── {ev.source} ── end {ev.text}
          {ev.meta.cost_usd != null && (
            <span className="text-zinc-500"> · ${(ev.meta.cost_usd as number).toFixed(4)}</span>
          )}
          {' '}──
        </div>
      );
    case 'tool_use':
      return (
        <div className="text-zinc-400">
          <span className={cls}>[{ev.source}]</span> tool: <span className="text-amber-200">{ev.text}</span>
        </div>
      );
    case 'tool_result': {
      const isErr = Boolean((ev.meta as { is_error?: boolean }).is_error);
      const snippet = (ev.text ?? '').replace(/\n/g, ' ').slice(0, 220);
      return (
        <div className={isErr ? 'text-rose-300' : 'text-zinc-500'}>
          <span className={cls}>[{ev.source}]</span> result: {isErr && 'ERR '}{snippet}
        </div>
      );
    }
    case 'review_verdict': {
      const issues = (ev.meta as { issues?: unknown[] }).issues ?? [];
      const pass = ev.text === 'pass';
      return (
        <div className="mt-2 rounded border border-zinc-800 bg-zinc-900/60 p-2">
          <div className={pass ? 'text-emerald-300' : 'text-rose-300'}>
            VERDICT: {ev.text} <span className="text-zinc-500">(issues={issues.length})</span>
          </div>
          {Array.isArray(issues) && issues.slice(0, 5).map((iss, k) => {
            const i = iss as { tool?: string; summary?: string };
            return (
              <div key={k} className="text-xs text-zinc-400 ml-3">
                · [{i.tool ?? '?'}] {i.summary}
              </div>
            );
          })}
        </div>
      );
    }
    case 'deploy_url':
      return (
        <div className={`${cls} font-semibold`}>
          ▸ LIVE URL:{' '}
          <a href={ev.text ?? '#'} target="_blank" rel="noreferrer" className="underline">
            {ev.text}
          </a>
        </div>
      );
    case 'pipeline_start':
      return <div className={cls}>{ev.text}</div>;
    case 'pipeline_end': {
      const cost = (ev.meta as { total_cost_usd?: number }).total_cost_usd;
      return (
        <div className={`${cls} mt-3 font-semibold`}>
          ◆ pipeline finished: {ev.text}
          {cost != null && <span className="text-zinc-500"> · ${cost.toFixed(4)}</span>}
        </div>
      );
    }
    case 'system':
      return <div className="text-zinc-600 text-xs">[system] {ev.text}</div>;
    case 'agent_text':
    default:
      return (
        <div>
          <span className={cls}>[{ev.source}]</span>{' '}
          <span className="text-zinc-200 whitespace-pre-wrap">{ev.text}</span>
        </div>
      );
  }
}
