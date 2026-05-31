# Ship-It — Autonomous Idea→Deployed App Factory

**Build plan v1 · 30 May 2026**

A personal multi-agent system: you type a one-line product idea into a web dashboard, and a crew of specialised AI agents plans it, writes a Next.js/TypeScript app, reviews its own code, and deploys it to Vercel — pausing at approval gates so you stay in control. You watch the whole thing happen live and end up with a real URL.

---

## 1. Why this is worth building

Most "AI app builders" are a single prompt wrapped in a UI. This is different in three ways that make it both a stronger learning project and genuinely useful to you:

- **It's an orchestration project, not a wrapper.** You build a *team* of agents with distinct roles (the orchestrator-worker pattern), which is the current frontier of agentic engineering. You learn context isolation, delegation, and self-review loops — skills that transfer to anything agentic.
- **It closes the loop to production.** Idea → code → review → *live deployment* on Vercel, using the Vercel tooling you already have connected. Nothing teaches you faster than "build me X" producing a working link.
- **It's reusable infrastructure.** Once built, it's your personal app factory. Every future side-project starts at the dashboard instead of a blank `create-next-app`.

---

## 2. Architecture at a glance

```
┌──────────────────────────────────────────────────────────────┐
│  FRONTEND  (Next.js + TypeScript dashboard)                    │
│  • Idea input + project list                                   │
│  • Live agent activity stream (SSE)                            │
│  • Approval-gate panels: spec review · diff review · deploy    │
└───────────────▲───────────────────────────────┬───────────────┘
                │ Server-Sent Events (live logs)  │ REST (commands)
                │                                  ▼
┌──────────────────────────────────────────────────────────────┐
│  BACKEND  (Python · FastAPI · Claude Agent SDK)                │
│                                                                │
│   ┌────────────────  ORCHESTRATOR  ────────────────┐          │
│   │  Owns the pipeline state machine + gates         │          │
│   └───┬──────────┬──────────┬──────────┬────────────┘          │
│       ▼          ▼          ▼          ▼                        │
│   Planner    Scaffolder   Coder     Reviewer    Deployer       │
│  (subagent) (subagent)  (subagent) (subagent)  (subagent)      │
│                                                                │
│  Tools available to workers: filesystem · git · shell ·        │
│  build/lint/test runner · Vercel deploy (MCP)                  │
└───────────────┬────────────────────────────────┬──────────────┘
                ▼                                  ▼
        SQLite (run state,            Generated app workspaces
        specs, approvals, logs)       (one git repo per project)
```

The **orchestrator** uses a capable model and owns the state machine; the **workers** are subagents with isolated context windows that each do one focused job and report back only what matters. This keeps the orchestrator's context clean and lets you swap cheaper models into specific workers later to control cost.

---

## 3. The agent crew

Each worker is a Claude Agent SDK **subagent** — a separate agent instance with its own system prompt and its own context window. The orchestrator hands each one a self-contained brief (it can't see the orchestrator's history, so the prompt must carry every file path and decision it needs).

| Agent | Job | Key tools | Output |
|---|---|---|---|
| **Orchestrator** | Runs the pipeline, enforces gates, routes work, aggregates results | (delegation only) | Run state |
| **Planner** | Turns your one-liner into a concrete spec: features, pages, routes, data model, component list, tech choices | web search (optional) | `spec.json` + human-readable summary |
| **Scaffolder** | Creates the Next.js + TS project, installs deps, sets up folder structure and config | shell, filesystem, git | Working empty app, committed |
| **Coder** | Implements features against the spec, page by page / component by component | filesystem, shell | Code commits per feature |
| **Reviewer** | Runs `lint`, `typecheck`, `build`, optional tests; reads the diff; flags issues and either approves or sends fixes back to Coder | shell, filesystem | Pass/fail + issue list |
| **Deployer** | Deploys the approved build to Vercel, returns the live URL and deployment logs | Vercel MCP | Live URL |

The **Coder ↔ Reviewer loop** is the heart of the system: the Reviewer can bounce work back with specific feedback until the build is green, then escalate to you. This self-correction loop is what makes the output trustworthy rather than "best effort."

