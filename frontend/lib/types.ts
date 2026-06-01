// Mirrors backend/app/events.py:PipelineEvent.EventKind exactly. If you add
// a new kind on the Python side, add it here too (and update sourceColour
// in components/ActivityStream.tsx if it needs styling).
export type EventKind =
  | 'pipeline_start'
  | 'stage_start'
  | 'stage_end'
  | 'agent_text'
  | 'tool_use'
  | 'tool_result'
  | 'system'
  | 'review_verdict'
  | 'deploy_url'
  | 'gate_open'
  | 'gate_decision'
  | 'pipeline_end'
  | 'stage_stalled'
  | 'stage_retry'
  | 'stream_end'; // synthetic, emitted by the SSE handler when the run is over

export type GateName = 'spec' | 'code' | 'deploy';

export type EventSource =
  | 'orchestrator'
  | 'planner'
  | 'scaffolder'
  | 'coder'
  | 'reviewer'
  | 'deployer'
  | 'tool'
  | 'system'
  | string; // e.g. 'coder-r2' for revision rounds

export interface PipelineEventDTO {
  id?: number;
  ts: number;
  kind: EventKind;
  source: EventSource;
  text: string | null;
  meta: Record<string, unknown>;
}

export interface RunDTO {
  id: number;
  idea: string;
  status: string;
  workspace: string;
  started_at: number;
  finished_at: number | null;
  total_cost_usd: number | null;
  deploy_url: string | null;
  error: string | null;
  live: boolean;
}
