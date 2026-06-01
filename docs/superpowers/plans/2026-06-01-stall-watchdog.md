# Stall Watchdog (in-stage inactivity timeout + auto-retry) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a stalled stage self-recover — bound every `query()` stage with an idle + total wall-clock deadline, then auto-retry the stage once fresh before failing cleanly, so a hung coding task never hangs the run forever.

**Architecture:** All stages funnel through the module-level `_run_stage` (`backend/app/orchestrator.py:91`). We add idle/total deadlines *inside* `_run_stage` via manual async-iterator stepping (`asyncio.wait_for(agen.__anext__(), ...)`), raising a new `StageTimeout`. A new `Orchestrator._run_stage_with_retry` wrapper owns the policy: on `StageTimeout`, emit `stage_stalled`/`stage_retry`, re-run the stage once with a nudge prefix, and on a second stall raise `PipelineFailure("failed_<stage>_timeout")` — which the existing `run()` error handler already turns into a terminal outcome. This fires in-process (never touches resume), so it is *retry fresh*, not *replay*.

**Tech Stack:** Python 3.10+, asyncio, Claude Agent SDK (`query`), pytest (`asyncio_mode = auto`), SQLite. Frontend: a one-line TypeScript union update.

**Spec:** `docs/superpowers/specs/2026-06-01-stall-watchdog-design.md`

---

## File Structure

- **Modify `backend/app/orchestrator.py`** — add `StageTimeout` exception; give `_run_stage` `idle_timeout_s`/`total_timeout_s` params + manual-iteration deadline + `aclose()` teardown; add `NUDGE_PREFIX`, `_idle_for()`, and `Orchestrator._run_stage_with_retry`; add 3 timeout fields to `OrchestratorConfig` + `_config_dict()`; switch the 6 stage call sites to the retry wrapper.
- **Modify `backend/app/events.py`** — add `stage_stalled` and `stage_retry` to the `EventKind` literal.
- **Modify `backend/app/server.py`** — read the 3 new timeout fields back in `_config_from_row` so a resumed run keeps them.
- **Create `backend/tests/test_stall_watchdog.py`** — unit tests for the deadline, the retry policy, teardown, and per-stage idle selection.
- **Modify `frontend/lib/types.ts`** — add the two new kinds to the `EventKind` union (type-completeness; no new panel).

---

## Task 1: `StageTimeout` exception + idle/total deadline in `_run_stage`

