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
  the CLI printer + the SQLite recorder; in M2 the SSE handler is a third
  listener. The orchestrator is now a **Python state machine**, not a model
  agent — each stage opens its own focused `query()` with that role as the
  main agent (`system_prompt`), so context is isolated and gating is trivial
  to add. CLI: `--deploy`, `--max-rounds`, per-role `--*-model`,
  `--list-runs`, `--show-run <id>`.
- **Milestone 2 (next):** Next.js dashboard (read-only) that lists runs and
  streams the live `PipelineEvent` feed over SSE.
- **Milestones 3–4:** approval gates (spec / code / deploy) → polish (diff
  viewer, model selection per worker, cost tracking).

When you complete a milestone, update this section and the milestone list in
`SHIP-IT_BUILD_PLAN.md`.

## Repository layout

```
.
├── CLAUDE.md                 # this file
├── SHIP-IT_BUILD_PLAN.md     # the design doc / source of truth
├── backend/                  # Python backend (Claude Agent SDK)
│   ├── app/
│   │   ├── agents/           # one stage-options factory per worker
│   │   │   ├── planner.py    # think-only: idea -> spec
│   │   │   ├── scaffolder.py # Read/Write/Edit/Bash: fresh Next.js project
│   │   │   ├── coder.py      # Read/Write/Edit/Bash: spec -> page.tsx
│   │   │   ├── reviewer.py   # Read/Bash (read-only): lint/tsc/build verdict
│   │   │   └── deployer.py   # Read/Bash: vercel deploy --prod
│   │   ├── events.py         # PipelineEvent + EventBus
│   │   ├── store.py          # SQLite Store: runs + events
│   │   ├── orchestrator.py   # Python state machine driving the stages
│   │   └── run_spike.py      # CLI entrypoint (M0+M1)
│   ├── requirements.txt
│   ├── shipit.db             # (gitignored) SQLite run/event store
│   └── .env.example
├── workspaces/               # (gitignored) per-run generated apps
└── tutorial/                 # progressive teaching tutorial (3 stages)
```

The `frontend/` (Next.js dashboard) does not exist yet — it arrives in
Milestone 2.

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

`tutorial/` builds the same concepts up in three runnable, annotated stages
(single agent → orchestrator+worker → coder/reviewer loop). Point newcomers
there; it mirrors this codebase's design.
