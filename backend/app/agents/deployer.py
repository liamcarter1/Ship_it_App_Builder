"""Deployer stage — pushes the green build to Vercel and returns a URL.

Uses the Vercel CLI via Bash (assumes `npx vercel` is reachable and a
`VERCEL_TOKEN` env var is set). For Milestone 1 this is intentionally minimal:
one production deploy, parse the URL out of stdout, return it.

In Milestone 4 we'll swap this for the Vercel MCP server (the SDK's
`mcp__vercel__*` tools) so we don't need a CLI on the host, and so the
deployer can also read deployment build logs.
"""
from __future__ import annotations

import re
from pathlib import Path

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

DEPLOYER_SYSTEM_PROMPT = """\
You are the **Deployer** in the Ship-It app factory pipeline.

The current working directory is a green Next.js project (the Reviewer just
passed it). Your job is to deploy it to Vercel in production and return the
live URL.

Assume the host has the `vercel` CLI available via `npx --yes vercel@latest`
and that `VERCEL_TOKEN` is set in the environment.

Run exactly this command via Bash, capturing stdout:

    npx --yes vercel@latest deploy --prod --yes --token "$VERCEL_TOKEN"

If the command exits 0, locate the deployment URL in stdout (it looks like
`https://<something>.vercel.app`). Reply with **exactly one fenced ```json
block** of the form:

```json
{ "status": "deployed", "url": "https://...vercel.app" }
```

If it exits non-zero, reply with:

```json
{ "status": "failed", "url": null, "error": "<one-line cause>" }
```

Do NOT retry. Do NOT modify files. After the JSON block, stop.
"""

DEPLOYER_TOOLS = ["Read", "Bash"]


def build_deployer(model: str | None = None) -> AgentDefinition:
    kwargs: dict = {
        "description": (
            "Deploys the current workspace to Vercel via the CLI and returns "
            "a structured JSON with the live URL."
        ),
        "prompt": DEPLOYER_SYSTEM_PROMPT,
        "tools": DEPLOYER_TOOLS,
    }
    if model:
        kwargs["model"] = model
    return AgentDefinition(**kwargs)


def build_deployer_options(
    workspace: Path, *, model: str | None = None, max_turns: int = 20
) -> ClaudeAgentOptions:
    kwargs: dict = {
        "system_prompt": DEPLOYER_SYSTEM_PROMPT,
        "allowed_tools": DEPLOYER_TOOLS,
        "permission_mode": "acceptEdits",
        "cwd": str(workspace),
        "max_turns": max_turns,
    }
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


_URL_RE = re.compile(r"https://[a-z0-9-]+\.vercel\.app", re.IGNORECASE)


def parse_deployer_result(text: str) -> dict:
    """Best-effort URL extraction from the Deployer's final message."""
    import json
    import re

    block = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(1))
            data.setdefault("status", "unknown")
            data.setdefault("url", None)
            return data
        except json.JSONDecodeError:
            pass
    url_match = _URL_RE.search(text)
    return {
        "status": "deployed" if url_match else "unknown",
        "url": url_match.group(0) if url_match else None,
        "error": None if url_match else "could not parse deployer output",
    }
