'use client';

import { useEffect, useRef, useState } from 'react';
import { streamUrl } from '@/lib/api';
import type { EventKind, EventSource, GateName, PipelineEventDTO } from '@/lib/types';
import { GatePanel } from './GatePanel';

interface OpenGate {
  name: GateName;
  payload: Record<string, unknown>;
  ts: number;
}

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
  /** Ground-truth liveness signal from `GET /api/runs/{id}.live`, which
   *  mirrors the backend's `_active_runs` registry — true iff the
   *  orchestrator asyncio task is currently alive in the server process.
   *  This is what keys the pulse animation and the elapsed counter;
   *  *not* the SSE connection state, which can be open against a dead task. */
  liveOnBackend: boolean;
  /** `run.status` from the DB. Terminal values (anything other than
   *  `'running'`) freeze the indicator even if SSE somehow remains open. */
  runStatus: string;
}

export function ActivityStream({ runId, alreadyFinished, liveOnBackend, runStatus }: Props) {
  const [events, setEvents] = useState<PipelineEventDTO[]>([]);
  const [connected, setConnected] = useState(false);
  const [streamEnded, setStreamEnded] = useState(alreadyFinished);
  const [error, setError] = useState<string | null>(null);
  // Gates currently awaiting a human decision. Keyed by gate name (spec /
  // code / deploy) since at most one of each can be open at any time per run.
  const [openGates, setOpenGates] = useState<Record<string, OpenGate>>({});
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
      'gate_open', 'gate_decision',
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

        // Drive the open-gate set off the event log so it's correct both
        // during live tail AND during full replay (a decided gate's
        // `gate_open` is followed by `gate_decision`, so the gate ends up
        // closed regardless of order).
        if (parsed.kind === 'gate_open') {
          const meta = (parsed.meta ?? {}) as { name?: GateName; payload?: Record<string, unknown> };
          if (meta.name) {
            setOpenGates((g) => ({
              ...g,
              [meta.name as string]: { name: meta.name as GateName, payload: meta.payload ?? {}, ts: parsed.ts },
            }));
          }
        } else if (parsed.kind === 'gate_decision') {
          const meta = (parsed.meta ?? {}) as { name?: string };
          if (meta.name) {
            setOpenGates((g) => {
              const next = { ...g };
              delete next[meta.name as string];
              return next;
            });
          }
        }

        if (parsed.kind === 'pipeline_end') {
          // Clear any still-open gate panels: the pipeline has finished,
          // so any pending decision is moot. (E.g. it was cancelled.)
          setOpenGates({});
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

  // GROUND TRUTH for "is this run genuinely alive right now?". All three
  // must hold: the backend's _active_runs registry confirms the orchestrator
  // task exists, the DB hasn't recorded a terminal status, and we haven't
  // seen the terminal event on the wire. Any one flipping false freezes
  // every indicator — the pulse, the elapsed counter, and the footer dot.
  // This is the line that makes the UI tell the truth instead of just
  // spinning whenever the SSE connection is open.
  const isGenuinelyAlive = liveOnBackend && !streamEnded && runStatus === 'running';

  // Tick a `now` clock once a second so the elapsed-since-last-event counter
  // updates smoothly. Gated on isGenuinelyAlive — when the run ends, we stop
  // updating `now`, which freezes the displayed counter at its final value
  // instead of letting it climb indefinitely after the work has stopped.
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!isGenuinelyAlive) return;
    const handle = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(handle);
  }, [isGenuinelyAlive]);

  // Derive the currently-running stage from the event log: walk backwards
  // until we find the most recent stage_start or stage_end. A stage_start
  // with no matching stage_end means that stage is still in flight.
  let currentStage: string | null = null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === 'stage_start') { currentStage = e.source; break; }
    if (e.kind === 'stage_end')   { currentStage = null;     break; }
  }

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const secondsSinceLastEvent =
    lastEvent != null && lastEvent.ts > 0 ? Math.max(0, Math.floor(now - lastEvent.ts)) : null;

  // Watchdog status for the current stage: did the server-side watchdog
  // emit a stage_stalled that hasn't yet been followed by a stage_retry?
  // If so, we know with certainty that this stage is stuck (the backend
  // told us). This is stronger than "Ns since last event" because the
  // watchdog actually killed and intends to restart a hung subprocess.
  let watchdogState: 'stalled' | 'retrying' | null = null;
  if (currentStage) {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.source !== currentStage) continue;
      if (e.kind === 'stage_stalled') { watchdogState = 'stalled'; break; }
      if (e.kind === 'stage_retry')   { watchdogState = 'retrying'; break; }
      if (e.kind === 'stage_start')   { break; } // current stage started fresh
    }
  }

  const openGateList = Object.values(openGates).sort((a, b) => a.ts - b.ts);
  const showQuietHint =
    isGenuinelyAlive && currentStage != null && secondsSinceLastEvent != null && secondsSinceLastEvent >= 30;

  return (
    <div className="space-y-3">
      {openGateList.map((g) => (
        <GatePanel
          key={g.name}
          runId={runId}
          name={g.name}
          payload={g.payload}
          onResolved={() => {
            // Optimistic close; the gate_decision SSE event will also
            // clear it, so this is just for snappiness.
            setOpenGates((prev) => {
              const next = { ...prev };
              delete next[g.name];
              return next;
            });
          }}
        />
      ))}
      <div className="rounded border border-zinc-800 bg-zinc-950">
        <div className="flex items-center justify-between gap-3 border-b border-zinc-800 px-3 py-2 text-xs">
          <div className="text-zinc-400">
            {events.length} event{events.length === 1 ? '' : 's'}
          </div>
          <div className="flex items-center gap-2 text-zinc-400 min-w-0">
            {streamEnded || runStatus !== 'running' ? (
              // Terminal: DB says the run is over, OR we saw the end event.
              // No pulse — the work has definitively stopped.
              <span className="text-zinc-500">
                stream ended{runStatus !== 'running' ? ` · ${runStatus}` : ''}
              </span>
            ) : !liveOnBackend ? (
              // Backend says the orchestrator task is gone but the run row
              // hasn't been finalised yet AND we haven't seen pipeline_end
              // on the wire. This is a transient race (task exiting,
              // finally block still running) — be honest about it.
              <>
                <span className="inline-block h-2 w-2 rounded-full bg-zinc-500" />
                <span className="text-zinc-400">backend task ended — finalising…</span>
              </>
            ) : !connected ? (
              // Backend says alive, but SSE has dropped. The run IS still
              // running on the server; we just can't see it right now.
              <>
                <span className="inline-block h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
                <span className="text-amber-400">reconnecting…</span>
                <span className="text-zinc-500">· backend confirms still alive</span>
              </>
            ) : watchdogState === 'stalled' ? (
              // Server-side watchdog has positively detected a stall on
              // the current stage. Truth-grade signal that "stuck" is real.
              <>
                <span className="inline-block h-2 w-2 rounded-full bg-amber-500 animate-pulse" />
                <span className="text-amber-400 truncate">watchdog: {currentStage} stalled</span>
                {secondsSinceLastEvent != null && (
                  <span className="text-zinc-500 tabular-nums">· {formatElapsed(secondsSinceLastEvent)}</span>
                )}
              </>
            ) : watchdogState === 'retrying' ? (
              <>
                <span className="inline-block h-2 w-2 rounded-full bg-cyan-400 animate-pulse" />
                <span className="text-cyan-400 truncate">watchdog: retrying {currentStage}</span>
                {secondsSinceLastEvent != null && (
                  <span className="text-zinc-500 tabular-nums">· {formatElapsed(secondsSinceLastEvent)}</span>
                )}
              </>
            ) : currentStage ? (
              <>
                <span className="inline-block h-2 w-2 rounded-full bg-cyan-400 animate-pulse" />
                <span className={`${sourceClasses(currentStage)} truncate`}>
                  running {currentStage}
                </span>
                {secondsSinceLastEvent != null && (
                  <span className="text-zinc-500 tabular-nums">· {formatElapsed(secondsSinceLastEvent)}</span>
                )}
              </>
            ) : (
              // Backend alive, SSE open, no current stage (between stages).
              <>
                <span className="inline-block h-2 w-2 rounded-full bg-cyan-400 animate-pulse" />
                <span className="text-cyan-400">live</span>
              </>
            )}
          </div>
        </div>
        {showQuietHint && watchdogState == null && (
          <div className="border-b border-zinc-800 bg-zinc-900/40 px-3 py-1.5 text-[11px] text-zinc-500 italic">
            Quiet stretches are normal — the Scaffolder waits on <code className="text-zinc-400">npm install</code>{' '}
            (often 60-120s) and the Coder thinks before writing. If something&apos;s genuinely stuck the
            server-side watchdog will fire a <code className="text-zinc-400">stage_stalled</code> event.
          </div>
        )}
        <div className="max-h-[60vh] overflow-y-auto p-3 text-sm leading-relaxed">
          {events.length === 0 ? (
            <p className="text-zinc-600 italic">Waiting for the orchestrator to emit its first event…</p>
          ) : (
            events.map((ev, i) => <EventRow key={ev.id ?? `${ev.ts}-${i}`} ev={ev} />)
          )}
          {isGenuinelyAlive && events.length > 0 && (
            // A subtle "still working" line at the bottom of the stream so
            // the eye has something to settle on during quiet stretches.
            // Animates ONLY while genuinely alive — same truth signal as
            // the header pulse, so the two never disagree.
            <div className="mt-2 flex items-center gap-2 text-xs text-zinc-600">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-zinc-500 animate-pulse" />
              <span>
                {currentStage ? `${currentStage} working…` : 'waiting for next stage…'}
              </span>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        {error && <p className="px-3 py-2 text-xs text-rose-300">SSE error: {error}</p>}
      </div>
    </div>
  );
}

function formatElapsed(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return rest === 0 ? `${m}m` : `${m}m ${rest}s`;
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
    case 'gate_open':
      return (
        <div className="text-amber-300 text-xs uppercase tracking-wide mt-2">
          ⏸ gate opened: {ev.text}
        </div>
      );
    case 'gate_decision': {
      const meta = ev.meta as { approve?: boolean; notes?: string | null };
      const approved = Boolean(meta.approve);
      return (
        <div className={approved ? 'text-emerald-300' : 'text-rose-300'}>
          {approved ? '✓' : '✗'} gate decision: {ev.text} ({approved ? 'approved' : 'rejected'})
          {meta.notes && <span className="text-zinc-400"> — notes: {meta.notes}</span>}
        </div>
      );
    }
    case 'stage_stalled': {
      // Server-side watchdog detected an idle/total-time breach on this
      // stage. Truth-grade evidence that something genuinely got stuck —
      // surface it loudly. Meta carries idle_s and timeout_s when present.
      const meta = ev.meta as { idle_s?: number; timeout_s?: number; reason?: string };
      return (
        <div className="mt-2 rounded border border-amber-700/60 bg-amber-950/30 px-2 py-1 text-amber-200 text-xs">
          <span className="font-semibold">⚠ watchdog: {ev.source} stalled</span>
          {meta.idle_s != null && (
            <span className="text-amber-300/70"> · idle {formatElapsed(Math.floor(meta.idle_s))}</span>
          )}
          {meta.timeout_s != null && (
            <span className="text-zinc-500"> (timeout {formatElapsed(Math.floor(meta.timeout_s))})</span>
          )}
          {meta.reason && <span className="text-zinc-400"> — {meta.reason}</span>}
          {ev.text && !meta.reason && <span className="text-zinc-400"> — {ev.text}</span>}
        </div>
      );
    }
    case 'stage_retry': {
      // Watchdog killed the hung subprocess and is starting the stage
      // fresh. Distinct cyan colour so it reads as "recovery action," not
      // "another failure on top of the stall."
      const meta = ev.meta as { attempt?: number };
      return (
        <div className="mt-1 rounded border border-cyan-800/60 bg-cyan-950/20 px-2 py-1 text-cyan-200 text-xs">
          <span className="font-semibold">↻ watchdog: retrying {ev.source}</span>
          {meta.attempt != null && <span className="text-cyan-300/70"> · attempt {meta.attempt}</span>}
          {ev.text && <span className="text-zinc-400"> — {ev.text}</span>}
        </div>
      );
    }
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