**Files:**
- Modify: `backend/app/orchestrator.py` (add exception after `PipelineFailure` at line 60-67; rewrite `_run_stage` body at lines 91-158)
- Test: `backend/tests/test_stall_watchdog.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_stall_watchdog.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_stall_watchdog.py -v`
Expected: FAIL — `ImportError: cannot import name 'StageTimeout'` (the symbol and the new `_run_stage` params don't exist yet).

- [ ] **Step 3: Add the `StageTimeout` exception**

In `backend/app/orchestrator.py`, immediately after the `PipelineFailure` class (after line 67), add:

```python
class StageTimeout(PipelineFailure):
    """A stage made no forward progress within its deadline.

    `kind` is "idle" (no SDK message for `idle_timeout_s`) or "total" (the
    absolute per-attempt wall-clock backstop). Subclasses PipelineFailure so
    an *unhandled* one (e.g. on the retry) is recorded as a clean terminal
    outcome rather than a traceback.
    """

    def __init__(self, *, stage: str, kind: str):
        self.stage = stage
        self.kind = kind
        super().__init__(f"stage {stage!r} stalled ({kind} timeout)")
```

- [ ] **Step 4: Rewrite `_run_stage` with the deadline**

Replace the signature and body of `_run_stage` (lines 91-158). New signature adds two params; the `async for` becomes manual iteration wrapped in `asyncio.wait_for`, with a total-deadline check and `aclose()` in `finally`. Keep all existing message-handling branches byte-for-byte.

```python
async def _run_stage(
    *,
    stage: str,
    prompt: str,
    options: ClaudeAgentOptions,
    bus: EventBus,
    idle_timeout_s: float = 180.0,
    total_timeout_s: float = 900.0,
) -> StageResult:
    """Run one stage to completion, streaming events into the bus.

    Bounded by two deadlines so a hung tool/build/model never stalls the run:
      * idle  — no new SDK message for `idle_timeout_s` (the true hang signal;
                a working stage keeps emitting tool_use/tool_result/agent_text).
      * total — absolute wall-clock backstop per attempt.
    On either, raise StageTimeout; `aclose()` in `finally` tears down the SDK
    session + child claude/npm/Bash process on every exit path.
    """
    await bus.emit(PipelineEvent(kind="stage_start", source=stage))

    result = StageResult()
    text_parts: list[str] = []

    loop = asyncio.get_event_loop()
    start = loop.time()
    agen = query(prompt=prompt, options=options)
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    agen.__anext__(), timeout=idle_timeout_s
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise StageTimeout(stage=stage, kind="idle")

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                        await bus.emit(
                            PipelineEvent(kind="agent_text", source=stage, text=block.text)
                        )
                    elif isinstance(block, ToolUseBlock):
                        await bus.emit(
                            PipelineEvent(
                                kind="tool_use",
                                source=stage,
                                text=block.name,
                                meta={"tool_use_id": block.id, "input": block.input},
                            )
                        )
            elif isinstance(message, UserMessage):
                for block in getattr(message, "content", []) or []:
                    if isinstance(block, ToolResultBlock):
                        await bus.emit(
                            PipelineEvent(
                                kind="tool_result",
                                source="tool",
                                text=str(block.content)[:500],
                                meta={
                                    "tool_use_id": block.tool_use_id,
                                    "is_error": bool(block.is_error),
                                },
                            )
                        )
            elif isinstance(message, SystemMessage):
                await bus.emit(
                    PipelineEvent(
                        kind="system",
                        source="system",
                        text=str(getattr(message, "subtype", "system")),
                    )
                )
            elif isinstance(message, ResultMessage):
                result.cost_usd = getattr(message, "total_cost_usd", None)
                result.turns = getattr(message, "num_turns", None)
                result.is_error = bool(getattr(message, "is_error", False))
                result.raw_result = getattr(message, "result", None)

            if loop.time() - start > total_timeout_s:
                raise StageTimeout(stage=stage, kind="total")
    finally:
        # Tear down the SDK session + any child claude/npm/Bash process on
        # EVERY exit (success, StageTimeout, or CancelledError from run-cancel).
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass

    result.text = "\n".join(text_parts).strip()
    await bus.emit(
        PipelineEvent(
            kind="stage_end",
            source=stage,
            text="error" if result.is_error else "ok",
            meta={"cost_usd": result.cost_usd, "turns": result.turns},
        )
    )
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_stall_watchdog.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/orchestrator.py backend/tests/test_stall_watchdog.py
git commit -m "feat: idle+total deadline inside _run_stage (StageTimeout)"
```

---

## Task 2: `stage_stalled`/`stage_retry` events + `_run_stage_with_retry` wrapper

**Files:**
- Modify: `backend/app/events.py:17-32` (EventKind literal)
- Modify: `backend/app/orchestrator.py` (add `NUDGE_PREFIX` near the top after imports; add `_run_stage_with_retry` method to `Orchestrator`, e.g. after `_run_stage`-using helpers, before `_stage_planner` at line 398)
- Test: `backend/tests/test_stall_watchdog.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_stall_watchdog.py`:

```python
from app.orchestrator import NUDGE_PREFIX, Orchestrator, OrchestratorConfig
from app.store import Store


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_stall_watchdog.py -k retry -v`
Expected: FAIL — `ImportError: cannot import name 'NUDGE_PREFIX'` / `AttributeError: ... '_run_stage_with_retry'`.

- [ ] **Step 3: Add the two event kinds**

In `backend/app/events.py`, add to the `EventKind` literal (after `"run_interrupted",` on line 30):

```python
    "stage_stalled",
    "stage_retry",
```

- [ ] **Step 4: Add `NUDGE_PREFIX` and the wrapper**

In `backend/app/orchestrator.py`, after the imports block (after line ~46, before `PipelineFailure`), add the module constant:

```python
NUDGE_PREFIX = (
    "NOTE: a previous attempt at this stage stalled with no forward progress. "
    "Do NOT start dev servers, watch-mode, or any long-running or interactive "
    "command — run only commands that terminate on their own. Work from the "
    "files already present in the workspace.\n\n"
)
```

Then add this method to the `Orchestrator` class (place it just before `_stage_planner`, around line 398):

```python
    async def _run_stage_with_retry(
        self,
        *,
        stage: str,
        prompt: str,
        options: ClaudeAgentOptions,
        bus: EventBus,
        idle_timeout_s: float,
        total_timeout_s: float,
    ) -> StageResult:
        """Run a stage, and on a stall (StageTimeout) abandon the wedged turn
        and re-run the stage ONCE fresh with a nudge brief. A second stall is
        re-raised as a clean PipelineFailure(`failed_<stage>_timeout`).

        This is *retry fresh*, not *resume*: the in-flight turn is discarded
        (its child process was torn down by `_run_stage`'s `aclose`), and the
        retry works from the workspace files already on disk.
        """
        try:
            return await _run_stage(
                stage=stage, prompt=prompt, options=options, bus=bus,
                idle_timeout_s=idle_timeout_s, total_timeout_s=total_timeout_s,
            )
        except StageTimeout as first:
            await bus.emit(
                PipelineEvent(
                    kind="stage_stalled",
                    source=stage,
                    text=f"{stage} stalled ({first.kind})",
                    meta={"timeout_kind": first.kind, "attempt": 1},
                )
            )
            await bus.emit(
                PipelineEvent(
                    kind="stage_retry",
                    source=stage,
                    text=f"retrying {stage}",
                    meta={"attempt": 2},
                )
            )
            try:
                return await _run_stage(
                    stage=stage, prompt=NUDGE_PREFIX + prompt, options=options,
                    bus=bus, idle_timeout_s=idle_timeout_s,
                    total_timeout_s=total_timeout_s,
                )
            except StageTimeout:
                raise PipelineFailure(f"failed_{stage}_timeout")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_stall_watchdog.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 6: Commit**

```bash
git add backend/app/events.py backend/app/orchestrator.py backend/tests/test_stall_watchdog.py
git commit -m "feat: _run_stage_with_retry wrapper + stage_stalled/stage_retry events"
```

---

## Task 3: Timeout config fields + per-stage wiring of all call sites

**Files:**
- Modify: `backend/app/orchestrator.py` — `OrchestratorConfig` (lines 182-200), `_config_dict()` (lines 224-234), add `_idle_for()` helper, and switch all 6 `_run_stage(...)` call sites to `self._run_stage_with_retry(...)`
- Modify: `backend/app/server.py:129-141` (`_config_from_row`)
- Test: `backend/tests/test_stall_watchdog.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_stall_watchdog.py`:

```python
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
        # Return parseable text where the caller needs it; reviewer/planner parse,
        # but we only assert idle here so a bare result is fine for coder.
        return StageResult(text="{}")

    monkeypatch.setattr(orch, "_run_stage_with_retry", capture)

    await orch._stage_coder_initial({"name": "x"}, tmp_path, _DummyOutcome())
    assert seen["coder"] == 111.0


def test_config_dict_roundtrips_timeouts():
    cfg = OrchestratorConfig(
        stage_idle_timeout_s=11.0,
        stage_idle_timeout_build_s=22.0,
        stage_total_timeout_s=33.0,
    )
    d = cfg._config_dict() if hasattr(cfg, "_config_dict") else None
    # _config_dict is a method on Orchestrator, not config; assert via Orchestrator:
    orch = Orchestrator(bus=EventBus(), store=Store(db_path=":memory:"), config=cfg)
    d = orch._config_dict()
    assert d["stage_idle_timeout_s"] == 11.0
    assert d["stage_idle_timeout_build_s"] == 22.0
    assert d["stage_total_timeout_s"] == 33.0
```

Add this tiny helper near the top of the test file (after the imports):

```python
class _DummyOutcome:
    """Minimal stand-in for RunOutcome for stage-method unit calls."""
    run_id = 1
    per_stage_cost: dict = {}
    total_cost_usd = 0.0
    status = "running"
    page_tsx_written = False
```

> Note: `Store(db_path=":memory:")` — if the Store requires a real path, use a `tmp_path` fixture instead. Check `backend/app/store.py` `Store.__init__`; if it rejects `:memory:`, change that one line to a `tmp_path / "t.db"` by making `test_config_dict_roundtrips_timeouts` take `tmp_path`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_stall_watchdog.py -k "idle or roundtrip" -v`
Expected: FAIL — `TypeError: OrchestratorConfig.__init__() got an unexpected keyword argument 'stage_idle_timeout_s'`.

- [ ] **Step 3: Add the config fields**

In `backend/app/orchestrator.py`, add to `OrchestratorConfig` (after `gate_timeout_s: float = 3600.0` at line 200):

```python
    # Inactivity watchdog (in-stage). A stage that emits no SDK message for
    # `stage_idle_timeout_s` (or `_build_s` for npm-running stages) — or runs
    # past `stage_total_timeout_s` total — is treated as stalled, killed, and
    # retried once with a nudge before failing as `failed_<stage>_timeout`.
    stage_idle_timeout_s: float = 180.0
    stage_idle_timeout_build_s: float = 420.0
    stage_total_timeout_s: float = 900.0
```

- [ ] **Step 4: Persist them in `_config_dict()`**

In `_config_dict()` (lines 224-234), add three entries to the returned dict:

```python
            "stage_idle_timeout_s": c.stage_idle_timeout_s,
            "stage_idle_timeout_build_s": c.stage_idle_timeout_build_s,
            "stage_total_timeout_s": c.stage_total_timeout_s,
```

- [ ] **Step 5: Add the `_idle_for()` helper**

In `backend/app/orchestrator.py`, add this method to `Orchestrator` (place it right before `_run_stage_with_retry`):

```python
    _BUILD_STAGES = ("scaffolder", "reviewer")

    def _idle_for(self, stage: str) -> float:
        """Build-running stages (npm install/build hold the Bash tool open
        with zero SDK messages for minutes) get the longer idle window."""
        if stage in self._BUILD_STAGES:
            return self.config.stage_idle_timeout_build_s
        return self.config.stage_idle_timeout_s
```

- [ ] **Step 6: Switch all 6 call sites to the retry wrapper**

In `backend/app/orchestrator.py`, replace each `result = await _run_stage(...)` with the wrapper, passing the per-stage idle + total. The six sites:

`_stage_planner` (line 404):
```python
        result = await self._run_stage_with_retry(
            stage="planner", prompt=prompt, options=options, bus=self.bus,
            idle_timeout_s=self._idle_for("planner"),
            total_timeout_s=self.config.stage_total_timeout_s,
        )
```

`_stage_scaffolder` (line 422):
```python
        result = await self._run_stage_with_retry(
            stage="scaffolder", prompt=prompt, options=options, bus=self.bus,
            idle_timeout_s=self._idle_for("scaffolder"),
            total_timeout_s=self.config.stage_total_timeout_s,
        )
```

`_stage_coder_initial` (line 448):
```python
        result = await self._run_stage_with_retry(
            stage="coder", prompt=prompt, options=options, bus=self.bus,
            idle_timeout_s=self._idle_for("coder"),
            total_timeout_s=self.config.stage_total_timeout_s,
        )
```

`_stage_reviewer` (line 569):
```python
        result = await self._run_stage_with_retry(
            stage="reviewer", prompt=prompt, options=options, bus=self.bus,
            idle_timeout_s=self._idle_for("reviewer"),
            total_timeout_s=self.config.stage_total_timeout_s,
        )
```

`_stage_coder_revise` (lines 587-589): the stage name is dynamic (`coder-rN`), which is not in `_BUILD_STAGES`, so it correctly gets the standard idle:
```python
        result = await self._run_stage_with_retry(
            stage=f"coder-r{round_num}", prompt=prompt, options=options, bus=self.bus,
            idle_timeout_s=self._idle_for(f"coder-r{round_num}"),
            total_timeout_s=self.config.stage_total_timeout_s,
        )
```

`_stage_deployer` (line 622):
```python
        result = await self._run_stage_with_retry(
            stage="deployer", prompt=prompt, options=options, bus=self.bus,
            idle_timeout_s=self._idle_for("deployer"),
            total_timeout_s=self.config.stage_total_timeout_s,
        )
```

- [ ] **Step 7: Read the fields back on resume**

In `backend/app/server.py`, in `_config_from_row` (lines 131-140), add three lines inside the `OrchestratorConfig(...)` call so a resumed run keeps any persisted overrides (defaults apply when absent):

```python
        stage_idle_timeout_s=raw.get("stage_idle_timeout_s", 180.0),
        stage_idle_timeout_build_s=raw.get("stage_idle_timeout_build_s", 420.0),
        stage_total_timeout_s=raw.get("stage_total_timeout_s", 900.0),
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_stall_watchdog.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 9: Commit**

```bash
git add backend/app/orchestrator.py backend/app/server.py backend/tests/test_stall_watchdog.py
git commit -m "feat: timeout config + per-stage idle wiring of retry wrapper"
```

---

## Task 4: Full regression + child-process teardown verification

The single most important correctness check (per the spec): `aclose()` must actually kill the `claude` child process **and** any `npm`/`Bash` it spawned, or a retry races an orphan against the same workspace.

**Files:** none changed unless verification fails (then `backend/app/orchestrator.py`).

- [ ] **Step 1: Run the entire backend suite (regression)**

Run: `cd backend && python -m pytest tests/ -v`
Expected: PASS — the prior 23 tests **plus** the new `test_stall_watchdog.py` tests, with no failures. If any prior test broke, fix it before continuing (the most likely break is a test calling `_run_stage` positionally — the new keyword-only params are fine, but confirm).

- [ ] **Step 2: Manually verify child-process teardown (empirical)**

This needs a real SDK run, so it is a manual check, not an automated test. With the backend running and `ANTHROPIC_API_KEY`/`claude` auth available, temporarily set a tiny idle timeout to force a stall, start a run from the dashboard or CLI, and watch the OS process list.

PowerShell, before/after a forced stall+retry:
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -in 'node.exe','npm.exe','claude.exe' } |
  Select-Object ProcessId, Name, CommandLine
```
Expected: after a `stage_stalled` → `stage_retry`, the **count of these processes does not grow** across the retry (the wedged turn's children were reaped by `aclose()`). If you see orphaned `node`/`npm`/`claude` accumulating, teardown is insufficient — do Step 3.

- [ ] **Step 3: (Only if Step 2 shows orphans) add explicit process-tree kill fallback**

If `aclose()` alone leaks children, capture the SDK client's child PID and kill its tree in `_run_stage`'s `finally`. The Agent SDK exposes the transport's subprocess; if a direct handle isn't reachable, record child PIDs spawned during the stage and, on `StageTimeout`, terminate each PID's tree (`psutil.Process(pid).children(recursive=True)` → `terminate()`/`kill()`). Add `psutil` to `backend/requirements.txt` only if this path is needed. Re-run Step 2 to confirm no orphans, then commit:

```bash
git add backend/app/orchestrator.py backend/requirements.txt
git commit -m "fix: explicit child-process-tree kill on stage timeout (teardown fallback)"
```

> If Step 2 passes (no orphans), record that in the commit message of Task 5 and skip Step 3 entirely.

---

## Task 5: Frontend event-type completeness + docs

**Files:**
- Modify: `frontend/lib/types.ts:4-19` (EventKind union)
- Modify: `CLAUDE.md` (Current status section)

- [ ] **Step 1: Add the two kinds to the frontend union**

In `frontend/lib/types.ts`, add to the `EventKind` union (after `| 'pipeline_end'` on line 16):

```typescript
  | 'stage_stalled'
  | 'stage_retry'
```

(No new panel needed for v1; `ActivityStream.tsx` will render them as ordinary log lines. The dashboard simply stops looking "frozen" because these events flow during a stall.)

- [ ] **Step 2: Build the frontend to confirm the union still type-checks**

Run: `cd frontend && npm run build`
Expected: build succeeds with no TypeScript errors.

- [ ] **Step 3: Record the feature in CLAUDE.md**

In `CLAUDE.md`, under "Current status", append a short note after the Milestone 5 entry:

```markdown
- **Hardening (DONE):** in-stage inactivity watchdog. `_run_stage` now bounds
  every stage with an idle + total wall-clock deadline (`StageTimeout`);
  `Orchestrator._run_stage_with_retry` abandons a stalled turn, tears down its
  child process, and retries the stage once fresh with a nudge brief before
  failing as `failed_<stage>_timeout`. Build-running stages (scaffolder,
  reviewer) get a longer idle window. New events: `stage_stalled`,
  `stage_retry`. Timeouts live on `OrchestratorConfig`
  (`stage_idle_timeout_s` / `_build_s` / `stage_total_timeout_s`), persisted
  in the run `config` JSON. This is *retry fresh*, not *replay* — it fires
  in-process and does not touch `resume_tail` (mid-LLM-stage resume remains a
  non-goal).
```

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/types.ts CLAUDE.md
git commit -m "feat: surface stage_stalled/stage_retry in frontend types + docs"
```

---

## Self-Review

**Spec coverage:**
- Idle + total deadline in `_run_stage` → Task 1. ✓
- `_run_stage_with_retry`, 1 retry, nudge, `failed_<stage>_timeout` → Task 2. ✓
- Applies to all model stages uniformly → Task 3, Step 6 (all 6 call sites). ✓
- Per-stage tuning (build stages longer idle) → Task 3 `_idle_for()` + `_BUILD_STAGES`. ✓
- Config fields persisted in JSON, no migration → Task 3 Steps 3-4-7. ✓
- New events `stage_stalled`/`stage_retry` → Task 2 Step 3, frontend Task 5. ✓
- Child-process teardown as first-class risk → Task 1 `finally: aclose()` + Task 4 manual verify + fallback. ✓
- Not-a-resume framing → documented in Task 2 wrapper docstring + Task 5 CLAUDE.md note. ✓
- Testing strategy (hang-from-start, idle-reset, total backstop, retry-succeeds, retry-also-stalls + aclose, regression) → Tasks 1-3 tests + Task 4 Step 1. ✓
- Out of scope (background watchdog, last_event_ts column, UI knobs) → not implemented, correct. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; commands have expected output. The only conditional is Task 4 Step 3, which is explicitly gated on Step 2 failing and gives the concrete `psutil` approach. ✓

**Type consistency:** `StageTimeout(stage=..., kind=...)` constructed identically in Task 1 impl and all tests; `_run_stage` keyword-only params (`idle_timeout_s`, `total_timeout_s`) match across `_run_stage`, `_run_stage_with_retry`, and all 6 call sites; `_idle_for`/`_BUILD_STAGES`/`NUDGE_PREFIX`/`_config_dict` names consistent throughout. ✓
