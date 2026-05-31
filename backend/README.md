# Ship-It backend

A multi-agent pipeline that takes a one-line product idea and produces a
green-build Next.js + TypeScript + Tailwind app, optionally deployed to
Vercel. CLI-driven; the Next.js dashboard arrives in Milestone 2.

## Pipeline

```
Planner -> Scaffolder -> Coder -> Reviewer ↺ ... -> Deployer (optional)
                                    ↑      │
                                    └──────┘  (green-build loop, capped)
```

- **Planner** (think-only) — idea → JSON spec.
- **Scaffolder** (Bash/Write/Edit) — fresh Next.js 14 + TS + Tailwind workspace,
  `npm install`, initial git commit.
- **Coder** (Bash/Write/Edit) — implements the spec into `app/page.tsx`
  (and `components/*` as needed).
- **Reviewer** (Bash/Read, **read-only**) — runs `npm run lint`, `tsc
  --noEmit`, `npm run build`; returns a JSON verdict with structured issues.
- **Coder revision** — when the Reviewer fails, the Coder gets the issue list
  back as a new brief and patches. Capped (default 3 rounds).
- **Deployer** (Bash) — runs `npx vercel deploy --prod` and returns the URL.
  Only invoked with `--deploy`.

The state machine is owned by Python (`app/orchestrator.py`), not by a model.
Each stage opens its own `query()` with the role as the **main agent**
(`system_prompt`), keeping context isolated and gating trivial to add later.

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # edit if you want to set ANTHROPIC_API_KEY / VERCEL_TOKEN
```

Auth: either an `ANTHROPIC_API_KEY` or a logged-in `claude` CLI session.

## Run it

```bash
# Build a green app for the default idea
python -m app.run_spike

# Custom idea
python -m app.run_spike "a landing page for a yoga studio"

# Build AND deploy to Vercel (needs VERCEL_TOKEN)
python -m app.run_spike --deploy "an indie note-taking app"

# Cheaper roles
python -m app.run_spike --planner-model claude-haiku-4-5-20251001 \
                       --reviewer-model claude-haiku-4-5-20251001 \
                       "a coffee subscription landing page"

