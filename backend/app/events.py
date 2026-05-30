"""Event bus for the Ship-It pipeline.

Every stage in the orchestrator (Planner, Scaffolder, Coder, Reviewer,
Deployer) emits structured `PipelineEvent`s instead of printing. Multiple
listeners can subscribe to the same stream: in Milestone 1 we attach the CLI
printer plus a SQLite recorder; in Milestone 2 the SSE handler will be a
third listener publishing the same events to the dashboard.

Listeners may be sync or async. The bus awaits the result if it's a coroutine.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

EventKind = Literal[
    "pipeline_start",
    "stage_start",
    "stage_end",
    "agent_text",
    "tool_use",
    "tool_result",
    "system",
    "review_verdict",
    "deploy_url",
    "pipeline_end",
]


@dataclass
class PipelineEvent:
    kind: EventKind
    source: str  # logical role: "orchestrator" | "planner" | "scaffolder" | "coder" | "reviewer" | "deployer" | "tool" | "system"
    text: str = ""
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


Listener = Callable[[PipelineEvent], "Awaitable[None] | None"]


class EventBus:
    """Tiny pub/sub for pipeline events.

    Order of listeners is preserved; each is awaited in turn so a slow
    listener can't be silently skipped. If a listener raises, the bus
    re-raises after letting earlier listeners observe the event.
    """

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def add(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def emit(self, event: PipelineEvent) -> None:
        for listener in self._listeners:
            result = listener(event)
            if result is not None and hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
