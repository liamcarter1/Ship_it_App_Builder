# CLAUDE.md — Ship-It

Guidance for Claude (and humans) working in this repository. Read this first.

## What this project is

**Ship-It** is a personal "app factory": a multi-agent system where you type a
one-line product idea and a crew of specialised agents plans it, writes a
Next.js/TypeScript app, reviews its own code, and deploys it to Vercel — with
human approval gates. It's built on the **Claude Agent SDK** (Python).

It doubles as a learning project: the owner (intermediate Python dev, PyCharm,
prefers React/Next.js/TypeScript front ends) is using it to understand
multi-agent orchestration deeply. Favour explanations that show *how* and
*why* the code works, not just *what* to run.

The authoritative design doc is **`SHIP-IT_BUILD_PLAN.md`**. Read it before
making architectural changes.

## Current status

- **Milestone 0 (DONE):** CLI spike proving the orchestrator -> subagent flow
  end to end. Idea -> Planner spec -> Coder writes a one-page Next.js app to
  disk. Verified live: page.tsx written, 3 turns, ~$0.26/run.
- **Milestone 1 (DONE):** full backend pipeline. Added Scaffolder, Reviewer
  (with a Coder↔Reviewer green-build loop, capped at 3 rounds), Deployer
  (Vercel CLI, opt-in via `--deploy`). SQLite-backed run state at
  `backend/shipit.db` (`runs` + `events` tables). `EventBus` fans events to
  the CLI printer + the SQLite recorder. The orchestrator is a **Python
  state machine**, not a model agent — each stage opens its own focused
  `query()` with that role as the main agent (`system_prompt`), so context
  is isolated and gating is trivial to add. CLI: `--deploy`, `--max-rounds`,
  per-role `--*-model`, `--list-runs`, `--show-run <id>`.
- **Milestone 2 (DONE):** Next.js + TS + Tailwind dashboard at `frontend/`
  driven by a FastAPI server at `backend/app/server.py`. The dashboard lists
  runs, starts new ones (`POST /api/runs`), and tails live progress via SSE
  (`GET /api/runs/{id}/events/stream`). The SSE stream polls SQLite for new
  rows past `last_id` — that one design choice means the dashboard
  works equally well for runs started in-process (via API) and runs started
  in another process by the M1 CLI.
- **Milestone 3 (DONE):** approval gates. New `backend/app/gates.py`
  (`GateBroker` + `GateDecision`); the orchestrator emits `gate_open`,
  awaits an `asyncio.Future` resolved by `POST /api/runs/{id}/gate/{name}`,
  then emits `gate_decision`. Three gates fire: **spec** (before any code
  is written; notes flow into the Coder's brief), **code** (after the
  green build), and **deploy** (only when `--deploy`). The dashboard
  renders an approve/reject/notes panel from `gate_open` events and clears
  it on `gate_decision`. `POST /api/runs/{id}/cancel` rejects every
  pending gate, aborting the run cleanly. CLI keeps M1/M2 behaviour: when
  `OrchestratorConfig.gate_broker is None`, `_gate()` is a silent no-op.
- **Milestone 4 (DONE):** polish. The code-gate panel now renders a
  per-line-coloured `git diff` against the scaffolder commit; the spec
  gate renders a structured view of the planner spec; the deploy gate
  renders a confirmation card. `NewRunForm` has an *Advanced* section
  exposing per-worker model overrides (`planner_model`, `scaffolder_model`,
  `coder_model`, `reviewer_model`, `deployer_model`) which thread through
  `POST /api/runs` to `OrchestratorConfig`. The run detail page now has a
  *Cancel run* button while a run is live. `Store.decode_event(row)` is
  the single source of truth for event-row decoding, used by both the
  FastAPI server and the CLI `--show-run` replay.
- **Milestone 5 (DONE):** restart-resumable, DB-backed gates. Gate state
  moved from in-process `asyncio.Future`s to a new SQLite `gates` table
  (`id, run_id, name, status, notes, payload, opened_at, decided_at`);
  `GateBroker` now polls the table every ~250ms for a decision — the same
  "poll SQLite" trick the SSE event stream already uses, so multi-worker
  correctness and restart-durability fall out of one mechanism. Run config
  (deploy flag, `max_review_rounds`, all five `*_model` overrides) is now
  persisted in a `config` JSON column on `runs` (idempotent `ALTER TABLE`
  migration) so a resumed run knows how to finish. `Orchestrator.resume_tail`
  re-enters the pipeline at the gate it died on; a startup sweep
  `_recover_runs()` in the FastAPI `lifespan` claims every `'running'` run
  (atomic `UPDATE … WHERE status='running'` guard against double-resume),
  resumes code/deploy-gated runs via `resume_tail`, and marks spec-gate or
  mid-stage runs `'interrupted'` (too cheap to bother resuming). Two new
  event kinds: `pipeline_resumed`, `run_interrupted`. 23 tests in
  `backend/tests/`. Non-goal: resuming a run that died mid-LLM-stage
  (in-flight agent calls cannot be replayed).

When you complete a milestone, update this section and the milestone list in
`SHIP-IT_BUILD_PLAN.md`.

## Repository layout

