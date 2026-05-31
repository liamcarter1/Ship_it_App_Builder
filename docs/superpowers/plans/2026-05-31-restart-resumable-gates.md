# Restart-resumable, DB-backed gates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move gate state from in-process `asyncio.Future`s into a SQLite `gates` table, have the orchestrator poll it for decisions, and auto-resume runs paused at the code/deploy gate after a server restart.

**Architecture:** A new `gates` table and a `config` JSON column on `runs` make gate state and the run config durable. `GateBroker` becomes a thin async wrapper over `Store` that polls the table (mirroring the SSE event poller). On startup the server sweeps `running` runs: those paused at the `code`/`deploy` gate resume via a new `Orchestrator.resume_tail()`; the rest are marked `interrupted`.

**Tech Stack:** Python 3.10+, SQLite (`sqlite3`), FastAPI, asyncio, `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-05-31-restart-resumable-gates-design.md`

---

## File Structure

- **Create** `backend/requirements-dev.txt` — pytest dev deps.
- **Create** `backend/pytest.ini` — pytest config (asyncio mode, pythonpath).
- **Create** `backend/tests/test_store_gates.py` — store gate + config tests.
- **Create** `backend/tests/test_store_recovery.py` — store recovery-method tests.
- **Create** `backend/tests/test_gate_broker.py` — `GateBroker` poll/timeout tests.
- **Create** `backend/tests/test_resume.py` — `Orchestrator.resume_tail` + disposition tests.
- **Modify** `backend/app/events.py` — add two `EventKind`s.
- **Modify** `backend/app/store.py` — `config` column, `gates` table, gate + recovery methods.
- **Modify** `backend/app/gates.py` — rewrite `GateBroker` over `Store`.
- **Modify** `backend/app/orchestrator.py` — `_gate` reopen flag, `_finish_after_code_gate`, `resume_tail`, config persistence on the CLI path.
- **Modify** `backend/app/server.py` — broker wiring, config persistence, startup recovery sweep, `_resume_disposition`, drop the gate-cancel in the run-task `finally`.
- **Modify** `CLAUDE.md`, `SHIP-IT_BUILD_PLAN.md`, `LEARNING.md` — status + walkthrough updates.

---

## Task 1: Test harness

**Files:**
- Create: `backend/requirements-dev.txt`
- Create: `backend/pytest.ini`
- Create: `backend/tests/test_smoke.py`

- [ ] **Step 1: Create dev requirements**

`backend/requirements-dev.txt`:
```
-r requirements.txt
pytest>=8.0,<9.0
pytest-asyncio>=0.23,<0.25
```

- [ ] **Step 2: Create pytest config**

`backend/pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
pythonpath = .
```

- [ ] **Step 3: Create a smoke test**

`backend/tests/test_smoke.py`:
```python
from app.store import Store


def test_app_package_imports(tmp_path):
    store = Store(db_path=tmp_path / "smoke.db")
    assert store.list_runs() == []
```

- [ ] **Step 4: Install deps and run it**

Run (from `backend/`, venv active):
```
pip install -r requirements-dev.txt
python -m pytest tests/test_smoke.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/requirements-dev.txt backend/pytest.ini backend/tests/test_smoke.py
git commit -m "test: add pytest harness for backend"
```

---

## Task 2: Event kinds

**Files:**
- Modify: `backend/app/events.py:17-30`

- [ ] **Step 1: Add the two new kinds**

In `backend/app/events.py`, change the `EventKind` Literal to include the two new kinds (add them before `"pipeline_end"`):
```python
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
    "gate_open",
    "gate_decision",
    "pipeline_resumed",
    "run_interrupted",
    "pipeline_end",
]
```

- [ ] **Step 2: Verify import still works**

Run (from `backend/`):
```
python -c "from app.events import PipelineEvent; print(PipelineEvent(kind='pipeline_resumed', source='orchestrator'))"
```
Expected: prints a `PipelineEvent(...)` line, no error.

- [ ] **Step 3: Commit**

```bash
git add backend/app/events.py
git commit -m "feat: add pipeline_resumed and run_interrupted event kinds"
```

---

## Task 3: Store — `config` column

**Files:**
- Modify: `backend/app/store.py` (SCHEMA, `connect`, `create_run`, add `get_run_config`)
- Test: `backend/tests/test_store_gates.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_store_gates.py`:
```python
from app.store import Store


def _store(tmp_path):
    return Store(db_path=tmp_path / "t.db")


def test_config_round_trips(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(
        idea="i",
        workspace=tmp_path / "ws",
        config={"deploy": True, "deployer_model": "haiku", "max_review_rounds": 2},
    )
    cfg = store.get_run_config(rid)
    assert cfg["deploy"] is True
    assert cfg["deployer_model"] == "haiku"
    assert cfg["max_review_rounds"] == 2


def test_config_defaults_to_empty(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    assert store.get_run_config(rid) == {}
```

