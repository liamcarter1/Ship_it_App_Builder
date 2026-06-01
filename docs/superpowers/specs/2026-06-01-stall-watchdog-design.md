# Design: In-stage inactivity watchdog with auto-retry

**Date:** 2026-06-01
**Status:** Approved (brainstorming) — pending implementation plan
**Area:** `backend/app/orchestrator.py` (primary), `events.py` — autonomy hardening.

## Problem

When a run reaches a coding stage and the underlying work hangs, the
orchestrator stalls forever and has to be manually rescued. The system is not
truly autonomous: it can stop and wait indefinitely with no forward progress.

An architect review traced the root cause to a single chokepoint. Every stage
runs through `_run_stage` (`orchestrator.py:91`), whose core is:

```python
async for message in query(prompt=prompt, options=options):  # orchestrator.py:104
```

This loop has only a `max_turns` cap (40 for the Coder, 25 for the Reviewer —
`coder.py:72`, `reviewer.py:96`). `max_turns` bounds *how many agent turns* the
SDK takes, **not how long a single turn may hang**. A stall happens *inside* a
turn, where the turn cap cannot see it. Concrete hang modes:

1. **A hung `Bash` command.** The Coder has `Bash` (`coder.py:53`); the Reviewer
   literally runs `npm run build` (`reviewer.py:36`). A wedged build (corrupt
   cache, native-module compile, OOM thrash) or a model that starts a
   watch-mode/dev server never returns a `ToolResultBlock`, so the `async for`
   never advances.
2. **Model "thinking" / network stall.** If the SDK connection to the `claude`
   child process stalls, the iterator simply never yields the next message.
3. **A tool call that never returns a result** — same mechanism.

In every case the stage emits `stage_start`, maybe a few events, then goes
silent forever. The run row stays `status='running'` with no `finished_at`.

**The cruel twist:** the only existing recovery (`_recover_runs` in the FastAPI
`lifespan`, `server.py:160`) fires *only on process restart*. A hung-but-alive
process never crashes, so the server never restarts, so recovery never
triggers. That gap is exactly the "have to rescue it manually" behaviour.

## Scope decisions (from brainstorming)

- **Jumpstart action: auto-retry the stage fresh.** On detecting a stall, kill
  the wedged turn + its child processes and re-run the *same* stage against the
  current workspace files, with a nudge brief. This is the genuinely autonomous
  path (not "open a human gate", not "just fail").
- **Detection layer: in-stage timeout only.** Wrap the `query()` loop in
  `_run_stage` with an idle + total wall-clock deadline. A separate background
  watchdog (scanning all runs for silence) is explicitly **out of scope** for
  v1 — YAGNI; the in-stage guard catches the reported bug with the least risk
  and no double-execution race. Revisit later if event-loop-wedged cases appear.
- **Retry budget: 1, then fail.** One automatic fresh retry. A second stall ends
  the run as `failed_<stage>_timeout`, consistent with how `failed_review`
  already escalates (`orchestrator.py:284`). This honours the project's "always
  cap loops / escalate on cap" safety rail — no infinite credit-burning loop.
- **Applies to all model stages uniformly** (Planner / Scaffolder / Coder /
  Reviewer / Deployer). The hang modes aren't unique to the Coder, and a
  uniform guard is simpler than special-casing.

## Not a violation of the resume non-goal

`CLAUDE.md` and `SHIP-IT_BUILD_PLAN.md` state that a run which dies *mid-LLM
stage* cannot be replayed — in-flight turn streams aren't checkpointed. This
design does **not** contradict that. The watchdog fires *inside the live
process*, before any restart, and never touches `resume_tail` / `_recover_runs`.

The honest framing: we are **not resuming** the stalled turn. We **abandon** the
wedged turn and **re-run the stage fresh** against the workspace files already
on disk. That is a *new* capability ("retry a stalled stage"), distinct from
"resume a paused gate". It is safe only because the Coder/Reviewer read current
files and overwrite them — they are idempotent-ish over a workspace.

## Architecture

### 1. Idle + total deadline inside `_run_stage` (`orchestrator.py:91`)

Replace the bare `async for` with manual iteration so each awaited message is
bounded:

```python
agen = query(prompt=prompt, options=options)
start = loop.time()
try:
    while True:
        try:
            message = await asyncio.wait_for(agen.__anext__(), timeout=idle_timeout_s)
        except StopAsyncIteration:
            break
        # ... existing AssistantMessage / UserMessage / SystemMessage / ResultMessage handling ...
        if loop.time() - start > total_timeout_s:
            raise StageTimeout(stage=stage, kind="total")
finally:
    await agen.aclose()   # tear down SDK session + child claude/npm/Bash process
```

- **Idle** = no new SDK message for `idle_timeout_s`. `asyncio.wait_for` restarts
  the clock on every iteration, so a working stage (which keeps emitting
  `tool_use` / `tool_result` / `agent_text`) never trips; only a genuine hang
  does.
- **Total** = absolute wall-clock backstop per attempt, for the rare
  trickle-forever case.
- On either expiry, raise a new `StageTimeout(PipelineFailure)` exception.
- `agen.aclose()` in `finally` is the teardown path on *every* exit (success,
  timeout, or cancellation).

`_run_stage` gains two parameters — `idle_timeout_s` and `total_timeout_s` —
supplied by the caller per stage.

### 2. `_run_stage_with_retry` wrapper (new)

A thin wrapper that owns the retry/escalation policy:

```python
async def _run_stage_with_retry(self, *, stage, prompt, options, bus,
                                idle_timeout_s, total_timeout_s) -> StageResult:
    try:
        return await _run_stage(stage=stage, prompt=prompt, options=options, bus=bus,
                                idle_timeout_s=idle_timeout_s, total_timeout_s=total_timeout_s)
    except StageTimeout as first:
        await bus.emit(PipelineEvent(kind="stage_stalled", source=stage,
                       meta={"timeout_kind": first.kind, "attempt": 1}))
        await bus.emit(PipelineEvent(kind="stage_retry", source=stage, meta={"attempt": 2}))
        nudged = NUDGE_PREFIX + prompt
        try:
            return await _run_stage(stage=stage, prompt=nudged, options=options, bus=bus,
                                    idle_timeout_s=idle_timeout_s, total_timeout_s=total_timeout_s)
        except StageTimeout:
            raise PipelineFailure(f"failed_{stage}_timeout")
```

`NUDGE_PREFIX` (generic): *"A previous attempt stalled with no progress. Do NOT
start dev servers, watch-mode, or any long-running/interactive command — run
only commands that terminate. Work from the files already in the workspace."*

All existing stage call sites in `run()` / `resume_tail()` / the
Coder⇄Reviewer loop switch from calling `_run_stage` to
`_run_stage_with_retry`. `run()` already maps a `PipelineFailure` to a terminal
status; `failed_<stage>_timeout` flows through that path.

### 3. Config & per-stage tuning (`OrchestratorConfig`)

The crux risk is the Reviewer/Scaffolder: `npm install` / `npm run build` hold
the Bash tool open with **zero SDK messages** for minutes. The idle window for
build-running stages must exceed worst-case build time, or we kill honest work.

New fields on `OrchestratorConfig` (`orchestrator.py:182`), persisted in the
existing `config` JSON column on `runs` — **no schema migration needed** (it is
already a JSON blob, added in Milestone 5):

| Field | Default | Rationale |
|---|---|---|
| `stage_idle_timeout_s` | `180` | Coder/Planner/Deployer: no long silent commands expected |
| `stage_idle_timeout_build_s` | `420` | Scaffolder/Reviewer: covers a slow cold `npm install`/`build` |
| `stage_total_timeout_s` | `900` | Absolute backstop per stage attempt |

`_run_stage_with_retry`'s caller selects the build idle value for the
`scaffolder` and `reviewer` stages, the standard value otherwise. Defaults are
deliberately generous: better to wait ~3 min on a true hang than abort a real
build. The three fields are added to `_config_dict()` (`orchestrator.py:224`)
so a (future) resumed run honours them; they are **not** exposed in the
dashboard `NewRunForm` for v1 (YAGNI).

### 4. New events (`events.py`)

Two additions to the `EventKind` literal (`events.py:17`):

- `stage_stalled` — emitted when an attempt trips the deadline. `meta` carries
  `timeout_kind` ("idle" | "total") and `attempt`. Lets the dashboard show
  "⚠ stalled, retrying…" instead of a frozen stream.
- `stage_retry` — emitted when the fresh retry begins.

Both flow through the existing EventBus → SQLite recorder → SSE pipeline with
no new plumbing. The terminal `failed_<stage>_timeout` surfaces via the normal
`pipeline_end` event. (The frontend `lib/types.ts` event union should gain the
two kinds for type-completeness, but no new panel is required for v1.)

## Correctness: child-process teardown (first-class risk)

`agen.aclose()` must actually kill the `claude` child process **and** any
`npm` / `Bash` it spawned. Otherwise the retry races an orphaned process against
the same workspace (and on Windows, child-process-tree kills need explicit
care). The implementation plan must include a **verification step** that
confirms teardown empirically, with a fallback of an explicit process-tree kill
(e.g. capturing the child PID and terminating its tree) if `aclose()` alone is
insufficient. This is the single most important correctness check in the work.

## Error handling

- `StageTimeout(PipelineFailure)` — new exception, carries `stage` and `kind`
  ("idle" | "total"). Caught by `_run_stage_with_retry`; on second occurrence
  re-raised as `PipelineFailure(f"failed_{stage}_timeout")`.
- `run()`'s existing failure handling maps that to a terminal outcome status and
  a `pipeline_end` event — no new terminal-path code beyond the status string.
- Cancellation (`asyncio.CancelledError`, e.g. run cancel) must still hit the
  `finally: aclose()` so a cancelled run also tears its child down.

## Testing strategy

Unit tests against a **fake async generator** injected in place of the real
`query()` (no SDK/LLM/network calls), using a small `idle_timeout_s` and
controlled timing so tests run fast:

1. **Hang from the start** — generator that never yields → `StageTimeout(kind="idle")`
   raised after `idle_timeout_s`.
2. **Idle clock resets** — generator yields a few messages then hangs →
   confirms the per-message reset, then trips.
3. **Total backstop** — generator that trickles a message just under the idle
   window forever → `StageTimeout(kind="total")` fires at the total deadline.
4. **Retry succeeds** — first attempt times out, second completes → stage
   returns a result; exactly one `stage_stalled` + one `stage_retry` emitted;
   the second prompt carries `NUDGE_PREFIX`.
5. **Retry also stalls** — both attempts time out → run outcome is
   `failed_<stage>_timeout`; `aclose()` asserted called on both generators.
6. **Teardown** — `aclose()` (or the fallback kill) invoked on every exit path
   (success, timeout, cancellation).
7. **Regression** — all 23 existing tests in `backend/tests/` still pass.

## Out of scope (v1)

- Background/out-of-band watchdog scanning all runs (Candidate 2 from the
  review).
- Persisted `runs.last_event_ts` heartbeat column (Candidate 3) — not needed
  for the in-stage approach.
- Exposing timeout knobs in the dashboard UI.
- Resuming a stalled mid-LLM stage across a process restart (remains a
  documented non-goal; this design retries in-process only).
