"""GateBroker — coordinates human-in-the-loop approval pauses.

Single in-process broker. Each gate is keyed by `(run_id, name)`; the
orchestrator calls `open()` to register and await a decision, the API
calls `resolve()` to unblock it.

If the server restarts while a gate is pending, the asyncio task running
the orchestrator dies and the run is effectively orphaned (stays
"running" in the DB forever). Restart-resumable gates are a Milestone 4
concern; for M3 the broker lives only in process memory.

The CLI keeps its M1/M2 behaviour: `OrchestratorConfig.gate_broker=None`
short-circuits `_gate()` to auto-approve.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GateDecision:
    approve: bool
    notes: Optional[str] = None


class GateBroker:
    """Maps `(run_id, name)` to an `asyncio.Future[GateDecision]`."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, str], asyncio.Future[GateDecision]] = {}

    def open(self, run_id: int, name: str) -> asyncio.Future[GateDecision]:
        """Register a new pending gate and return its future. Replaces any
        stale future under the same key (shouldn't happen in normal flow)."""
        key = (run_id, name)
        old = self._pending.pop(key, None)
        if old is not None and not old.done():
            old.cancel()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GateDecision] = loop.create_future()
        self._pending[key] = future
        return future

    def resolve(self, run_id: int, name: str, decision: GateDecision) -> bool:
        """Return True if a pending gate was resolved, False if there was
        no matching future (already resolved, never opened, or wrong key)."""
        key = (run_id, name)
        future = self._pending.pop(key, None)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def cancel_all(self, run_id: int, *, notes: Optional[str] = None) -> list[str]:
        """Reject every pending gate for a run with `approve=False`.
        Returns the names cancelled. Used by `POST /api/runs/{id}/cancel`."""
        names: list[str] = []
        for key in list(self._pending.keys()):
            rid, name = key
            if rid != run_id:
                continue
            future = self._pending.pop(key)
            if not future.done():
                future.set_result(GateDecision(approve=False, notes=notes or "cancelled"))
            names.append(name)
        return names

    def pending_for(self, run_id: int) -> list[str]:
        return [
            name
            for (rid, name), fut in self._pending.items()
            if rid == run_id and not fut.done()
        ]