- [ ] **Step 2: Run it to verify failure**

Run: `python -m pytest tests/test_store_gates.py -v`
Expected: FAIL — `create_run() got an unexpected keyword argument 'config'`.

- [ ] **Step 3: Add the `config` column to the runs schema**

In `backend/app/store.py`, in the `SCHEMA` string, add a `config TEXT` column to the `runs` table (after the `error TEXT` line, before the closing `);`):
```sql
    error TEXT,
    config TEXT
```

- [ ] **Step 4: Add an idempotent migration in `connect()`**

In `backend/app/store.py`, replace the body of `connect()` so it runs a migration after the schema. The current `connect()` does `conn.executescript(SCHEMA); yield conn; conn.commit()`. Change to:
```python
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()
```
And add this module-level function just above `connect()`:
```python
def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that CREATE TABLE IF NOT EXISTS can't add to existing DBs."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "config" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN config TEXT")
```

- [ ] **Step 5: Thread `config` through `create_run` and add `get_run_config`**

In `backend/app/store.py`, replace `create_run` with:
```python
    def create_run(
        self, idea: str, workspace: Path, *, config: Optional[dict] = None
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO runs (idea, workspace, started_at, config) VALUES (?, ?, ?, ?)",
                (
                    idea,
                    str(workspace),
                    time.time(),
                    json.dumps(config) if config is not None else None,
                ),
            )
            return int(cur.lastrowid)
```
And add this method (e.g. just after `get_run`):
```python
    def get_run_config(self, run_id: int) -> dict:
        row = self.get_run(run_id)
        if row is None or row["config"] is None:
            return {}
        return json.loads(row["config"])
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_store_gates.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/app/store.py backend/tests/test_store_gates.py
git commit -m "feat: persist run config in a JSON column on runs"
```

---

## Task 4: Store — `gates` table + gate methods

**Files:**
- Modify: `backend/app/store.py` (SCHEMA, add gate methods)
- Test: `backend/tests/test_store_gates.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_store_gates.py`:
```python
def test_open_and_pending(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    store.open_gate(rid, "code", payload={"hello": "world"})
    assert store.pending_gates(rid) == ["code"]
    gate = store.get_gate(rid, "code")
    assert gate["status"] == "open"


def test_resolve_gate(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    store.open_gate(rid, "code")
    assert store.resolve_gate(rid, "code", approve=True, notes="lgtm") is True
    assert store.pending_gates(rid) == []
    gate = store.get_gate(rid, "code")
    assert gate["status"] == "approved"
    assert gate["notes"] == "lgtm"


def test_resolve_missing_or_decided_returns_false(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    assert store.resolve_gate(rid, "code", approve=True) is False  # never opened
    store.open_gate(rid, "code")
    assert store.resolve_gate(rid, "code", approve=True) is True
    assert store.resolve_gate(rid, "code", approve=False) is False  # already decided


def test_open_gate_is_idempotent_while_open(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    store.open_gate(rid, "code", payload={"v": 1})
    store.open_gate(rid, "code", payload={"v": 2})  # no-op: stays open
    assert store.pending_gates(rid) == ["code"]


def test_cancel_open_gates(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    store.open_gate(rid, "code")
    store.open_gate(rid, "deploy")
    cancelled = store.cancel_open_gates(rid, notes="bye")
    assert set(cancelled) == {"code", "deploy"}
    assert store.pending_gates(rid) == []
    assert store.get_gate(rid, "code")["status"] == "rejected"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_store_gates.py -v`
Expected: FAIL — `'Store' object has no attribute 'open_gate'`.

- [ ] **Step 3: Add the `gates` table to SCHEMA**

In `backend/app/store.py`, append to the `SCHEMA` string (after the `events_run_id_idx` index):
```sql
CREATE TABLE IF NOT EXISTS gates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT,
    payload TEXT,
    opened_at REAL NOT NULL,
    decided_at REAL,
    UNIQUE(run_id, name)
);

CREATE INDEX IF NOT EXISTS gates_run_idx ON gates(run_id, status);
```

- [ ] **Step 4: Add the gate methods**