# Replay or inspect past runs (no model calls)
python -m app.run_spike --list-runs
python -m app.run_spike --show-run 3
```

The transcript-independent pass/fail signal is:

1. workspace contains `app/page.tsx`
2. Reviewer's last verdict is `pass`
3. with `--deploy`, the Deployer returned a `vercel.app` URL

## Persistence

Every run writes to `backend/shipit.db` (SQLite):

- `runs(id, idea, status, workspace, started_at, finished_at, total_cost_usd,
  deploy_url, error)`
- `events(id, run_id, ts, kind, source, text, meta)` — every PipelineEvent
  the bus saw, including agent text, tool calls, tool results, verdicts.

The same `EventBus` will drive the SSE stream to the Milestone 2 dashboard;
the printer and the recorder are just two listeners.

## Workspaces

Each run gets its own throwaway workspace at
`<repo>/workspaces/run-<ts>-<id>/`. The directory is gitignored — only the
SQLite event log persists across runs by default.

## Layout

```
backend/
├── app/
│   ├── __init__.py
│   ├── events.py            # PipelineEvent + EventBus (pub/sub)
│   ├── store.py             # SQLite Store: runs + events
│   ├── orchestrator.py      # Python state machine driving each stage
│   ├── run_spike.py         # CLI entrypoint
│   └── agents/
│       ├── planner.py       # idea -> JSON spec  (think-only)
│       ├── scaffolder.py    # fresh Next.js + TS + Tailwind project
│       ├── coder.py         # spec -> page.tsx; handles revision passes
│       ├── reviewer.py      # lint+tsc+build, structured verdict
│       └── deployer.py      # vercel deploy --prod, parses URL
├── requirements.txt
├── .env.example
└── README.md (this file)
```

## Dashboard server (Milestone 2)

A small FastAPI app at `app/server.py` exposes the same pipeline over HTTP
so the Next.js dashboard at `../frontend/` can drive it:

| Route | Purpose |
|---|---|
| `GET  /api/runs`                          | list runs, newest first |
| `GET  /api/runs/{id}`                     | one run's metadata (+ `live: bool`) |
| `GET  /api/runs/{id}/events`              | all events for that run (one-shot JSON) |
| `GET  /api/runs/{id}/events/stream`       | SSE: replay history then live tail |
| `POST /api/runs`                          | start a new pipeline run |
| `GET  /api/runs/{id}/gates`               | names of gates currently awaiting a decision (M3) |
| `POST /api/runs/{id}/gate/{name}`         | approve/reject a gate with optional notes (M3) |
| `POST /api/runs/{id}/cancel`              | reject every pending gate → abort the run cleanly (M3) |
| `GET  /api/healthz`                       | liveness probe |

Run it:

```bash
uvicorn app.server:app --reload --port 8000
```

**Windows:** use the launcher instead —

```bash
python run_server.py --port 8000
```

The Claude Agent SDK spawns the `claude` CLI as a child process, and
asyncio can only do that on a ProactorEventLoop. `run_server.py` sets the
Proactor policy *before* uvicorn creates its loop; the bare `uvicorn`
command can land on a SelectorEventLoop where subprocess spawning fails
with "CLIConnectionError: Failed to start Claude Code:". (On macOS/Linux
the launcher is just a thin wrapper, so it's safe everywhere.)

The SSE handler polls SQLite for new rows past the client's last id
(default ~250 ms). This means it tails any run regardless of which
process started it — runs from `POST /api/runs` and runs from `python -m
app.run_spike` both appear, because both write to the same `shipit.db`.

CORS is configured for `http://localhost:3000` by default; override with
the `CORS_ORIGINS` env var (comma-separated). The Next.js dev config
rewrites `/api/*` to this server, so you usually don't need CORS at all
during local development.

## Approval gates (Milestone 3)

Three gates fire when running via the API: **spec** (after the Planner,
before any code is written), **code** (after the green build), and
**deploy** (only when the run was started with `deploy=true`). Each gate:

1. The orchestrator emits a `gate_open` event with the relevant payload
   (the JSON spec for spec; the workspace + verdict for code; the
   workspace for deploy) and awaits a future from the in-process
   `GateBroker`.
2. The dashboard sees `gate_open` on its SSE stream and renders a
   `GatePanel` with the payload and a notes textarea.
3. You click Approve or Reject. The dashboard `POST`s to
   `/api/runs/{id}/gate/{name}` which resolves the future.
4. The orchestrator emits `gate_decision` and either continues (approve)
   or aborts the run with `status=rejected_at_<name>` (reject).

Spec-gate notes are passed verbatim into the Coder's first brief as a
hard requirement, so "use a muted palette" or "no emoji" actually
constrains the implementation.

The **CLI keeps M1/M2 behaviour**: `OrchestratorConfig.gate_broker` is
None by default, which makes `_gate()` a silent no-op. If you want
human-in-the-loop runs, start them from the dashboard (or hit
`POST /api/runs` directly).

A gate that goes unanswered for `OrchestratorConfig.gate_timeout_s`
(default 1 h) ends the run with `status=expired_at_<name>` and emits a
`gate_decision` event so the dashboard clears the panel — abandoned
tabs can't leak orchestrator tasks.

Restart caveat: the broker lives only in process memory. If the FastAPI
server restarts while a gate is pending, the asyncio task dies with it
and the run stays as `running` in the DB forever. Restart-resumable
gates are an M4 concern. **Multi-worker caveat:** the broker is a
per-process singleton, so a gate opened in one uvicorn worker is
invisible to a resolve POST landing in another. Run with a single
worker (`--workers 1`, the default) until M4 swaps the broker for a
shared backing store.

## What's next (Milestone 4)

Polish: a diff viewer in the code-gate panel (`git diff` against the
scaffolder commit), restart-resumable gates, per-worker model selection
from the dashboard, and a cost-tracking overlay.
