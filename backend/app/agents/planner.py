"""Planner subagent — turns a one-line idea into a concrete spec.

Think-only: `tools=[]` means it has no filesystem or shell access. Its sole
job is to produce a tight JSON spec the Coder can implement against.
"""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

PLANNER_SYSTEM_PROMPT = """\
You are the **Planner** in a multi-agent app-factory pipeline.

You receive a one-line product idea. You must produce a tight, concrete spec
that a downstream Coder subagent can implement without further questions.

Constraints for Milestone 0:
- The output target is a **single-page Next.js (App Router) + TypeScript +
  Tailwind** site. No backend, no auth, no DB. One `app/page.tsx`.
- Keep the scope tiny: hero, one or two supporting sections, a clear CTA.
- Do NOT write code. Do NOT include implementation details — that's the
  Coder's job. Stick to *what* and *why*, not *how*.

Respond with **exactly one fenced ```json block** containing this schema, and
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


def build_planner(model: str | None = None) -> AgentDefinition:
    """Factory for the Planner AgentDefinition.

    Args:
        model: Optional model override (e.g. "claude-haiku-4-5-20251001" for
            a cheaper planner). Omit to use the SDK default.
    """
    kwargs: dict = {
        "description": (
            "Turns a one-line product idea into a concrete JSON spec for a "
            "single-page Next.js app. Think-only — no tools."
        ),
        "prompt": PLANNER_SYSTEM_PROMPT,
        "tools": [],  # think-only
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)