In `backend/app/store.py`, add these methods to `Store` (e.g. after `get_run_config`):
```python
    # --- gates (restart-resumable, DB-backed) ------------------------------

    def open_gate(
        self, run_id: int, name: str, *, payload: Optional[dict] = None
    ) -> None:
        """Register `(run_id, name)` as an open gate. Idempotent while open:
        re-opening an already-open row is a no-op (resume relies on this)."""
        payload_json = json.dumps(payload, default=str) if payload is not None else None
        now = time.time()
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM gates WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO gates (run_id, name, status, payload, opened_at) "
                    "VALUES (?, ?, 'open', ?, ?)",
                    (run_id, name, payload_json, now),
                )
            elif row["status"] != "open":
                conn.execute(
                    "UPDATE gates SET status='open', notes=NULL, decided_at=NULL, "
                    "payload=?, opened_at=? WHERE run_id=? AND name=?",
                    (payload_json, now, run_id, name),
                )

    def get_gate(self, run_id: int, name: str) -> Optional[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return conn.execute(
                "SELECT * FROM gates WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()

    def resolve_gate(
        self, run_id: int, name: str, *, approve: bool, notes: Optional[str] = None
    ) -> bool:
        """Decide an open gate. Returns True iff a row was 'open' and got
        flipped (False if missing or already decided)."""
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE gates SET status=?, notes=?, decided_at=? "
                "WHERE run_id=? AND name=? AND status='open'",
                (
                    "approved" if approve else "rejected",
                    notes,
                    time.time(),
                    run_id,
                    name,
                ),
            )
            return cur.rowcount > 0

    def cancel_open_gates(
        self, run_id: int, *, notes: Optional[str] = None
    ) -> list[str]:
        with connect(self.db_path) as conn:
            names = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM gates WHERE run_id=? AND status='open'", (run_id,)
                ).fetchall()
            ]
            if names:
                conn.execute(
                    "UPDATE gates SET status='rejected', notes=?, decided_at=? "
                    "WHERE run_id=? AND status='open'",
                    (notes or "cancelled", time.time(), run_id),
                )
            return names

    def pending_gates(self, run_id: int) -> list[str]:
        with connect(self.db_path) as conn:
            return [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM gates WHERE run_id=? AND status='open' ORDER BY id",
                    (run_id,),
                ).fetchall()
            ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_store_gates.py -v`
Expected: all passed (7 tests total in the file now).

- [ ] **Step 6: Commit**

```bash
git add backend/app/store.py backend/tests/test_store_gates.py
git commit -m "feat: add DB-backed gates table and store gate methods"
```

---

## Task 5: Store — recovery methods

**Files:**
- Modify: `backend/app/store.py` (add `running_runs`, `claim_run_for_resume`, `reset_stale_resuming`, `mark_interrupted`)
- Test: `backend/tests/test_store_recovery.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_store_recovery.py`:
```python
from app.store import Store


def _store(tmp_path):
    return Store(db_path=tmp_path / "t.db")


def test_running_runs_lists_only_running(tmp_path):
    store = _store(tmp_path)
    a = store.create_run(idea="a", workspace=tmp_path / "wa")
    b = store.create_run(idea="b", workspace=tmp_path / "wb")
    store.finish_run(b, status="built")
    ids = [r["id"] for r in store.running_runs()]
    assert a in ids and b not in ids


def test_claim_is_atomic(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    assert store.claim_run_for_resume(rid) is True
    assert store.get_run(rid)["status"] == "resuming"
    assert store.claim_run_for_resume(rid) is False  # already claimed


def test_reset_stale_resuming(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    store.claim_run_for_resume(rid)
    assert store.reset_stale_resuming() == 1
    assert store.get_run(rid)["status"] == "running"


def test_mark_interrupted(tmp_path):
    store = _store(tmp_path)
    rid = store.create_run(idea="i", workspace=tmp_path / "ws")
    store.mark_interrupted(rid, error="server restarted")
    row = store.get_run(rid)
    assert row["status"] == "interrupted"
    assert row["error"] == "server restarted"
    assert row["finished_at"] is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_store_recovery.py -v`
Expected: FAIL — `'Store' object has no attribute 'running_runs'`.

- [ ] **Step 3: Add the recovery methods**

In `backend/app/store.py`, add to `Store` (e.g. after `pending_gates`):
```python
    # --- restart recovery --------------------------------------------------

    def running_runs(self) -> list[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM runs WHERE status='running' ORDER BY id"
                ).fetchall()
            )

    def claim_run_for_resume(self, run_id: int) -> bool:
        """Atomically flip a 'running' run to 'resuming'. Returns True only
        for the caller that won the flip (guards against double-resume)."""
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE runs SET status='resuming' WHERE id=? AND status='running'",
                (run_id,),
            )
            return cur.rowcount > 0

    def reset_stale_resuming(self) -> int:
        """Return any 'resuming' runs (left by a crash mid-resume) to
        'running' so the next sweep reconsiders them. Returns the count."""
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE runs SET status='running' WHERE status='resuming'"
            )
            return cur.rowcount

    def mark_interrupted(self, run_id: int, *, error: str = "server restarted") -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET status='interrupted', finished_at=?, error=? WHERE id=?",
                (time.time(), error, run_id),
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_store_recovery.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/store.py backend/tests/test_store_recovery.py
git commit -m "feat: add store restart-recovery helpers"
```

---