```
.
├── CLAUDE.md                 # this file
├── SHIP-IT_BUILD_PLAN.md     # the design doc / source of truth
├── backend/                  # Python backend (Claude Agent SDK + FastAPI)
│   ├── app/
│   │   ├── agents/           # one stage-options factory per worker
│   │   │   ├── planner.py    # think-only: idea -> spec
│   │   │   ├── scaffolder.py # Read/Write/Edit/Bash: fresh Next.js project
│   │   │   ├── coder.py      # Read/Write/Edit/Bash: spec -> page.tsx
│   │   │   ├── reviewer.py   # Read/Bash (read-only): lint/tsc/build verdict
│   │   │   └── deployer.py   # Read/Bash: vercel deploy --prod
│   │   ├── events.py         # PipelineEvent + EventBus
│   │   ├── store.py          # SQLite Store: runs + events
│   │   ├── gates.py          # GateBroker + GateDecision, DB-backed (M3+M5)
│   │   ├── orchestrator.py   # Python state machine driving the stages
│   │   ├── server.py         # FastAPI app: /api/runs + SSE + gates (M2+M3)
│   │   └── run_spike.py      # CLI entrypoint (M0+M1)
│   ├── requirements.txt
│   ├── shipit.db             # (gitignored) SQLite run/event store
│   └── .env.example
├── frontend/                 # Next.js 14 + TS + Tailwind dashboard (M2)
│   ├── app/                  # App Router pages
│   │   ├── page.tsx          # runs list + new-idea form
│   │   ├── runs/[id]/page.tsx# live run view (SSE)
│   │   ├── layout.tsx
│   │   └── globals.css
│   ├── components/
│   │   ├── RunsList.tsx
│   │   ├── NewRunForm.tsx
│   │   ├── ActivityStream.tsx# SSE consumer + open-gate tracker (M2+M3)
│   │   └── GatePanel.tsx     # approve/reject/notes panel (M3)
│   ├── lib/{api,types}.ts    # typed fetch + DTOs mirroring PipelineEvent
│   └── next.config.mjs       # rewrites /api/* -> FastAPI backend
├── workspaces/               # (gitignored) per-run generated apps
└── tutorial/                 # progressive teaching tutorial (3 stages)
```

To run the full M2 stack locally:

```bash
# terminal 1 — FastAPI backend
cd backend && source .venv/bin/activate
uvicorn app.server:app --reload --port 8000

# terminal 2 — Next.js dashboard
cd frontend && npm install && npm run dev    # http://localhost:3000
```

The Next config rewrites `/api/*` to `http://127.0.0.1:8000` so the dashboard
calls same-origin URLs (no CORS surprises). Override with
`NEXT_PUBLIC_BACKEND_URL` if the backend lives elsewhere.

## How to run (Milestone 0)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Auth: either run `claude` once to log in, or set ANTHROPIC_API_KEY (.env)
python -m app.run_spike                              # uses the default idea
python -m app.run_spike "a landing page for a yoga studio"
```

The script's pass/fail signal is whether `generated_app/app/page.tsx` was
created — a deliberately transcript-independent check.

## Key SDK facts & gotchas (Claude Agent SDK, Python)

These are the things that bite people; respect them when editing:

- **Subagents are invoked via the built-in `Agent` tool.** `"Agent"` MUST be
  in `ClaudeAgentOptions.allowed_tools`, or delegation silently won't happen
  and the orchestrator does the work itself. This is the #1 failure mode.
- **Subagents have isolated context.** A subagent sees only its own system
  prompt + the brief passed in the Agent tool call — NOT the orchestrator's
  history. Pack everything a worker needs into its brief.
- **`AgentDefinition.tools`:** omit = inherit all tools; `[]` = no tools (used
  for the think-only Planner); a list = exactly those tools.
- **`allowed_tools` is a pre-approval allowlist**, not an availability filter.
  Custom-tool ids follow `mcp__<server-label>__<tool-name>`.
- **Distinguishing parent vs. subagent messages:** messages from inside a
  subagent carry a non-empty `parent_tool_use_id` (a field on
  `AssistantMessage`). The orchestrator uses this to label events.
- **`permission_mode="acceptEdits"` + `cwd`** is how the spike runs unattended
  inside a sandbox. Don't point `cwd` at anything important; `Bash` can run
  arbitrary commands within it.
- **Billing:** from **2026-06-15**, Agent SDK usage on subscription plans draws
  from a separate monthly Agent SDK credit pool. Relevant for tight loops.
- Pinned to `claude-agent-sdk>=0.2.0,<0.3.0` (verified against 0.2.87).

## Conventions

- **Agents are factories**, not constants: each `app/agents/*.py` exposes a
  `build_<name>(model=...)` returning an `AgentDefinition`, so later milestones
  can parameterise model/strictness at runtime.
- **Progress is structured, not printed ad hoc.** The orchestrator emits
  `PipelineEvent`s through an `on_event` callback. Keep this — in Milestone 2
  the same events become SSE messages to the dashboard. Don't bury `print`s
  inside the run loop; emit events.
- **Per-worker model choice is the main cost lever.** Cheap models for
  planning/review, stronger for coding/orchestration.
- Python: type hints, async-first (the SDK is async). Target Python 3.10+.
- Front end (when it exists): Next.js App Router + TypeScript, SSE via
  `EventSource`, Tailwind + shadcn/ui.

## Safety rails to preserve

- Always cap loops: `max_turns` on each `query()` (inner loop) and an explicit
  round cap on any outer Coder⇄Reviewer loop. Never ship an un-approved result;
  on hitting a cap, escalate rather than pretend success.
- Keep generated-app work confined to `generated_app/` / a per-project
  workspace. Containerise the build step before running untrusted briefs.

## Where to learn the mechanics

[`LEARNING.md`](LEARNING.md) is an annotated walkthrough of how the system fits
together — the Python-state-machine orchestrator, how each agent fires up via
`query()`, context isolation, the capped Coder⇄Reviewer loop, the event bus, and
the asyncio approval gates — with inline `file:line` links into the source and a
mapping to LangGraph / CrewAI / OpenAI Agents SDK. Start there.

`tutorial/` builds the same concepts up in three runnable, annotated stages
(single agent → orchestrator+worker → coder/reviewer loop). Point newcomers
there; it mirrors this codebase's design.
