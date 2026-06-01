import asyncio

import pytest

import app.orchestrator as orch_mod
from app.events import EventBus
from app.orchestrator import StageTimeout, _run_stage


class _FakeResult:
    """Stand-in for the SDK ResultMessage (only attrs _run_stage reads)."""
    total_cost_usd = 0.01
    num_turns = 1
    is_error = False
    result = "done"


class FakeAgen:
    """A fake async iterator standing in for `query(...)`.

    `script` is a list of either:
      - a message object  -> yielded immediately
      - a float           -> sleep that long before yielding the NEXT item
                             (use a long sleep to simulate a hang)
    `aclose()` records that teardown happened.
    """

    def __init__(self, script):
        self._script = list(script)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._script:
            item = self._script.pop(0)
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)
                continue
            return item
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


def _patch_query(monkeypatch, agen):
    """Make `query(prompt=..., options=...)` return our fake iterator."""
    monkeypatch.setattr(orch_mod, "query", lambda **kwargs: agen)
    return agen


async def test_run_stage_idle_timeout_raises(monkeypatch):
    # Generator hangs forever before yielding anything.
    agen = _patch_query(monkeypatch, FakeAgen([999.0]))
    with pytest.raises(StageTimeout) as ei:
        await _run_stage(
            stage="coder", prompt="p", options=None, bus=EventBus(),
            idle_timeout_s=0.05, total_timeout_s=10.0,
        )
    assert ei.value.kind == "idle"
    assert ei.value.stage == "coder"
    assert agen.closed is True  # aclose() ran in finally


async def test_run_stage_idle_clock_resets_then_trips(monkeypatch):
    # Yields a message quickly, then hangs -> idle clock must reset, then trip.
    msg = _FakeResult()
    agen = _patch_query(monkeypatch, FakeAgen([msg, 999.0]))
    with pytest.raises(StageTimeout) as ei:
        await _run_stage(
            stage="coder", prompt="p", options=None, bus=EventBus(),
            idle_timeout_s=0.05, total_timeout_s=10.0,
        )
    assert ei.value.kind == "idle"
    assert agen.closed is True


async def test_run_stage_total_backstop_trips(monkeypatch):
    # Trickle a message every 0.02s forever: idle never trips (0.05s window),
    # but the 0.1s total backstop must fire.
    script = []
    for _ in range(100):
        script += [0.02, _FakeResult()]
    agen = _patch_query(monkeypatch, FakeAgen(script))
    with pytest.raises(StageTimeout) as ei:
        await _run_stage(
            stage="coder", prompt="p", options=None, bus=EventBus(),
            idle_timeout_s=0.05, total_timeout_s=0.1,
        )
    assert ei.value.kind == "total"
    assert agen.closed is True