## Task 6: Rewrite `GateBroker` over `Store`

**Files:**
- Modify: `backend/app/gates.py` (full rewrite of `GateBroker`; keep `GateDecision`)
- Test: `backend/tests/test_gate_broker.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_gate_broker.py`:
```python
import asyncio

import pytest

from app.gates import GateBroker, GateDecision
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "t.db")


@pytest.fixture
def run_id(store, tmp_path):
    return store.create_run(idea="i", workspace=tmp_path / "ws")


async def test_round_trip(store, run_id):
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(run_id, "code", payload={"hi": 1})
    assert broker.pending_for(run_id) == ["code"]

    async def approve_soon():
        await asyncio.sleep(0.03)
        assert broker.resolve(run_id, "code", GateDecision(approve=True, notes="ok"))

    asyncio.create_task(approve_soon())
    decision = await broker.wait(run_id, "code", timeout=2.0)
    assert decision.approve is True
    assert decision.notes == "ok"
    assert broker.pending_for(run_id) == []


async def test_wait_times_out(store, run_id):
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(run_id, "code")
    with pytest.raises(asyncio.TimeoutError):
        await broker.wait(run_id, "code", timeout=0.05)


def test_resolve_missing_returns_false(store, run_id):
    broker = GateBroker(store)
    assert broker.resolve(run_id, "code", GateDecision(approve=True)) is False


def test_cancel_all(store, run_id):
    broker = GateBroker(store)
    broker.open(run_id, "code")
    broker.open(run_id, "deploy")
    assert set(broker.cancel_all(run_id, notes="x")) == {"code", "deploy"}
    assert broker.pending_for(run_id) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_gate_broker.py -v`
Expected: FAIL — `GateBroker.__init__()` doesn't accept a `Store` (current signature takes no args), or `wait`/`open` mismatch.

- [ ] **Step 3: Rewrite `GateBroker`**

Replace the entire contents of `backend/app/gates.py` with the following (the module docstring is updated to describe the DB-backed design; `GateDecision` is unchanged):
```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_gate_broker.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/gates.py backend/tests/test_gate_broker.py
git commit -m "feat: make GateBroker DB-backed with poll-based wait"
```

---

## Task 7: Orchestrator — reopen flag, tail extraction, `resume_tail`

**Files:**
- Modify: `backend/app/orchestrator.py` (`run` config persistence + tail extraction; `_gate` reopen flag; new `_finish_after_code_gate`, `resume_tail`, `_config_dict`)
- Test: `backend/tests/test_resume.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_resume.py`:
```python
import asyncio

import pytest

from app.events import EventBus
from app.gates import GateBroker, GateDecision
from app.orchestrator import Orchestrator, OrchestratorConfig
from app.store import Store

GATED = ("spec", "code", "deploy")


@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "t.db")


async def test_resume_tail_built(store, tmp_path):
    rid = store.create_run(idea="i", workspace=tmp_path / "ws", config={"deploy": False})
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(rid, "code", payload={"workspace": str(tmp_path / "ws")})
    store.claim_run_for_resume(rid)

    config = OrchestratorConfig(deploy=False, gate_broker=broker, gated_stages=GATED)
    orch = Orchestrator(bus=EventBus(), store=store, config=config)

    async def approve_soon():
        await asyncio.sleep(0.03)
        broker.resolve(rid, "code", GateDecision(approve=True))

    asyncio.create_task(approve_soon())
    outcome = await orch.resume_tail(rid, from_gate="code")
    assert outcome.status == "built"
    assert store.get_run(rid)["status"] == "built"


async def test_resume_tail_rejected(store, tmp_path):
    rid = store.create_run(idea="i", workspace=tmp_path / "ws", config={"deploy": False})
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(rid, "code")
    config = OrchestratorConfig(deploy=False, gate_broker=broker, gated_stages=GATED)
    orch = Orchestrator(bus=EventBus(), store=store, config=config)

    async def reject_soon():
        await asyncio.sleep(0.03)
        broker.resolve(rid, "code", GateDecision(approve=False, notes="no"))

    asyncio.create_task(reject_soon())
    outcome = await orch.resume_tail(rid, from_gate="code")
    assert outcome.status == "rejected_at_code"


async def test_resume_tail_deploy(store, tmp_path):
    rid = store.create_run(idea="i", workspace=tmp_path / "ws", config={"deploy": True})
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(rid, "deploy")
    config = OrchestratorConfig(deploy=True, gate_broker=broker, gated_stages=GATED)
    orch = Orchestrator(bus=EventBus(), store=store, config=config)

    async def fake_deploy(workspace, outcome):
        return {"status": "deployed", "url": "https://x.vercel.app"}

    orch._stage_deployer = fake_deploy  # type: ignore[assignment]

    async def approve_soon():
        await asyncio.sleep(0.03)
        broker.resolve(rid, "deploy", GateDecision(approve=True))

    asyncio.create_task(approve_soon())
    outcome = await orch.resume_tail(rid, from_gate="deploy")
    assert outcome.status == "deployed"
    assert store.get_run(rid)["deploy_url"] == "https://x.vercel.app"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_resume.py -v`