---

## 4. Approval gates (you stay in control)

The pipeline pauses and waits for you at three checkpoints. Each gate is a backend pause + a frontend panel where you approve, reject, or approve-with-notes (your notes get injected into the next agent's prompt).

1. **Spec gate** — review what it *plans* to build before a line of code is written. Cheapest place to course-correct.
2. **Code gate** — review the diff and the Reviewer's report after the build goes green, before deploying.
3. **Deploy gate** — final confirm before it goes live on Vercel.

Gates are the single most important design decision: they make a runaway agent impossible and turn a scary autonomous system into a tool you trust.

---

## 5. Tech stack & rationale

**Backend — Python**
- **FastAPI** — async, first-class Server-Sent Events for streaming live agent logs to the UI, clean REST for commands. Pairs naturally with PyCharm.
- **Claude Agent SDK** (`pip install claude-agent-sdk`, Python 3.10+) — gives you the same agent loop, tool use, and context management that powers Claude Code, plus native **subagents** for the worker pattern. This is the piece that makes the multi-agent design straightforward instead of hand-rolled.
- **SQLite** — run state, specs, approvals, and logs. Zero-config and perfect for a personal tool; swap for Postgres only if you ever multi-user it.
- **Git worktrees / one repo per generated app** — each project is an isolated, versioned workspace, so the agents can't trample each other and you get a clean history per app.

**Frontend — Next.js + TypeScript + React**
- App Router, server components for the shell, client components for the live stream.
- **SSE consumer** (`EventSource`) for the real-time agent activity feed — simpler than WebSockets and one-directional is all we need.
- Tailwind for fast, clean styling; shadcn/ui for the gate panels and diff viewer.

**Sandboxing**
- Generated-app builds and shell commands run in an **isolated working directory per project**, never against your real environment. (Phase 2: containerise the build step for stronger isolation.)

---

## 6. Suggested project structure

```
Next Agent Build/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app, routes, SSE endpoints
│   │   ├── orchestrator.py    # pipeline state machine + gate logic
│   │   ├── agents/            # subagent definitions (prompts + tools)
│   │   │   ├── planner.py
│   │   │   ├── scaffolder.py
│   │   │   ├── coder.py
│   │   │   ├── reviewer.py
│   │   │   └── deployer.py
│   │   ├── tools/             # filesystem, shell, build-runner, vercel
│   │   ├── store.py           # SQLite models + queries
│   │   └── events.py          # event bus → SSE
│   └── requirements.txt
├── frontend/                  # Next.js + TS dashboard
│   ├── app/
│   │   ├── page.tsx           # project list + new-idea input
│   │   └── runs/[id]/page.tsx # live run view + gate panels
│   └── components/            # ActivityStream, GatePanel, DiffViewer
├── workspaces/                # generated apps live here (one git repo each)
└── SHIP-IT_BUILD_PLAN.md      # this file
```

---

## 7. Build milestones

**Milestone 0 — Spike (prove the core loop). ✅ DONE.** A CLI-only script: one orchestrator + a Planner and a Coder subagent that takes an idea and writes a 1-page Next.js app to disk. No UI, no gates. Goal: confirm the Agent SDK subagent flow works end to end. *Live-run verified: `page.tsx` written, 3 turns, ~$0.26/run.*

**Milestone 1 — Full backend pipeline. ✅ DONE.** Scaffolder, Reviewer (with the Coder↔Reviewer build-green loop, capped at 3 rounds), and Deployer (`--deploy` flag, Vercel CLI) all wired in. SQLite `runs` + `events` schema at `backend/shipit.db`, and an `EventBus` that fans every PipelineEvent to both the CLI printer and the recorder (the SSE handler will be the third listener in M2). The orchestrator is now an explicit **Python state machine** — each stage opens its own focused `query()` with that role as the main agent (`system_prompt`), so context is isolated and Milestone 3's gates drop in naturally between stages. CLI flags: `--deploy`, `--max-rounds`, per-role `--*-model`, `--list-runs`, `--show-run`.

**Milestone 2 — Web dashboard, read-only. ✅ DONE.** Next.js 14 + TS + Tailwind dashboard at `frontend/`, served alongside a FastAPI app at `backend/app/server.py`. The dashboard lists runs (refresh every 2s), starts new ones via `POST /api/runs`, and tails live progress via `GET /api/runs/{id}/events/stream` (Server-Sent Events). The SSE handler polls SQLite for new event rows past `last_id` — that single design choice means the dashboard tails any run regardless of which process started it (in-process via API or out-of-process via the M1 CLI), and avoids the classic replay-vs-live race window. Next config rewrites `/api/*` to the FastAPI backend so the dashboard speaks to its own origin.

**Milestone 3 — Approval gates. ✅ DONE.** All three gates wired (`spec` / `code` / `deploy`). The orchestrator emits `gate_open`, awaits an `asyncio.Future` from a per-process `GateBroker` resolved by `POST /api/runs/{id}/gate/{name}`, then emits `gate_decision`. The dashboard's `ActivityStream` derives its open-gate set directly from the event log so approve/reject panels appear correctly on both live tail AND replay. Spec-gate notes are passed verbatim into the Coder's first brief as a hard requirement. `POST /api/runs/{id}/cancel` rejects every pending gate to abort cleanly. CLI behaviour preserved: when `OrchestratorConfig.gate_broker is None`, `_gate()` is a silent no-op.

**Milestone 4 — Polish & reuse. ✅ DONE.** The `GatePanel` now dispatches to per-gate body renderers: a structured `SpecView` for the spec gate, a line-coloured `DiffView` (over `git diff HEAD` against the scaffolder commit, capped at 40 KB) for the code gate, and a confirmation `DeployView` for the deploy gate. The `NewRunForm` exposes per-worker model overrides in an *Advanced* disclosure that threads through `POST /api/runs` to `OrchestratorConfig`. The run detail page has a *Cancel run* button that hits `POST /api/runs/{id}/cancel`. `Store.decode_event(row)` consolidated the event-row JSON parse used by both the FastAPI server and the CLI `--show-run` replay. Remaining: restart-resumable gates / DB-backed broker (deferred to a follow-up — it's real architecture work).

Each milestone is independently useful and demoable — you're never more than one milestone from "working."

---

## 8. Risks & honest caveats

- **Cost.** The orchestrator makes many model calls (decompose + aggregate on top of every worker call). Fine for personal use, but watch it. Mitigation: cheaper models on Scaffolder/Reviewer, cap iterations on the Coder↔Reviewer loop, and log token spend per run from day one.
- **Agent SDK billing change.** From **15 June 2026**, Agent SDK / `claude -p` usage on subscription plans draws from a separate monthly Agent SDK credit pool rather than your interactive limits — factor this into how heavily you run it. *(Verify current terms before you scale up usage.)*
- **The orchestrator is a single point of failure.** If it misroutes a task, the wrong worker gets it. Mitigation: keep the pipeline a constrained state machine (fixed stages) rather than letting the orchestrator freely improvise routing — predictability is a feature here.
- **Generated code quality varies.** The Reviewer loop + your Code gate are the safety net. Start with simple app types (landing pages, CRUD-on-SQLite, dashboards) before attempting anything with auth or payments.
- **Sandboxing.** Until Milestone 4 containerises builds, the Scaffolder/Coder run shell commands in a working dir — keep that dir well away from anything important.

---

## 9. What you'll learn

Orchestrator-worker multi-agent design · Claude Agent SDK subagents and context isolation · self-correcting review loops · streaming agent state to a UI over SSE · FastAPI async patterns · and the Next.js/Vercel deploy pipeline end to end. Roughly: the backend is where the interesting agent engineering lives; the frontend is a clean, modern React project on top.

---

## 10. Recommended next step

All four planned milestones are done. The remaining hardening work — restart-resumable gates and a DB-backed `GateBroker` for multi-worker deployments — is real architecture work that deserves its own commit; flagged in `backend/README.md` as the next thing to pick up.
