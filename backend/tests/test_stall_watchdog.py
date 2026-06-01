import asyncio

import pytest

import app.orchestrator as orch_mod
from app.events import EventBus
from app.orchestrator import NUDGE_PREFIX, Orchestrator, OrchestratorConfig, StageTimeout, _run_stage
from app.store import Store


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
            # The sleep item is popped before sleeping, so a cancelled sleep
            # loses that item — scripts with items after a hang value won't
            # resume from where they left off.
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
    # Trickle a message every 0.01s forever: idle never trips (0.10s window),
    # but the 0.15s total backstop must fire.
    script = []
    for _ in range(100):
        script += [0.01, _FakeResult()]
    agen = _patch_query(monkeypatch, FakeAgen(script))
    with pytest.raises(StageTimeout) as ei:
        await _run_stage(
            stage="coder", prompt="p", options=None, bus=EventBus(),
            idle_timeout_s=0.10, total_timeout_s=0.15,
        )
    assert ei.value.kind == "total"
    assert agen.closed is True


# ---------------------------------------------------------------------------
# _run_stage_with_retry tests
# ---------------------------------------------------------------------------


def _capturing_bus():
    bus = EventBus()
    events = []
    bus.add(lambda e: events.append(e))
    return bus, events


def _orch(bus, tmp_path):
    return Orchestrator(bus=bus, store=Store(db_path=tmp_path / "t.db"),
                        config=OrchestratorConfig())


async def test_retry_succeeds_after_one_stall(monkeypatch, tmp_path):
    bus, events = _capturing_bus()
    orch = _orch(bus, tmp_path)
    calls = []

    async def fake_run_stage(*, stage, prompt, options, bus, idle_timeout_s, total_timeout_s):
        calls.append(prompt)
        if len(calls) == 1:
            raise StageTimeout(stage=stage, kind="idle")
        from app.orchestrator import StageResult
        return StageResult(text="ok")

    monkeypatch.setattr(orch_mod, "_run_stage", fake_run_stage)

    result = await orch._run_stage_with_retry(
        stage="coder", prompt="ORIGINAL", options=None, bus=bus,
        idle_timeout_s=1.0, total_timeout_s=2.0,
    )

    assert result.text == "ok"
    assert len(calls) == 2
    assert calls[1].startswith(NUDGE_PREFIX)        # retry got the nudge
    assert "ORIGINAL" in calls[1]                    # original brief preserved
    kinds = [e.kind for e in events]
    assert kinds.count("stage_stalled") == 1
    assert kinds.count("stage_retry") == 1


async def test_second_stall_raises_failed_timeout(monkeypatch, tmp_path):
    bus, events = _capturing_bus()
    orch = _orch(bus, tmp_path)

    async def always_stall(*, stage, prompt, options, bus, idle_timeout_s, total_timeout_s):
        raise StageTimeout(stage=stage, kind="idle")

    monkeypatch.setattr(orch_mod, "_run_stage", always_stall)

    with pytest.raises(orch_mod.PipelineFailure) as ei:
        await orch._run_stage_with_retry(
            stage="coder", prompt="p", options=None, bus=bus,
            idle_timeout_s=1.0, total_timeout_s=2.0,
        )
    assert "failed_coder_timeout" in str(ei.value)
    kinds = [e.kind for e in events]
    assert "stage_stalled" in kinds
    assert "stage_retry" in kinds
    assert not isinstance(ei.value, orch_mod.StageTimeout)


# ---------------------------------------------------------------------------
# Timeout config + per-stage idle selector tests
# ---------------------------------------------------------------------------


class _DummyOutcome:
    """Minimal stand-in for RunOutcome for stage-method unit calls."""
    run_id = 1
    total_cost_usd = 0.0
    status = "running"
    page_tsx_written = False

    def __init__(self):
        self.per_stage_cost: dict = {}


async def test_build_stages_get_longer_idle(monkeypatch, tmp_path):
    """Scaffolder/Reviewer (which run npm install/build) use the build idle
    window; other stages use the standard one."""
    bus, _ = _capturing_bus()
    cfg = OrchestratorConfig(
        stage_idle_timeout_s=111.0,
        stage_idle_timeout_build_s=222.0,
    )
    orch = Orchestrator(bus=bus, store=Store(db_path=tmp_path / "t.db"), config=cfg)

    seen = {}

    async def capture(*, stage, prompt, options, bus, idle_timeout_s, total_timeout_s):
        seen[stage] = idle_timeout_s
        from app.orchestrator import StageResult
        return StageResult(text="{}")

    monkeypatch.setattr(orch, "_run_stage_with_retry", capture)

    # Non-build stage: standard window.
    await orch._stage_coder_initial({"name": "x"}, tmp_path, _DummyOutcome())
    assert seen["coder"] == 111.0

    # Build stage: longer window. The scaffolder method runs sanity checks
    # AFTER the (captured) stage call and will raise PipelineFailure because
    # the tmp workspace has no real Next.js project — we only care that the
    # build idle window was selected, which is recorded before that raise.
    from app.orchestrator import PipelineFailure
    try:
        await orch._stage_scaffolder(tmp_path, _DummyOutcome())
    except PipelineFailure:
        pass
    assert seen["scaffolder"] == 222.0


def test_config_dict_roundtrips_timeouts(tmp_path):
    cfg = OrchestratorConfig(
        stage_idle_timeout_s=11.0,
        stage_idle_timeout_build_s=22.0,
        stage_total_timeout_s=33.0,
    )
    orch = Orchestrator(bus=EventBus(), store=Store(db_path=tmp_path / "t.db"), config=cfg)
    d = orch._config_dict()
    assert d["stage_idle_timeout_s"] == 11.0
    assert d["stage_idle_timeout_build_s"] == 22.0
    assert d["stage_total_timeout_s"] == 33.0