Expected: FAIL — `'Orchestrator' object has no attribute 'resume_tail'`.

- [ ] **Step 3: Add `config` persistence on the CLI path**

In `backend/app/orchestrator.py`, in `run()`, replace the run-row creation block (currently `if run_id is None: run_id = self.store.create_run(idea=idea, workspace=workspace)`) with:
```python
        if run_id is None:
            run_id = self.store.create_run(
                idea=idea, workspace=workspace, config=self._config_dict()
            )
```
And add this helper method to `Orchestrator` (e.g. after `_add_cost`):
```python
    def _config_dict(self) -> dict:
        c = self.config
        return {
            "deploy": c.deploy,
            "max_review_rounds": c.max_review_rounds,
            "planner_model": c.planner_model,
            "scaffolder_model": c.scaffolder_model,
            "coder_model": c.coder_model,
            "reviewer_model": c.reviewer_model,
            "deployer_model": c.deployer_model,
        }
```

- [ ] **Step 4: Extract the post-green-build tail**

In `backend/app/orchestrator.py`, inside `run()`, the `else:` branch that runs when `verdict.get("verdict") == "pass"` currently sets `page_tsx_written`, builds `code_payload`, calls the code gate, and does the deploy/built logic. Replace that whole `else` body with a single call:
```python
                else:
                    await self._finish_after_code_gate(
                        workspace, outcome, verdict, entry_gate=None
                    )
```
Then add this new method to `Orchestrator` (e.g. just after `_stage_coder_initial`, before `_gate`):
```python
    async def _finish_after_code_gate(
        self,
        workspace: Path,
        outcome: RunOutcome,
        verdict: Optional[dict],
        *,
        entry_gate: Optional[str] = None,
    ) -> None:
        """The pipeline tail from the code gate onward. Shared by `run()`
        (entry_gate=None, fresh) and `resume_tail()` (entry_gate in
        {'code','deploy'}, where the gate row is already open).
        """
        do_code = entry_gate in (None, "code")
        code_reopen = entry_gate is None
        if do_code:
            if code_reopen:
                outcome.page_tsx_written = (workspace / "app" / "page.tsx").exists()
                payload: dict = {"workspace": str(workspace), "verdict": verdict}
                diff = _compute_workspace_diff(workspace)
                if diff is not None:
                    payload["diff"] = diff
            else:
                payload = {}
            await self._gate("code", payload, outcome, reopen=code_reopen)

        if self.config.deploy:
            deploy_reopen = entry_gate != "deploy"
            await self._gate(
                "deploy", {"workspace": str(workspace)}, outcome, reopen=deploy_reopen
            )
            deploy = await self._stage_deployer(workspace, outcome)
            outcome.deploy = deploy
            outcome.status = (
                "deployed" if deploy.get("status") == "deployed" else "deploy_failed"
            )
        else:
            outcome.status = "built"
```

- [ ] **Step 5: Add the `reopen` flag to `_gate`**

In `backend/app/orchestrator.py`, change `_gate`'s signature and its open/emit block. Replace the signature line `async def _gate(self, name: str, payload: dict, outcome: RunOutcome) -> Optional[GateDecision]:` with:
```python
    async def _gate(
        self,
        name: str,
        payload: dict,
        outcome: RunOutcome,
        *,
        reopen: bool = True,
    ) -> Optional[GateDecision]:
```
Then, in the body, the two statements that register the future and emit `gate_open` (currently `future = self.config.gate_broker.open(outcome.run_id, name)` followed by the `await self.bus.emit(PipelineEvent(kind="gate_open", ...))`) must become conditional on `reopen`, and the `await asyncio.wait_for(future, ...)` must become a broker `wait()`. Replace from the `future = ...` line through the `decision = await asyncio.wait_for(...)` line with:
```python
        if reopen:
            self.config.gate_broker.open(outcome.run_id, name, payload=payload)
            await self.bus.emit(
                PipelineEvent(
                    kind="gate_open",
                    source="orchestrator",
                    text=name,
                    meta={"name": name, "payload": payload},
                )
            )
        try:
            decision = await self.config.gate_broker.wait(
                outcome.run_id, name, timeout=self.config.gate_timeout_s
            )
```
Leave the rest of `_gate` (the `except asyncio.TimeoutError:` block, the `gate_decision` emit, and the rejection handling) unchanged. (`asyncio` is already imported at the top of the module; the `wait_for` import use is simply replaced — no import change needed.)

