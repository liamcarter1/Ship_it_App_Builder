"""Milestone 0 orchestrator.

Opens a single `query()` against the Claude Agent SDK with the Planner and
Coder registered as subagents, then drives them with a small instruction
script. Emits structured `PipelineEvent`s for every message we see, labelled
by which agent produced it (orchestrator vs. planner vs. coder), so the
caller can stream them to a CLI now and to SSE later.

Key SDK details respected here:

- "Agent" MUST appear in `allowed_tools` — without it, delegation silently
  doesn't happen and the orchestrator does the work itself. This is the #1
  failure mode for new Agent SDK code.
- Subagent context is isolated: a subagent only sees its own system prompt
  plus the brief the orchestrator passes in the Agent tool call. So the
  orchestrator's user prompt has to make the *plan of action* explicit
  ("first call planner, then call coder with the spec").
- `parent_tool_use_id` on `AssistantMessage` tells us whether a message came
  from inside a subagent (non-empty) or from the orchestrator (None). We use
  it purely for labelling — the SDK already handles routing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .agents import build_coder, build_planner

EventKind = Literal[
    "pipeline_start",
    "agent_text",
    "tool_use",
    "tool_result",
    "system",
    "pipeline_end",
]


@dataclass
class PipelineEvent:
    """A single structured event from the pipeline.

    `source` is the logical agent name ("orchestrator", "planner", "coder").
    For Milestone 0 we infer it from `parent_tool_use_id` plus a map we
    maintain as we observe `ToolUseBlock`s for the Agent tool.
    """

    kind: EventKind
    source: str
    text: str = ""
    meta: dict = field(default_factory=dict)


OnEvent = Callable[[PipelineEvent], Awaitable[None] | None]


ORCHESTRATOR_PROMPT_TEMPLATE = """\
You are the Orchestrator for the Ship-It app factory pipeline (Milestone 0).

You have two subagents available via the Agent tool:
  • "planner" — think-only; turns an idea into a JSON spec.
  • "coder"   — has Read/Write/Edit/Bash; writes files into the current
                working directory (the project workspace).

Your job is to drive them in sequence. Do NOT write any code yourself; your
only actions should be Agent tool calls.

Step 1 — Delegate to the **planner**:
  Pass the following idea verbatim as its prompt, asking it to return the
  JSON spec exactly as described in its own system prompt:

    IDEA: {idea!r}

Step 2 — Delegate to the **coder**:
  Pass the planner's full JSON spec to the coder, telling it to implement
  the spec as `app/page.tsx`, `app/layout.tsx`, and `app/globals.css` in the
  current working directory. Include the JSON block verbatim in the brief
  (the coder cannot see your conversation with the planner).

Step 3 — Summarise:
  After the coder reports back, reply with a one-paragraph summary of what
  was planned and what was written, then STOP.

Hard rules:
  • Do not call Read, Write, Edit, or Bash yourself — only Agent.
  • Do not loop or retry. One pass through planner -> coder is enough.
