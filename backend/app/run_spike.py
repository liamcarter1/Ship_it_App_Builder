"""Milestone 0 CLI entrypoint.

    python -m app.run_spike
    python -m app.run_spike "a landing page for a yoga studio"

Loads `.env` if present (so `ANTHROPIC_API_KEY` can live there), runs the
pipeline, and exits non-zero if `generated_app/app/page.tsx` wasn't written.
That file-based check is the deliberate pass/fail signal: it's
transcript-independent, so we don't get fooled by an agent that *says* it
wrote files but didn't.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .orchestrator import PipelineEvent, default_workspace, run_pipeline

DEFAULT_IDEA = "a calm one-page landing site for a tiny indie note-taking app"


# Compact ANSI labels so the CLI output is readable even without a TTY parser.
_LABEL_COLOURS = {
    "orchestrator": "\033[36m",  # cyan
    "planner": "\033[35m",       # magenta
    "coder": "\033[32m",         # green
    "tool": "\033[33m",          # yellow
    "system": "\033[90m",        # bright black
}
_RESET = "\033[0m"


def _print_event(event: PipelineEvent) -> None:
    colour = _LABEL_COLOURS.get(event.source, "")
    label = f"{colour}[{event.source}]{_RESET}" if colour else f"[{event.source}]"
    if event.kind == "agent_text":
        print(f"{label} {event.text}")
    elif event.kind == "tool_use":
        # event.text is the tool name; show the subagent target if Agent.
        target = ""
        if event.text == "Agent" and isinstance(event.meta.get("input"), dict):
            target = (
                event.meta["input"].get("subagent_type")
                or event.meta["input"].get("agent")
                or ""
            )
            target = f" -> {target}" if target else ""
        print(f"{label} tool: {event.text}{target}")
    elif event.kind == "tool_result":
        snippet = event.text.replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        marker = "ERR " if event.meta.get("is_error") else ""
        print(f"{label} result: {marker}{snippet}")
    elif event.kind == "pipeline_start":
        print(f"{label} {event.text}")
    elif event.kind == "pipeline_end":
        cost = event.meta.get("total_cost_usd")
        turns = event.meta.get("num_turns")
        suffix = []
        if turns is not None:
            suffix.append(f"turns={turns}")
        if cost is not None:
            suffix.append(f"cost=${cost:.4f}")
        tail = f" ({', '.join(suffix)})" if suffix else ""
        print(f"{label} pipeline finished{tail}")
    elif event.kind == "system":
        # System messages are noisy; print only their subtype label.
        print(f"{label} {event.text}")


async def _main_async(idea: str, workspace: Path) -> int:
    summary = await run_pipeline(idea=idea, workspace=workspace, on_event=_print_event)
    print()
    if summary["page_tsx_written"]:
        print(f"OK  wrote {summary['page_tsx_path']}")
        return 0
    print(f"FAIL  expected {summary['page_tsx_path']} to exist after the run")
    return 1


def main() -> None:
    # Find the repo's .env so this works whether you run `python -m app.run_spike`
    # from `backend/` or anywhere else.
    here = Path(__file__).resolve()
    for candidate in (here.parents[1] / ".env", here.parents[2] / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()  # fall back to CWD

    idea = " ".join(sys.argv[1:]).strip() or DEFAULT_IDEA
    workspace = default_workspace()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Not fatal — the user may be logged in via `claude` instead — but
        # worth a heads-up before we burn time on a 401.
        print(
            "[warn] ANTHROPIC_API_KEY is not set. The SDK will fall back to a "
            "logged-in `claude` CLI session if one exists; otherwise this will "
            "fail to authenticate.",
            file=sys.stderr,
        )

    exit_code = asyncio.run(_main_async(idea=idea, workspace=workspace))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
