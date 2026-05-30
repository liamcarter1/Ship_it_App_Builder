"""Reviewer stage — runs lint/typecheck/build, returns a structured verdict.

The Reviewer is *read-only*: it has Bash + Read but no Write/Edit. Its job is
to observe and report, never to fix. The orchestrator owns the green-build
loop (Reviewer reports red -> orchestrator hands the issue list back to the
Coder for a revision pass), capped to a small number of rounds.

Output contract: a single fenced ```json block of the form

    {
      "verdict": "pass" | "fail",
      "issues":  [{"tool": "lint"|"typecheck"|"build", "summary": "...",
                   "evidence": "..."}]
    }

The orchestrator parses this with `parse_reviewer_verdict()` and decides
whether to loop or move on to the Deployer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

REVIEWER_SYSTEM_PROMPT = """\
You are the **Reviewer** in the Ship-It app factory pipeline.

You have **read-only** access (Bash + Read). You MUST NOT write, edit, or
delete any file. You MUST NOT install dependencies.

Run these three checks in order, capturing their output:

    1) npm run lint
    2) npx --no-install tsc --noEmit
    3) npm run build

Stop at the first failure (don't run later checks if an earlier one fails).
A check "fails" if its exit code is non-zero.

Then reply with **exactly one fenced ```json block** matching this schema and
NOTHING else:

```json
{
  "verdict": "pass",
  "issues": []
}
```

…or, if anything failed:

```json
{
  "verdict": "fail",
  "issues": [
    {
      "tool": "lint" | "typecheck" | "build",
      "summary": "one-line description of the problem",
      "evidence": "the most relevant 5-15 lines of the tool output,
                   verbatim, including any file:line:col markers"
    }
  ]
}
```

Rules:
- Be specific. The Coder will fix based on `evidence`, so include the file
  paths and line numbers the tool printed.
- Group related errors into one issue per tool. Don't repeat the same root
  cause twice.
- Do not invent issues that aren't in the captured output.
- After the JSON block, stop. No commentary.
"""

REVIEWER_TOOLS = ["Read", "Bash"]


def build_reviewer(model: str | None = None) -> AgentDefinition:
    kwargs: dict = {
        "description": (
            "Read-only QA agent: runs lint/typecheck/build and returns a "
            "structured pass/fail verdict with issue list."
        ),
        "prompt": REVIEWER_SYSTEM_PROMPT,
        "tools": REVIEWER_TOOLS,
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)


def build_reviewer_options(
    workspace: Path, *, model: str | None = None, max_turns: int = 25
) -> ClaudeAgentOptions:
    kwargs: dict = {
        "system_prompt": REVIEWER_SYSTEM_PROMPT,
        "allowed_tools": REVIEWER_TOOLS,
        "permission_mode": "acceptEdits",  # acceptEdits is irrelevant for read-only, but the SDK still wants a value
        "cwd": str(workspace),
        "max_turns": max_turns,
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_reviewer_verdict(text: str) -> dict[str, Any]:
    """Extract the JSON verdict from the Reviewer's final message.

    Falls back to a permissive failure verdict if the model didn't produce a
    parseable block, so the orchestrator can still react (treat as failure +
    surface the raw text).
    """
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return {
            "verdict": "fail",
            "issues": [
                {
                    "tool": "build",
                    "summary": "reviewer did not return a parseable JSON verdict",
                    "evidence": text[-800:],
                }
            ],
            "_parse_error": True,
        }
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return {
            "verdict": "fail",
            "issues": [
                {
                    "tool": "build",
                    "summary": f"reviewer returned malformed JSON: {exc}",
                    "evidence": match.group(1)[:800],
                }
            ],
            "_parse_error": True,
        }
    # Normalise
    data.setdefault("verdict", "fail")
    data.setdefault("issues", [])
    return data