- [ ] **Step 6: Add `resume_tail`**

In `backend/app/orchestrator.py`, add this method to `Orchestrator` (e.g. just after `run()`):
```python
    async def resume_tail(self, run_id: int, *, from_gate: str) -> RunOutcome:
        """Continue a run that was paused at the code/deploy gate when the
        server died. Reconstructs minimal state from the run row; `self.config`
        is supplied by the caller (built from the persisted run config)."""
        run_row = self.store.get_run(run_id)
        if run_row is None:
            raise ValueError(f"run {run_id} not found")
        workspace = Path(run_row["workspace"])
        outcome = RunOutcome(run_id=run_id, idea=run_row["idea"], workspace=workspace)
        outcome.total_cost_usd = run_row["total_cost_usd"] or 0.0

        store_listener = attach_store_to_bus(self.bus, self.store, run_id)
        unexpected: Optional[BaseException] = None
        try:
            await self.bus.emit(
                PipelineEvent(
                    kind="pipeline_resumed",
                    source="orchestrator",
                    text=f"run #{run_id} resumed at gate {from_gate!r}",
                    meta={"run_id": run_id, "from_gate": from_gate},
                )
            )
            try:
                await self._finish_after_code_gate(
                    workspace, outcome, None, entry_gate=from_gate
                )
            except PipelineFailure as exc:
                outcome.status = (
                    outcome.status if outcome.status != "running" else "errored"
                )
                outcome.error = str(exc)
            except Exception as exc:
                outcome.status = "errored"
                outcome.error = f"{type(exc).__name__}: {exc}"
                unexpected = exc

            await self.bus.emit(
                PipelineEvent(
                    kind="pipeline_end",
                    source="orchestrator",
                    text=outcome.error or outcome.status,
                    meta={
                        "is_error": outcome.status not in ("built", "deployed"),
                        "total_cost_usd": outcome.total_cost_usd,
                        "deploy_url": (outcome.deploy or {}).get("url"),
                        "resumed": True,
                    },
                )
            )
            self.store.finish_run(
                run_id,
                status=outcome.status,
                total_cost_usd=outcome.total_cost_usd,
                deploy_url=(outcome.deploy or {}).get("url"),
                error=outcome.error,
            )
        finally:
            self.bus.remove(store_listener)

        if unexpected is not None:
            raise unexpected
        return outcome
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_resume.py -v`
Expected: 3 passed.

- [ ] **Step 8: Run the full suite (regression check)**

Run: `python -m pytest -v`
Expected: all passed.

- [ ] **Step 9: Commit**

```bash
git add backend/app/orchestrator.py backend/tests/test_resume.py
git commit -m "feat: orchestrator resume_tail + DB-poll gates + tail extraction"
```

---

## Task 8: Server — wiring, config persistence, startup recovery sweep

**Files:**
- Modify: `backend/app/server.py`
- Test: `backend/tests/test_resume.py` (append a disposition test)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_resume.py`:
```python
def test_resume_disposition():
    from app.server import _resume_disposition

    assert _resume_disposition("code") == "resume"
    assert _resume_disposition("deploy") == "resume"
    assert _resume_disposition("spec") == "interrupt"
    assert _resume_disposition(None) == "interrupt"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_resume.py::test_resume_disposition -v`
Expected: FAIL — `cannot import name '_resume_disposition'`.

- [ ] **Step 3: Construct the broker over the store**

In `backend/app/server.py`, replace `_gate_broker = GateBroker()` with:
```python
_gate_broker = GateBroker(_store)
```
(and update the nearby comment that says the broker "Lives for the FastAPI process lifetime... Restart recovery is M4" to note it is now DB-backed and restart-recoverable).

- [ ] **Step 4: Build a config dict in `create_run` and persist it**

In `backend/app/server.py`, in `create_run`, replace the `run_id = _store.create_run(idea=idea, workspace=workspace)` line so the same config dict feeds both the DB row and the orchestrator. Specifically, just before creating the run row, build:
```python
    config_dict = {
        "deploy": payload.deploy,
        "max_review_rounds": payload.max_rounds,
        "planner_model": payload.planner_model or None,
        "scaffolder_model": payload.scaffolder_model or None,
        "coder_model": payload.coder_model or None,
        "reviewer_model": payload.reviewer_model or None,
        "deployer_model": payload.deployer_model or None,
    }
    run_id = _store.create_run(idea=idea, workspace=workspace, config=config_dict)
```
and replace the `OrchestratorConfig(...)` inside `_run_pipeline` with one built from `config_dict` plus the broker:
```python
            config = OrchestratorConfig(
                max_review_rounds=config_dict["max_review_rounds"],
                deploy=config_dict["deploy"],
                gate_broker=_gate_broker,
                gated_stages=_GATED_STAGES,
                planner_model=config_dict["planner_model"],
                scaffolder_model=config_dict["scaffolder_model"],
                coder_model=config_dict["coder_model"],
                reviewer_model=config_dict["reviewer_model"],
                deployer_model=config_dict["deployer_model"],
            )
