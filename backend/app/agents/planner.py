"""Planner stage — turns a one-line idea into a concrete JSON spec.

Think-only: `tools=[]`. The downstream Scaffolder + Coder implement against
the spec without needing to talk back to the Planner.
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

PLANNER_SYSTEM_PROMPT = """\
You are the **Planner** in the Ship-It app factory pipeline.

You receive a one-line product idea. Produce a tight, concrete spec a Coder
can implement without asking further questions.

Constraints:
- Output target is a **single-page Next.js (App Router) + TypeScript +
  Tailwind** site. No backend, no auth, no DB. The route is `app/page.tsx`.
- Keep scope small: hero, 2-3 supporting sections, one CTA. The Coder should
  be able to ship it in one pass.
- Do NOT write code. Do NOT include implementation details — that's the
  Coder's job. Stick to *what* and *why*, not *how*.

Respond with **exactly one fenced ```json block** matching this schema, and
nothing else after it:

```json
{
  "title": "string — short product name",
  "one_liner": "string — refined one-sentence pitch",
  "audience": "string — who it's for",
  "sections": [
    {"name": "string", "purpose": "string", "key_content": "string"}
  ],
  "primary_cta": {"label": "string", "intent": "string"},
  "visual_tone": "string — 1-2 adjectives (e.g. 'calm, minimal')",
  "notes_for_coder": "string — anything non-obvious the Coder must respect"
}
```
"""

PLANNER_TOOLS: list[str] = []


def build_planner(model: str | None = None) -> AgentDefinition:
    kwargs: dict = {
        "description": (
            "Turns a one-line product idea into a concrete JSON spec for a "
            "single-page Next.js app. Think-only — no tools."
        ),
        "prompt": PLANNER_SYSTEM_PROMPT,
        "tools": PLANNER_TOOLS,
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)


def build_planner_options(
    workspace: Path | None = None, *, model: str | None = None, max_turns: int = 6
) -> ClaudeAgentOptions:
    """Run the Planner as the main agent of its own `query()`.

    `workspace` is accepted for API parity with the other stages but unused —
    the Planner has no tools and doesn't touch the filesystem.
    """
    kwargs: dict = {
        "system_prompt": PLANNER_SYSTEM_PROMPT,
        "allowed_tools": PLANNER_TOOLS,
        "permission_mode": "acceptEdits",
        "max_turns": max_turns,
    }
    if workspace is not None:
        kwargs["cwd"] = str(workspace)
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


import json
import re

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_planner_spec(text: str) -> dict:
    """Extract the JSON spec from the Planner's final message."""
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        raise ValueError("Planner did not return a fenced JSON block")
    return json.loads(match.group(1))
