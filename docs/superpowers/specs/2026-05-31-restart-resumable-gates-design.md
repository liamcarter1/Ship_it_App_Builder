# Design: Restart-resumable, DB-backed gates

**Date:** 2026-05-31
**Status:** Approved (brainstorming) — pending implementation plan
**Area:** `backend/` — the "remaining hardening" item from `CLAUDE.md` / `SHIP-IT_BUILD_PLAN.md` §10.

## Problem

`GateBroker` (`backend/app/gates.py`) keeps pending gates as `asyncio.Future`s
in process memory. When the orchestrator reaches a gate it blocks on
`await asyncio.wait_for(future, ...)`. This has two failure modes:

1. **Restart loses the gate.** If the FastAPI process restarts while a run is
   paused at a gate, the orchestrator's asyncio task dies, the future is gone,
   and the run is stranded at `status='running'` in the DB forever.
2. **Single-worker only.** With more than one uvicorn worker, a gate opened in
   worker A's memory is invisible to worker B, so a `POST .../gate/{name}` that
   lands on the wrong worker 404s.

Both share one root cause: gate state — and the run state needed to continue
past the gate — lives only in RAM.

## Scope decisions (from brainstorming)

- **Recovery scope: resume only the cheap tail.** A run paused at the **code**
  or **deploy** gate (where the expensive Coder⇄Reviewer work is already
  durable on disk) is resumed after a restart. A run paused at the early
  **spec** gate (nothing expensive done yet) is marked `interrupted` and not
  auto-resumed — starting fresh is cheap.
- **Wait mechanism: pure DB-poll.** The orchestrator polls a `gates` table for
  a decision instead of awaiting an in-process future. This mirrors the
  existing SSE design (which polls the `events` table), and makes both
  restart-durability and multi-worker correctness fall out of one mechanism.
  Cost: up to ~250 ms wake-up latency on approval (imperceptible to a human).

## Architecture

Gate state moves into a new `gates` table in the existing SQLite DB. Resolving
a gate is a DB write; waiting on one is a poll loop. The decision, the run row,
and the workspace are all durable across a restart — only the in-flight Python
task is lost, and for the code/deploy gates everything that task needs to
finish is reconstructable from the DB + disk.

### 1. Schema (`store.py`)

```sql
CREATE TABLE IF NOT EXISTS gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    name   TEXT NOT NULL,            -- 'spec' | 'code' | 'deploy'
    status TEXT NOT NULL,            -- 'open' | 'approved' | 'rejected'
    notes  TEXT,
    payload TEXT,                    -- JSON gate payload; render still comes from the gate_open event
    opened_at  REAL NOT NULL,
    decided_at REAL,
    UNIQUE(run_id, name)             -- each gate fires once per run
);
```

Plus a new **`config TEXT`** column on `runs` (JSON: `deploy`,
`max_review_rounds`, and the five `*_model` overrides). This is the piece that
makes tail-resume possible — without it a resumed run wouldn't know whether to
deploy or with which model. Added via an idempotent
`ALTER TABLE runs ADD COLUMN config TEXT` migration in `connect()` so existing
`shipit.db` files keep working.

New `Store` methods (all SQL stays in `store.py`):

- `open_gate(run_id, name, payload)` — upsert a row to `status='open'`;
  idempotent (re-opening an already-open row leaves it open — resume relies on
  this).
- `get_gate(run_id, name)` → row or `None`.
- `resolve_gate(run_id, name, *, approve, notes)` → `bool` (True if a row was
  `open` and got decided; False if missing/already decided).
- `cancel_open_gates(run_id, *, notes)` → list of names flipped to `rejected`.
- `pending_gates(run_id)` → list of names with `status='open'`.
- `running_runs()` → rows with `status='running'`.
- `claim_run_for_resume(run_id)` → atomic
  `UPDATE runs SET status='resuming' WHERE id=? AND status='running'`; returns
  True only for the caller that won the flip (guards against double-resume).
- `create_run(...)` / `get_run(...)` gain config write/read.

### 2. `GateBroker` rewrite (`gates.py`)

`GateDecision` is unchanged. `GateBroker` wraps a `Store` instead of holding
futures:

- `open(run_id, name, payload)` → `store.open_gate(...)`.
- `async wait(run_id, name, *, timeout, poll_interval=0.25)` → poll loop:
  `get_gate`; return `GateDecision` when `status != 'open'`; `asyncio.sleep`
  otherwise; raise `asyncio.TimeoutError` once `timeout` elapses.
- `resolve(run_id, name, decision)` → `store.resolve_gate(...)`.
- `cancel_all(run_id, *, notes)` → `store.cancel_open_gates(...)`.
- `pending_for(run_id)` → `store.pending_gates(...)`.

### 3. Orchestrator (`orchestrator.py`)

`_gate()` now: `broker.open(...)` (DB write) → emit `gate_open` → `broker.wait(...)`.
Ordering preserved (open row before event) so a fast POST always finds the row.
On `TimeoutError` the existing `expired_at_<name>` behaviour is kept.

The post-green-build logic (code gate → optional deploy gate → deployer →
final status) is extracted into `_finish_after_code_gate(outcome, workspace, verdict)`.
Two callers:

- **`run()`** — unchanged flow, now delegates the tail to the extracted method.
- **`resume_tail(run_id)`** (new) — loads the run row (idea, workspace,
  `config`), rebuilds a minimal `outcome` and an `OrchestratorConfig` (with the
  shared broker + gated stages), re-attaches the store listener, emits
  `pipeline_resumed`, then re-enters at the gate it died on:
  - died at **code** gate → re-run `_finish_after_code_gate` (the open `code`
    row means `wait()` continues where it left off);
  - died at **deploy** gate → await the deploy gate then run the deployer.
  Timeout clock resets on resume (the human just came back).

`create_run` calls thread `config` through so it's persisted on both the API
and CLI paths.

### 4. Restart recovery sweep (`server.py` lifespan)

On startup, before serving:

1. For each `running_runs()` row, `claim_run_for_resume(run_id)` (atomic flip
   to `resuming`; skip if not won).
2. Inspect the claimed run's open gate:
   - open gate is **`code`** or **`deploy`** → launch `resume_tail(run_id)` into
     `_active_runs`.
   - open gate is **`spec`**, or no open gate (died mid-stage) → mark run
     `interrupted`, `cancel_open_gates(notes="server restarted")`, emit
     `run_interrupted`.

`_gate_broker = GateBroker(_store)`. `post_gate`, `cancel_run`, `list_gates`
keep their signatures — they hit the DB through the broker now. `post_gate`'s
"no pending gate" 404 now derives from `resolve_gate` returning False.

### 5. Events (`events.py`)

Add `pipeline_resumed` and `run_interrupted` to `EventKind`. They make recovery
visible in the event log and dashboard replay. No frontend change is strictly
required: the gate panel already re-renders from a replayed `gate_open` that has
no matching `gate_decision`, so a reconnecting dashboard shows the pending gate
again automatically.

## Data flow (resume, paused at code gate)

```
[old process] run() → green build → open_gate('code') → emit gate_open → wait()  ✗ process killed
                                          │
                              gates row: code/open  (durable)
                                          │
[new process] startup sweep → claim_run_for_resume → open gate is 'code'
                            → resume_tail(run_id) → _finish_after_code_gate
                            → wait() polls gates row → human approves (DB write)
                            → deploy gate / built → finish_run
```

## Testing (new `backend/tests/`)

Add `pytest` + `pytest-asyncio` as dev deps. Fast, SDK-free tests against a temp
DB:

- gate round-trip: `open` → `pending_for` → `resolve` → `wait` returns the
  decision;
- `wait` times out → `asyncio.TimeoutError`;
- restart sweep: `running` + open `code` gate → resumed; + open `spec` gate →
  `interrupted`; + no open gate → `interrupted`;
- `claim_run_for_resume` is atomic — a second claim returns False.

The `resume_tail` deploy/build path is exercised with the deployer stubbed (no
live Vercel / SDK call).

## Decisions / non-goals

- **Config storage:** one `config` JSON column on `runs` (vs. separate columns)
  — fewer migrations, future-proof.
- **Timeout resets on resume** rather than honoring the original `opened_at`.
- **CLI unchanged:** `gate_broker=None` still no-ops gates; `config` is
  persisted but harmless.
- **Non-goal:** resuming a run that died mid-stage (e.g. while the Coder query
  was in flight). Those are marked `interrupted`; the in-flight agent call
  cannot be resumed.
