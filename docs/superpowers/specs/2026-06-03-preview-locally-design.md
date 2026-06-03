# Design: "Preview locally" button

**Date:** 2026-06-03
**Status:** Approved (brainstorming) — pending implementation plan
**Area:** `backend/app/` (new `preview.py`, wired into `server.py`),
`frontend/` (run detail page + new `PreviewPanel`). New feature, post-hardening.

## Problem

When a run finishes, you can only *see the finished product* easily if it was
deployed — the run detail page renders `deploy_url` as a clickable link
(`frontend/app/runs/[id]/page.tsx:99-108`). For a run that only reached
`built` (the common case — e.g. run #9, a complete Next.js app on disk), the
dashboard shows the workspace path as **plain text**
(`runs/[id]/page.tsx:113`). To view the app you must drop to a terminal, `cd`
into the workspace, and run `npm run dev` yourself, picking a non-colliding
port. That is the UX gap this feature closes: a one-click way to launch a
completed run's app and open it in the browser, with a matching way to stop it.

The workspace is already a complete, standalone Next.js project with
`node_modules` installed and a green build (it passed the Reviewer), so it is
immediately runnable — no install or build step is required to preview it.

## Scope decisions (from brainstorming)

- **Previewable runs: `built` or `deployed` only.** The button is offered only
  for runs whose status guarantees the app passed review. Previewing a
  partially-built `errored`/`interrupted` workspace is out of scope.
- **One preview at a time.** At most one preview process runs across the whole
  server. Starting a preview for a different run transparently stops the
  current one first. Simplest mental model for a single-user local tool and
  bounds resource use.
- **Idle auto-stop.** A preview stops itself after a fixed idle window
  (default **30 minutes**). `last_active` is bumped on start and on every
  preview status poll, so an open run-detail tab keeps it warm and **closing
  the tab lets it idle out** — no manual cleanup needed in the common case.
- **PID-tracked, swept on restart.** The active preview's PID/port is persisted
  so a backend restart can kill the orphaned `node`/`npm` tree on next startup.
  Mirrors the project's M5 restart-durability ethos (`_recover_runs`).
- **`npm run dev`, not production build.** `node_modules` is already present and
  dev mode needs no separate build step. A production `build`+`start` preview
  mode is explicitly deferred (YAGNI).

## Architecture

A new module **`backend/app/preview.py`** exposing a single `PreviewManager`,
instantiated once in `server.py` next to `_store` and `_gate_broker`. It owns
*at most one* preview and is the single source of truth for preview state.

```
PreviewInfo (dataclass / dict): { run_id, pid, port, url, started_at, last_active }

PreviewManager(store)
  async start(run_id, workspace) -> PreviewInfo   # stops any existing preview first
  async stop() -> bool                            # True if something was stopped
  status() -> PreviewInfo | None                  # the one active preview, if any
  touch() -> None                                 # bump last_active
  async recover() -> None                         # startup sweep: kill orphan PID, clear record
  # internal: _idle_reaper() background asyncio task
```

State is held both **in memory** (the live `PreviewInfo` + the `Popen` handle)
and **persisted** (see Persistence) so the in-memory copy survives normal
operation and the persisted copy survives a restart.

### Shared tree-kill helper

`_kill_process_tree(pid)` already exists in `orchestrator.py:122` (psutil walks
the `npm`→`node` tree; on Windows the SDK's `TerminateProcess` would orphan
grandchildren). Both the orchestrator and the preview manager need it, so it
moves into a small shared module — **`backend/app/proc.py`** — and both import
it. `orchestrator.py` keeps its current behaviour via the import; no logic
change there beyond the relocation.

## Process mechanics

- **Spawn:** `subprocess.Popen([npm, "run", "dev", "--", "-p", str(port)],
  cwd=workspace, stdout=log, stderr=STDOUT)` where `npm = shutil.which("npm")`
  (resolves `npm.cmd` on Windows). stdout/stderr are redirected to
  `workspace/_preview.log` for debugging.
- **Port selection:** scan the fixed range **4300–4399** and bind-test each
  (`socket.bind(("127.0.0.1", port))`) to find the first free port — avoids the
  dashboard (3000) and backend (8000). If the whole range is occupied, raise a
  clear error.
- **Readiness:** after spawn, TCP-poll the chosen port (`socket.connect_ex`)
  until it accepts, with a ~30s deadline. Also fail fast if the child process
  exits before the port opens (read the tail of `_preview.log` into the error).
  On timeout/early-exit, kill the half-started tree and raise.
- **Stop / replace:** `stop()` calls `_kill_process_tree(pid)`, clears in-memory
  and persisted state. `start()` calls `stop()` first if any preview is active.

## Persistence & restart sweep

A single persisted record holds the active preview (`run_id, pid, port,
started_at`). Implementation: a one-row **`previews` table** in the existing
SQLite store (created idempotently in `Store._ensure_schema`, consistent with
how `runs`/`events`/`gates` are created), with `Store` helpers
`set_active_preview(...)`, `get_active_preview()`, `clear_active_preview()`.
Using the existing store keeps one persistence mechanism rather than adding a
sidecar file.

The FastAPI `lifespan` (`server.py:93`) gains a **`_recover_previews()`** step
alongside `_recover_runs()`: read the persisted record (if any), attempt
`_kill_process_tree(pid)` to reap the orphan from the dead process, then clear
the record. A stale PID that no longer exists is a no-op (psutil handles the
missing-process case, as it already does in the watchdog path).

## Idle auto-stop

`PreviewManager` runs a single background `asyncio` task (`_idle_reaper`) that
wakes periodically (~30s) and stops the preview if
`now - last_active > idle_timeout_s` (default 1800s, configurable via a module
constant / env var `PREVIEW_IDLE_TIMEOUT_S`). `last_active` is bumped by
`start()` and by `touch()`, and `touch()` is called from the `GET .../preview`
handler. The reaper task is started in `lifespan` and cancelled on shutdown.

> Note: this relies on the same monotonic-time source the watchdog uses; the
> `Date.now()`-free constraint is a Workflow-script concern only and does not
> apply to backend runtime code.

## API

All under `/api`, following the existing run-scoped pattern:

```
POST   /api/runs/{id}/preview     start preview for this run -> PreviewInfo
GET    /api/runs/{id}/preview     preview status for this run -> PreviewInfo | {active: false}
DELETE /api/runs/{id}/preview     stop preview for this run -> {stopped: bool}
```

- **POST** validates, in order: run exists (404); status ∈ {`built`,`deployed`}
  (409 with message); `workspace/package.json` and `workspace/node_modules`
  exist (409 telling the user to run `npm install`). If the *same* run already
  has a live preview, it is idempotent — return the existing `PreviewInfo`
  rather than restarting. If a *different* run is previewing, that one is
  stopped first.
- **GET** returns the active preview only if it belongs to `{id}` (so each run's
  panel reflects only its own preview); calls `touch()` to keep it warm.
- **DELETE** stops the preview if it belongs to `{id}`; returns `{stopped:
  false}` if there was nothing to stop.

`PreviewInfo` JSON: `{ run_id, port, url, started_at }` (`pid` stays
server-side; the client only needs the URL).

## Frontend

- **`lib/types.ts`:** add `PreviewDTO` (`{ active: boolean; run_id?: number;
  port?: number; url?: string; started_at?: number }`).
- **`lib/api.ts`:** add `startPreview(runId)`, `getPreview(runId)`,
  `stopPreview(runId)` mirroring the existing typed-fetch helpers.
- **`components/PreviewPanel.tsx`:** rendered on the run detail page only when
  `run.status ∈ {built, deployed}`. States:
  - *idle:* a **Preview locally** button.
  - *starting:* disabled button + spinner ("Starting preview…").
  - *running:* a clickable `http://localhost:<port>` link (opens new tab) +
    a **Stop preview** button.
  - *error:* inline message (npm-not-found, no free port, readiness timeout,
    missing node_modules) with a retry affordance.
  On mount and while running, it polls `getPreview(runId)` (e.g. every 5s) to
  reflect externally-changed state and to keep the idle timer warm.
- Wire `PreviewPanel` into `runs/[id]/page.tsx` near the existing
  cost/deploy-url block.

## Error handling

| Condition | Result |
|---|---|
| `npm` not on PATH | 500/clear message "npm not found" surfaced in panel |
| No free port in 4300–4399 | error "no free preview port available" |
| Child exits before port opens | error with tail of `_preview.log` |
| Readiness timeout (~30s) | kill half-started tree, error |
| Run not `built`/`deployed` | 409, button not shown for these in practice |
| `node_modules` missing | 409 "run npm install in the workspace first" |
| Start while another run previews | stop the old preview, start the new |

## Testing

`backend/tests/test_preview.py`, following the dummy/mocked style of
`test_stall_watchdog.py` (no real npm spawned):

- **Port selection** picks the first free port in range; errors when full.
- **start/stop** lifecycle: `start` records state + persists; `stop` kills the
  tree (assert `_kill_process_tree` called) + clears state.
- **One-at-a-time:** starting for run B while run A previews stops A first.
- **Idempotent start:** starting for the same run returns existing info, no
  second spawn.
- **Idle reaper** stops a preview whose `last_active` is older than the timeout.
- **Restart sweep:** `recover()` kills the persisted orphan PID and clears the
  record; tolerates a missing PID.

Endpoint tests (httpx/ASGI, as in `test_server_recovery.py`):

- 404 for unknown run; 409 for non-built run and for missing `node_modules`;
  happy-path POST→GET→DELETE with `PreviewManager.start/stop` mocked.

`subprocess.Popen`, the readiness poll, and `_kill_process_tree` are mocked so
tests are fast and hermetic.

## Out of scope (YAGNI)

- Production `build`+`start` preview mode.
- Multiple concurrent previews.
- Auto-running `npm install` for stale workspaces.
- Previewing arbitrary (non-built/deployed) statuses.
- Exposing the preview beyond localhost / any tunnelling.

## Files touched

- **new** `backend/app/preview.py` — `PreviewManager`, `PreviewInfo`.
- **new** `backend/app/proc.py` — relocated `_kill_process_tree`.
- `backend/app/orchestrator.py` — import `_kill_process_tree` from `proc`.
- `backend/app/store.py` — `previews` table + accessor helpers.
- `backend/app/server.py` — instantiate `PreviewManager`, 3 endpoints,
  `_recover_previews()` + reaper lifecycle in `lifespan`.
- **new** `backend/tests/test_preview.py`.
- `frontend/lib/types.ts`, `frontend/lib/api.ts` — `PreviewDTO` + helpers.
- **new** `frontend/components/PreviewPanel.tsx`.
- `frontend/app/runs/[id]/page.tsx` — render `PreviewPanel`.
- Docs: update `CLAUDE.md` status + `SHIP-IT_BUILD_PLAN.md` on completion.
