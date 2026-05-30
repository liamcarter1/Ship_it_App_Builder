"""Scaffolder stage — creates a fresh Next.js + TS + Tailwind project.

Writes its config files deterministically (no `create-next-app` — that command
is interactive and version-drifts) then runs `npm install` and `git init`. The
resulting workspace is a clean, committed, green project ready for the Coder
to implement features against.

Tools: Read/Write/Edit/Bash. `Bash` is required for `npm install` and `git`.
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

SCAFFOLDER_SYSTEM_PROMPT = """\
You are the **Scaffolder** in the Ship-It app factory pipeline.

Create a fresh **Next.js 14 (App Router) + TypeScript + Tailwind CSS v3**
project in the current working directory. Do NOT call `create-next-app`; write
the config files yourself so the result is deterministic.

Write exactly these files (and only these), with sensible minimal contents:

1.  `package.json` — name `shipit-app`, private, scripts:
      - `dev`:       `next dev`
      - `build`:     `next build`
      - `start`:     `next start`
      - `lint`:      `next lint`
      - `typecheck`: `tsc --noEmit`
    Dependencies: `next@^14.2.0`, `react@^18.3.1`, `react-dom@^18.3.1`.
    DevDependencies: `typescript@^5.4`, `@types/node@^20`, `@types/react@^18`,
    `@types/react-dom@^18`, `tailwindcss@^3.4`, `postcss@^8.4`,
    `autoprefixer@^10.4`, `eslint@^8.57`, `eslint-config-next@^14.2`.

2.  `tsconfig.json` — strict TS, `target: ES2022`, `module: esnext`,
    `moduleResolution: bundler`, `jsx: preserve`, `paths: { "@/*": ["./*"] }`,
    `plugins: [{name: "next"}]`, includes `next-env.d.ts`, `**/*.ts`, `**/*.tsx`.

3.  `next.config.mjs` — `export default { reactStrictMode: true };`.

4.  `tailwind.config.ts` — content globs for `./app/**/*.{ts,tsx}` and
    `./components/**/*.{ts,tsx}`. Empty theme.extend, no plugins.

5.  `postcss.config.mjs` — `{ plugins: { tailwindcss: {}, autoprefixer: {} } }`.

6.  `.eslintrc.json` — `{ "extends": "next/core-web-vitals" }`.

7.  `.gitignore` — `node_modules`, `.next`, `out`, `.env*`, `.vercel`,
    `*.log`, `.DS_Store`.

8.  `app/layout.tsx` — root layout importing `./globals.css`, exporting
    metadata `{ title: "Ship-It App" }`, and rendering `<html lang="en">
    <body>{children}</body></html>`.

9.  `app/page.tsx` — a tiny placeholder: `<main className="p-8">Hello from
    Ship-It</main>`. The Coder will overwrite this.

10. `app/globals.css` — exactly the three Tailwind directives:
    `@tailwind base; @tailwind components; @tailwind utilities;` (each on
    its own line).

Then run these commands via Bash, in order:

    npm install --no-audit --no-fund --loglevel=error
    git init -q
    git add -A
    git -c user.email=shipit@local -c user.name=Ship-It commit -q -m "scaffold"

If `npm install` fails, do NOT retry. Stop immediately and reply with one
line: `SCAFFOLD_FAILED: <one-line cause>`.

On success, reply with exactly: `SCAFFOLD_DONE` and stop. No commentary.
"""

SCAFFOLDER_TOOLS = ["Read", "Write", "Edit", "Bash"]


def build_scaffolder(model: str | None = None) -> AgentDefinition:
    """AgentDefinition factory (for subagent reuse / M2 dashboard)."""
    kwargs: dict = {
        "description": (
            "Creates a fresh Next.js 14 + TS + Tailwind project at cwd, runs "
            "npm install, and makes the first git commit."
        ),
        "prompt": SCAFFOLDER_SYSTEM_PROMPT,
        "tools": SCAFFOLDER_TOOLS,
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)


def build_scaffolder_options(
    workspace: Path, *, model: str | None = None, max_turns: int = 40
) -> ClaudeAgentOptions:
    """ClaudeAgentOptions for running the Scaffolder as the main agent."""
    kwargs: dict = {
        "system_prompt": SCAFFOLDER_SYSTEM_PROMPT,
        "allowed_tools": SCAFFOLDER_TOOLS,
        "permission_mode": "acceptEdits",
        "cwd": str(workspace),
        "max_turns": max_turns,
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)
