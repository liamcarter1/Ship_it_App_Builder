"""GateBroker — DB-backed coordination for human-in-the-loop approval pauses.

Gate state lives in the `gates` table (via `Store`), not in process memory, so
it survives a server restart and is visible across workers. The orchestrator
`wait()`s for a decision by polling the table — the same mechanism the SSE
event stream uses. The API `resolve()`s a gate by writing the decision.

The CLI keeps its M1/M2 behaviour: `OrchestratorConfig.gate_broker=None`
short-circuits `_gate()` to auto-approve.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .store import Store


@dataclass(frozen=True)
class GateDecision:
    approve: bool
    notes: Optional[str] = None


class GateBroker:
    """Async wrapper over the `gates` table."""

    def __init__(self, store: Store, *, poll_interval: float = 0.25) -> None:
        self._store = store
        self._poll_interval = poll_interval

    def open(self, run_id: int, name: str, *, payload: Optional[dict] = None) -> None:
        self._store.open_gate(run_id, name, payload=payload)

    async def wait(self, run_id: int, name: str, *, timeout: float) -> GateDecision:
        """Poll until the gate is decided; raise asyncio.TimeoutError past
        `timeout`. Uses the loop's monotonic clock so it's immune to wall-clock
        jumps."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            gate = self._store.get_gate(run_id, name)
            if gate is not None and gate["status"] != "open":
                return GateDecision(
                    approve=(gate["status"] == "approved"),
                    notes=gate["notes"],
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.sleep(min(self._poll_interval, remaining))

    def resolve(self, run_id: int, name: str, decision: GateDecision) -> bool:
        return self._store.resolve_gate(
            run_id, name, approve=decision.approve, notes=decision.notes
        )

    def cancel_all(self, run_id: int, *, notes: Optional[str] = None) -> list[str]:
        return self._store.cancel_open_gates(run_id, notes=notes)

    def pending_for(self, run_id: int) -> list[str]:
        return self._store.pending_gates(run_id)