```

- [ ] **Step 5: Stop cancelling gates when the run task exits**

In `backend/app/server.py`, in `_run_pipeline`'s `finally` block, **remove** the line:
```python
            _gate_broker.cancel_all(run_id, notes="run task exited")
```
Leave `_active_runs.pop(run_id, None)`. Add a short comment in its place:
```python
            # NOTE: do NOT cancel open gates here. With DB-backed gates a task
            # that dies at a gate (e.g. graceful shutdown) must leave the gate
            # 'open' so the startup sweep can resume it. Explicit cancellation
            # only happens via POST /cancel.
```

- [ ] **Step 6: Add the disposition helper and config-from-row builder**

In `backend/app/server.py`, add near the other helpers (after `_event_row_to_dict`):
```python
def _resume_disposition(open_gate: Optional[str]) -> str:
    """Decide what to do with a `running` run found at startup, based on the
    gate it was paused at. Only the cheap-tail gates (code/deploy) resume."""
    return "resume" if open_gate in ("code", "deploy") else "interrupt"


def _config_from_row(row) -> OrchestratorConfig:
    raw = json.loads(row["config"]) if row["config"] else {}
    return OrchestratorConfig(
        max_review_rounds=raw.get("max_review_rounds", 3),
        deploy=raw.get("deploy", False),
        gate_broker=_gate_broker,
        gated_stages=_GATED_STAGES,
        planner_model=raw.get("planner_model"),
        scaffolder_model=raw.get("scaffolder_model"),
        coder_model=raw.get("coder_model"),
        reviewer_model=raw.get("reviewer_model"),
        deployer_model=raw.get("deployer_model"),
    )
```

- [ ] **Step 7: Add the startup recovery sweep via lifespan**

In `backend/app/server.py`, add `from contextlib import asynccontextmanager` near the top imports, and add `PipelineEvent` to the events import so it can be constructed here:
```python
from .events import EventBus, PipelineEvent
```
Add the recovery functions (after the helpers from Step 6):
```python
def _launch_resume(run_id: int, row, open_gate: str) -> None:
    bus = EventBus()
    config = _config_from_row(row)

    async def _run() -> None:
        try:
            orchestrator = Orchestrator(bus=bus, store=_store, config=config)
            await orchestrator.resume_tail(run_id, from_gate=open_gate)
        except Exception:
            logger.exception("resume of run %s failed", run_id)
        finally:
            _active_runs.pop(run_id, None)

    _active_runs[run_id] = asyncio.create_task(_run())


async def _recover_runs() -> None:
    """On startup, reconsider every run left 'running' by a dead process:
    resume the cheap-tail gates, mark the rest interrupted."""
    _store.reset_stale_resuming()
    for row in _store.running_runs():
        run_id = row["id"]
        if not _store.claim_run_for_resume(run_id):
            continue  # another worker won the claim
        pending = _store.pending_gates(run_id)
        open_gate = pending[0] if pending else None
        if _resume_disposition(open_gate) == "resume":
            fresh = _store.get_run(run_id)  # re-read (status is now 'resuming')
            _launch_resume(run_id, fresh, open_gate)
            logger.info("resumed run %s at gate %s", run_id, open_gate)
        else:
            _store.cancel_open_gates(run_id, notes="server restarted")
            _store.mark_interrupted(run_id, error="server restarted before resumable gate")
            _store.record_event(
                run_id,
                PipelineEvent(
                    kind="run_interrupted",
                    source="orchestrator",
                    text="server restarted",
                    meta={"run_id": run_id},
                ),
            )
            logger.info("marked run %s interrupted (gate=%s)", run_id, open_gate)
```
Then add the lifespan and pass it to `FastAPI(...)`. Replace `app = FastAPI(title="Ship-It backend")` with:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _recover_runs()
    yield


app = FastAPI(title="Ship-It backend", lifespan=lifespan)
```

- [ ] **Step 8: Run the disposition test + full suite**

Run: `python -m pytest -v`
Expected: all passed (including `test_resume_disposition`).

- [ ] **Step 9: Manual smoke — import the server module**

Run (from `backend/`):
```
python -c "import app.server; print('server import OK')"
```
Expected: prints `server import OK` (validates lifespan wiring and imports).

- [ ] **Step 10: Commit**

```bash
git add backend/app/server.py backend/tests/test_resume.py
git commit -m "feat: DB-backed gate wiring + startup resume/interrupt sweep"
```

---

## Task 9: Manual end-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Start the backend**

Run (from `backend/`, venv active):
```
uvicorn app.server:app --port 8000
```
Expected: starts cleanly; server log shows no resume/interrupt lines on a fresh DB (or correct lines if prior runs exist).