"""


def _build_options(workspace: Path, planner_model: str | None, coder_model: str | None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        agents={
            "planner": build_planner(model=planner_model),
            "coder": build_coder(model=coder_model),
        },
        # "Agent" is the critical one — without it, delegation silently fails.
        # The others are the union of what the Coder needs once delegated to.
        allowed_tools=["Agent", "Read", "Write", "Edit", "Bash"],
        permission_mode="acceptEdits",
        cwd=str(workspace),
    )


async def _emit(on_event: OnEvent | None, event: PipelineEvent) -> None:
    if on_event is None:
        return
    result = on_event(event)
    if hasattr(result, "__await__"):
        await result  # type: ignore[func-returns-value]


def _source_for(message: AssistantMessage, agent_by_tool_use: dict[str, str]) -> str:
    """Resolve the logical agent name for an AssistantMessage.

    The SDK marks messages from a subagent with a non-empty
    `parent_tool_use_id`. We map that id back to the subagent name we saw on
    the Agent tool call that started it. Anything else is the orchestrator.
    """
    parent = getattr(message, "parent_tool_use_id", None)
    if parent and parent in agent_by_tool_use:
        return agent_by_tool_use[parent]
    return "orchestrator"


async def run_pipeline(
    idea: str,
    workspace: Path,
    *,
    on_event: OnEvent | None = None,
    planner_model: str | None = None,
    coder_model: str | None = None,
    max_turns: int = 30,
) -> dict:
    """Run the Milestone 0 pipeline end to end.

    Args:
        idea: The one-line product idea.
        workspace: Directory the Coder is allowed to write into. Created if
            missing. This becomes the SDK `cwd`.
        on_event: Optional callback invoked for every PipelineEvent. May be
            sync or async.
        planner_model / coder_model: Optional per-worker model overrides.
        max_turns: Safety cap on orchestrator turns within the single
            `query()` call. Keeps a misbehaving model from looping forever.

    Returns:
        A small dict with summary info, including whether
        `app/page.tsx` was written (the pipeline's pass/fail signal).
    """
    workspace.mkdir(parents=True, exist_ok=True)

    options = _build_options(workspace, planner_model, coder_model)

    # The SDK doesn't have a single typed field for "max orchestrator turns"
    # across all versions, so we also pass it via options when available.
    # 0.2.x exposes it as a constructor kwarg on ClaudeAgentOptions.
    try:
        options.max_turns = max_turns  # type: ignore[attr-defined]
    except Exception:
        pass

    await _emit(
        on_event,
        PipelineEvent(
            kind="pipeline_start",
            source="orchestrator",
            text=f"Starting pipeline for idea: {idea!r}",
            meta={"workspace": str(workspace)},
        ),
    )

    # Track which Agent tool_use_id corresponds to which subagent so we can
    # label downstream messages from inside that subagent.
    agent_by_tool_use: dict[str, str] = {}

    prompt = ORCHESTRATOR_PROMPT_TEMPLATE.format(idea=idea)

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            source = _source_for(message, agent_by_tool_use)
            for block in message.content:
                if isinstance(block, TextBlock):
                    await _emit(
                        on_event,
                        PipelineEvent(kind="agent_text", source=source, text=block.text),
                    )
                elif isinstance(block, ToolUseBlock):
                    # Remember the agent name if this is a delegation, so we
                    # can label messages that come back from inside it.
                    if block.name == "Agent":
                        sub_name = ""
                        if isinstance(block.input, dict):
                            sub_name = (
                                block.input.get("subagent_type")
                                or block.input.get("agent")
                                or block.input.get("name")
                                or ""
                            )
                        if sub_name:
                            agent_by_tool_use[block.id] = sub_name
                    await _emit(
                        on_event,
                        PipelineEvent(
                            kind="tool_use",
                            source=source,
                            text=block.name,
                            meta={"tool_use_id": block.id, "input": block.input},
                        ),
                    )
        elif isinstance(message, UserMessage):
            # UserMessages here are usually tool results being fed back into
            # the model. Surface them so the event log is complete.
            for block in getattr(message, "content", []) or []:
                if isinstance(block, ToolResultBlock):
                    await _emit(
                        on_event,
                        PipelineEvent(
                            kind="tool_result",
                            source="tool",
                            text=str(block.content)[:500],
                            meta={"tool_use_id": block.tool_use_id, "is_error": bool(block.is_error)},
                        ),
                    )
        elif isinstance(message, SystemMessage):
            await _emit(
                on_event,
                PipelineEvent(kind="system", source="system", text=str(getattr(message, "subtype", "system"))),
            )
        elif isinstance(message, ResultMessage):
            await _emit(
                on_event,
                PipelineEvent(
                    kind="pipeline_end",
                    source="orchestrator",
                    text=getattr(message, "result", "") or "done",
                    meta={
                        "is_error": bool(getattr(message, "is_error", False)),
                        "duration_ms": getattr(message, "duration_ms", None),
                        "num_turns": getattr(message, "num_turns", None),
                        "total_cost_usd": getattr(message, "total_cost_usd", None),
                    },
                ),
            )

    page_tsx = workspace / "app" / "page.tsx"
    return {
        "idea": idea,
        "workspace": str(workspace),
        "page_tsx_written": page_tsx.exists(),
        "page_tsx_path": str(page_tsx),
    }


def default_workspace() -> Path:
    """`<repo>/generated_app`. Kept here so the CLI and any future caller agree."""
    return Path(os.environ.get("SHIPIT_WORKSPACE", Path(__file__).resolve().parents[2] / "generated_app"))
