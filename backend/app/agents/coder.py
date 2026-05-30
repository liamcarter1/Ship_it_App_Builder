"""Coder subagent — implements the Planner's spec as files on disk.

Has Read/Write/Edit/Bash so it can create the Next.js page. Runs inside the
orchestrator's `cwd` (the `generated_app/` workspace), so its writes are
sandboxed to that directory.
"""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

CODER_SYSTEM_PROMPT = """\
You are the **Coder** in a multi-agent app-factory pipeline.

You receive a JSON spec from the Planner inside your brief. Implement it as a
**single-page Next.js (App Router) + TypeScript + Tailwind** site, writing
files into the current working directory (which is the project workspace).

Hard requirements for Milestone 0:
- Write `app/page.tsx` — this file MUST exist when you're done. The pipeline's
  pass/fail check is purely whether this file exists.
- Also write a minimal `app/layout.tsx` (root layout with `<html>` and
  `<body>`) and `app/globals.css` (with the three Tailwind directives:
  `@tailwind base; @tailwind components; @tailwind utilities;`).
- Use only Tailwind utility classes for styling. No external UI libraries.
- TypeScript only. React Server Components are fine; no client-side state
  needed for a static landing page.
- Do NOT run `npm install`, `npm run build`, or any package manager command.
  Do NOT create `package.json`, `next.config.js`, `tsconfig.json`, or
  `node_modules`. The Scaffolder owns those in Milestone 1; for now, only
  write the three files above.
- Keep the page short and tasteful. Match the `visual_tone` in the spec.

When done, reply with a one-line summary of what you wrote (which files,
which sections) and STOP. Do not loop.
"""


def build_coder(model: str | None = None) -> AgentDefinition:
    """Factory for the Coder AgentDefinition.

    Args:
        model: Optional model override. Omit to use the SDK default.
    """
    kwargs: dict = {
        "description": (
            "Implements a Planner spec as a single-page Next.js + TS + "
            "Tailwind site. Writes app/page.tsx, app/layout.tsx, "
            "app/globals.css into the project workspace."
        ),
        "prompt": CODER_SYSTEM_PROMPT,
        "tools": ["Read", "Write", "Edit", "Bash"],
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)