- [ ] **Step 2: Restart-resume drill**

With the dashboard (or `curl`) start a run and let it reach the **code** gate (do not approve). Then stop uvicorn (Ctrl+C) and start it again.
Expected on restart: server log shows `resumed run <id> at gate code`; `GET /api/runs/<id>/gates` returns `{"pending": ["code"]}`. Approving the gate (`POST /api/runs/<id>/gate/code` with body `{"decision":"approve"}`) drives the run to `built`.

- [ ] **Step 3: Interrupt drill**

Start a run, let it reach the **spec** gate, stop and restart uvicorn.
Expected: server log shows `marked run <id> interrupted (gate=spec)`; `GET /api/runs/<id>` shows `status: "interrupted"`.

- [ ] **Step 4: Commit (if any tweaks were needed)**

```bash
git add -A
git commit -m "fix: address findings from restart-resume manual verification"
```
(Skip if no changes.)

---

## Task 10: Docs

**Files:**
- Modify: `CLAUDE.md` (Current status — close out the "Remaining hardening" item)
- Modify: `SHIP-IT_BUILD_PLAN.md` (§10 recommended next step / milestone list)
- Modify: `LEARNING.md` (add a short section on DB-backed gates + resume)

- [ ] **Step 1: Update `CLAUDE.md`**

In the **Current status** section, replace the "Remaining hardening" bullet with a "Milestone 5 (DONE)" entry summarizing: gates moved to a `gates` table; `GateBroker` polls the DB; runs paused at code/deploy resume on startup; spec-gate/mid-stage runs are marked `interrupted`; run config persisted in a `config` column. Note the remaining true non-goal (mid-stage resume).

- [ ] **Step 2: Update `SHIP-IT_BUILD_PLAN.md`**

In §10, mark the restart-resumable / DB-backed broker work as done and point at the spec/plan docs.

- [ ] **Step 3: Update `LEARNING.md`**

Add a short subsection explaining the DB-poll gate pattern (mirrors the SSE poller), the `config` column's role in resume, and the startup sweep, with `file:line` links into `store.py`/`gates.py`/`server.py`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md SHIP-IT_BUILD_PLAN.md LEARNING.md
git commit -m "docs: record DB-backed resumable gates milestone"
```

---

## Self-Review

**Spec coverage:**
- `gates` table + `config` column → Tasks 3, 4. ✓
- Store gate methods (`open/get/resolve/cancel/pending`) → Task 4. ✓
- Recovery methods (`running_runs`, `claim_run_for_resume`, `mark_interrupted`; plus `reset_stale_resuming` for crash-mid-resume) → Task 5. ✓
- `GateBroker` DB-poll rewrite (open/wait/resolve/cancel_all/pending_for) → Task 6. ✓
- `_gate` open→emit→wait ordering + timeout preserved → Task 7 Step 5. ✓
- `_finish_after_code_gate` extraction + `resume_tail` → Task 7. ✓
- Restart sweep (resume code/deploy, interrupt spec/none) in lifespan → Task 8. ✓
- New event kinds `pipeline_resumed`, `run_interrupted` → Task 2 (emitted in Tasks 7 & 8). ✓
- Config persisted on both API (Task 8) and CLI (Task 7 Step 3) paths. ✓
- Tests for round-trip, timeout, sweep classification, atomic claim → Tasks 4, 5, 6, 7, 8. ✓
- Docs → Task 10. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✓

**Type/signature consistency:**
- `Store.open_gate(run_id, name, *, payload)` / `get_gate` / `resolve_gate(*, approve, notes)` / `cancel_open_gates(*, notes)` / `pending_gates` — used identically by `GateBroker` (Task 6) and tests (Tasks 4, 6). ✓
- `GateBroker(store, *, poll_interval)`, `.open(run_id, name, *, payload)`, `.wait(run_id, name, *, timeout)`, `.resolve(run_id, name, decision)` — matches orchestrator `_gate` (Task 7) and server (Task 8). ✓
- `Orchestrator.resume_tail(run_id, *, from_gate)` and `_finish_after_code_gate(workspace, outcome, verdict, *, entry_gate)` — consistent across Task 7 definition and Task 7/8 callers. ✓
- `create_run(idea, workspace, *, config)` — consistent across store (Task 3), orchestrator CLI path (Task 7), server (Task 8), and all tests. ✓
- `_resume_disposition(open_gate)` returns `"resume"`/`"interrupt"` — used in Task 8 sweep and tested in Task 8. ✓

**Notable correctness decision captured in the plan:** Task 8 Step 5 removes the `cancel_all` from the run-task `finally` — required so a graceful-shutdown task death leaves the gate `open` for the sweep to resume, instead of silently rejecting it.
