# Ship-It backend — Milestone 0 spike

A CLI script that proves the end-to-end **orchestrator → subagent** flow on the
Claude Agent SDK: you give it a one-line product idea, a **Planner** subagent
writes a tight spec, and a **Coder** subagent writes a one-page Next.js app to
disk under `../generated_app/`.

No web UI, no gates, no review loop — those arrive in later milestones. The
whole point of this milestone is to confirm the subagent wiring works before
we build anything else on top.

## Setup

```bash
# from repo root
cd backend
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env   # then edit .env if you want to set ANTHROPIC_API_KEY
```

You need **either**:

- An `ANTHROPIC_API_KEY` in `.env` (or your shell), **or**
- A logged-in `claude` CLI session (run `claude` once interactively).

## Run it

```bash
python -m app.run_spike                                  # default idea
python -m app.run_spike "a landing page for a yoga studio"
```

The pass/fail check is **file-based**, not transcript-based: after the run,
the script verifies that `../generated_app/app/page.tsx` exists. If it does,
the subagent flow worked end to end.

## What's actually happening

1. `run_spike.py` parses the idea, prepares `generated_app/`, and calls
   `orchestrator.run_pipeline(idea, on_event=...)`.
2. The orchestrator opens a single `query()` with `ClaudeAgentOptions`
   containing both subagents (`planner`, `coder`) in its `agents` dict and
   `"Agent"` in `allowed_tools` so delegation is actually allowed.
3. The orchestrator's *user prompt* is a small script: "delegate to planner,
   then hand its spec to coder, who writes files into the cwd."
4. The SDK streams messages; we read `parent_tool_use_id` on each
   `AssistantMessage` to label whether the text came from the orchestrator or
   from a subagent, and we emit a structured `PipelineEvent` for each one.

This same event stream becomes the SSE feed for the dashboard in Milestone 2,
which is why we keep print-free, structured events from day one.

## Layout

```
backend/
├── app/
│   ├── __init__.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── planner.py     # think-only Planner factory
│   │   └── coder.py       # Read/Write/Edit/Bash Coder factory
│   ├── orchestrator.py    # pipeline runner + PipelineEvent stream
│   └── run_spike.py       # CLI entrypoint
├── requirements.txt
├── .env.example
└── README.md (this file)
```

## Next: Milestone 1

Add the Scaffolder, the Reviewer (with a Coder↔Reviewer green-build loop),
the Deployer (Vercel MCP), and SQLite-backed run state. Still CLI-driven, but
ending in a real live URL. See `../SHIP-IT_BUILD_PLAN.md` for the plan.
