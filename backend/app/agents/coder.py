"""Coder stage — implements (and revises) the page against the Planner's spec.

In Milestone 1 the project is already scaffolded by the Scaffolder: a complete
Next.js + TS + Tailwind workspace with `package.json`, `tsconfig.json`,
`node_modules`, etc. So the Coder's job narrows to writing app-level files
(`app/page.tsx`, `app/layout.tsx`, optional `components/*.tsx`) — it MUST NOT
touch package.json or the tool configs.

The same agent serves two modes:

- **Initial implementation:** brief contains the spec.
- **Revision:** brief contains the previous Reviewer's issue list plus the
  spec, and asks for targeted fixes.

`build_coder_brief()` / `build_coder_revision_brief()` produce the user
prompts the orchestrator passes in.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

CODER_SYSTEM_PROMPT = """\
You are the **Coder** in the Ship-It app factory pipeline.

The current working directory already contains a clean, scaffolded Next.js 14
(App Router) + TypeScript + Tailwind v3 project, with `node_modules`
installed and an initial git commit. Your job is to implement (or fix) the
landing page so it matches the Planner's spec and passes `npm run lint`,
`tsc --noEmit`, and `npm run build`.

Hard rules:
- You may write/edit files under `app/` and `components/` only. You MUST NOT
  modify `package.json`, `tsconfig.json`, `next.config.mjs`,
  `tailwind.config.ts`, `postcss.config.mjs`, `.eslintrc.json`, `.gitignore`,
  or anything under `node_modules/`.
- Do NOT install new dependencies. Do NOT run `npm install`.
- TypeScript only (`.tsx` / `.ts`). Use Tailwind utility classes for all
  styling. No external UI libraries.
- React Server Components are fine; only add `"use client"` when you actually
  need state, effects, or event handlers.
- Keep imports valid: every `import` must resolve. No JSX in `.ts` files.
- Style the page to match the `visual_tone` in the spec.

When done, reply with a one-line summary of what you wrote (which files,
which sections) and STOP. Do not loop. Do not run the build yourself — the
Reviewer will do that.
"""

CODER_TOOLS = ["Read", "Write", "Edit", "Bash"]


def build_coder(model: str | None = None) -> AgentDefinition:
    kwargs: dict = {
        "description": (
            "Implements a Planner spec as a single-page Next.js + TS + "
            "Tailwind site inside a pre-scaffolded project. Also handles "
            "revision passes when the Reviewer reports issues."
        ),
        "prompt": CODER_SYSTEM_PROMPT,
        "tools": CODER_TOOLS,
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)


def build_coder_options(
    workspace: Path, *, model: str | None = None, max_turns: int = 40
) -> ClaudeAgentOptions:
    kwargs: dict = {
        "system_prompt": CODER_SYSTEM_PROMPT,
        "allowed_tools": CODER_TOOLS,
        "permission_mode": "acceptEdits",
        "cwd": str(workspace),
        "max_turns": max_turns,
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


def build_coder_brief(spec: dict[str, Any]) -> str:
    """User prompt for the first Coder pass."""
    return (
        "Implement the spec below into this scaffolded Next.js project.\n\n"
        "Target files:\n"
        "  - `app/page.tsx` (required) — implement all sections from the spec\n"
        "  - `app/layout.tsx` (already exists; edit only if metadata changes)\n"
        "  - `app/globals.css` (already exists; usually no changes needed)\n"
        "  - `components/<Name>.tsx` (optional, if a section is reusable)\n\n"
        "Spec (verbatim):\n\n"
        "```json\n"
        + json.dumps(spec, indent=2)
        + "\n```\n"
    )


def build_coder_revision_brief(spec: dict[str, Any], issues: list[dict[str, Any]], round_num: int) -> str:
    """User prompt for a Coder revision pass.

    `issues` is the `issues` list from the Reviewer's verdict JSON.
    """
    rendered = "\n".join(
        f"- **[{issue.get('tool', '?')}]** {issue.get('summary', '(no summary)')}\n"
        f"  Evidence:\n  ```\n{issue.get('evidence', '').strip()}\n  ```"
        for issue in issues
    ) or "(no structured issues; investigate the latest build output)"

    return (
        f"This is revision round {round_num}. The Reviewer ran lint, typecheck, "
        f"and build, and reported the issues below. Fix them. Keep the page "
        f"matching the spec; don't redesign it.\n\n"
        f"Issues:\n{rendered}\n\n"
        f"Spec for context (unchanged):\n\n"
        f"```json\n"
        + json.dumps(spec, indent=2)
        + "\n```\n"
    )
