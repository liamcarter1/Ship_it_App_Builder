# Preview-Locally Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-click "Preview locally" button to a completed run's detail page that spawns the generated app with `npm run dev` and surfaces a clickable `localhost` URL, plus a Stop button.

**Architecture:** A backend `PreviewManager` (one shared instance in `server.py`) owns at most one preview process. It picks a free port, spawns `npm run dev`, waits for the port to accept, and tracks the PID both in memory and in a one-row `previews` SQLite table so a restart can reap orphans. Three run-scoped REST endpoints drive it; a React `PreviewPanel` consumes them. An idle reaper stops a preview after 30 min of no status polls.

**Tech Stack:** Python 3.10+, FastAPI, SQLite (`sqlite3`), `psutil` (existing), `subprocess`; Next.js 14 App Router + TypeScript + Tailwind; pytest (`asyncio_mode=auto`, as used by existing async tests).

**Spec:** `docs/superpowers/specs/2026-06-03-preview-locally-design.md`

**Conventions to honor:**
- Backend runs on the Windows Proactor loop; subprocess spawning happens off the event loop via `asyncio.to_thread` so a slow spawn never blocks the SSE pollers.
- Tests call server endpoint functions **directly** with monkeypatched module globals (the pattern in `backend/tests/test_server_recovery.py`) — no httpx/TestClient needed.
- Run from `backend/` with the venv active: `.\.venv\Scripts\python.exe -m pytest`.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/proc.py` (new) | `kill_process_tree(pid)` — relocated from `orchestrator.py`, shared by orchestrator + preview |
| `backend/app/orchestrator.py` (modify) | import `kill_process_tree` from `proc` instead of defining it |
| `backend/app/store.py` (modify) | `previews` table + `set/get/clear_active_preview` |
| `backend/app/preview.py` (new) | `PreviewManager`, `PreviewInfo`, `PreviewError`, port/spawn/ready helpers |
| `backend/app/server.py` (modify) | instantiate `PreviewManager`, 3 endpoints, lifespan wiring |
| `backend/tests/test_proc.py` (new) | kill-helper relocation smoke test |
| `backend/tests/test_preview.py` (new) | `PreviewManager` unit tests (mocked spawn/kill) |
| `backend/tests/test_server_preview.py` (new) | endpoint validation + happy path |
| `frontend/lib/types.ts` (modify) | `PreviewDTO` |
| `frontend/lib/api.ts` (modify) | `startPreview` / `getPreview` / `stopPreview` |
| `frontend/components/PreviewPanel.tsx` (new) | the button/link/stop UI + polling |
| `frontend/app/runs/[id]/page.tsx` (modify) | render `PreviewPanel` for previewable runs |

---

## Task 1: Relocate the process-tree kill helper into `proc.py`

**Files:**
- Create: `backend/app/proc.py`
- Modify: `backend/app/orchestrator.py:109-156` (remove local def, import instead)
- Test: `backend/tests/test_proc.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_proc.py`:

```python
"""kill_process_tree lives in app.proc and is importable by both the
orchestrator and the preview manager. With psutil unavailable it is a no-op;
with a fake psutil it terminates the parent and its descendants."""
from __future__ import annotations

import app.proc as proc


def test_kill_is_noop_without_psutil(monkeypatch):
    monkeypatch.setattr(proc, "psutil", None)
    proc.kill_process_tree(12345)  # must not raise


def test_kill_terminates_parent_and_children(monkeypatch):
    calls = {"terminated": [], "killed": []}

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
        def children(self, recursive=False):
            return [FakeProc(999)] if self.pid == 1 else []
        def terminate(self):
            calls["terminated"].append(self.pid)
        def kill(self):
            calls["killed"].append(self.pid)

    class FakePsutil:
        Error = Exception
        def Process(self, pid):
            return FakeProc(pid)
        def wait_procs(self, procs, timeout=None):
            return (procs, [])  # all gone, none alive

    monkeypatch.setattr(proc, "psutil", FakePsutil())
    proc.kill_process_tree(1)
    assert set(calls["terminated"]) == {1, 999}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_proc.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.proc'`.

- [ ] **Step 3: Create `backend/app/proc.py`**

Move the body of `_kill_process_tree` verbatim from `orchestrator.py` and rename to the public `kill_process_tree`:

```python
"""Shared process-tree teardown.

Used by the orchestrator (reaping a stalled stage's child claude/npm/Bash
tree) and the preview manager (stopping a `npm run dev` server). Lives here so
neither module imports the other just for this helper.
"""
from __future__ import annotations

try:
    import psutil
except ImportError:  # pragma: no cover - psutil should be installed
    psutil = None


def kill_process_tree(pid: int) -> None:
    """Terminate `pid` and all its descendants.

    Windows safety net: a bare `TerminateProcess` reaps only the immediate
    process, orphaning any `npm`/`node`/Bash grandchildren. psutil walks the
    tree and kills them too. Must be called while the tree is still intact
    (descendants reachable from `pid`). No-op if psutil is unavailable.
    """
    if psutil is None:
        return
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        victims = parent.children(recursive=True)
    except psutil.Error:
        victims = []
    victims.append(parent)
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    try:
        _gone, alive = psutil.wait_procs(victims, timeout=3)
    except psutil.Error:
        alive = victims
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
```

- [ ] **Step 4: Update `orchestrator.py` to import instead of define**

In `backend/app/orchestrator.py`, delete the `_kill_process_tree` function (lines ~122-156) AND the now-unused local `psutil` import block (lines ~109-112) ONLY IF psutil is not referenced elsewhere in the file. Verify first:

Run: `.\.venv\Scripts\python.exe -c "import re,sys; t=open('app/orchestrator.py',encoding='utf-8').read(); print('psutil uses:', t.count('psutil'))"`

- If `psutil` appears only in the def + import block being removed, remove both.
- If it appears elsewhere, keep the import block and remove only the function.

Then add near the other local imports (after the `from .store import ...` line, around `orchestrator.py:` top imports):

```python
from .proc import kill_process_tree
```

Replace the single call site `_kill_process_tree(pid)` (around `orchestrator.py:286`) with `kill_process_tree(pid)`.

- [ ] **Step 5: Run the full backend suite to verify nothing broke**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: PASS — the previous 32 tests + 2 new = all green. The stall-watchdog tests still pass because the kill behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/app/proc.py backend/app/orchestrator.py backend/tests/test_proc.py
git commit -m "refactor: extract kill_process_tree into shared app.proc"
```

---

## Task 2: Add the `previews` table and Store accessors

**Files:**
- Modify: `backend/app/store.py:24-63` (SCHEMA), add methods after `clear`/recovery section
- Test: `backend/tests/test_preview_store.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_preview_store.py`:

```python
"""The store holds at most one active preview row (id is pinned to 1)."""
from __future__ import annotations

from app.store import Store


def test_set_get_clear_active_preview(tmp_path):
    store = Store(db_path=tmp_path / "p.db")
    assert store.get_active_preview() is None

    store.set_active_preview(run_id=7, pid=4321, port=4300, started_at=1000.0)
    row = store.get_active_preview()
    assert row["run_id"] == 7 and row["pid"] == 4321 and row["port"] == 4300

    # set again -> overwrites the single row (no second row appears)
    store.set_active_preview(run_id=9, pid=5555, port=4301, started_at=2000.0)
    row = store.get_active_preview()
    assert row["run_id"] == 9 and row["pid"] == 5555

    store.clear_active_preview()
    assert store.get_active_preview() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview_store.py -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'set_active_preview'`.

- [ ] **Step 3: Add the table to SCHEMA**

In `backend/app/store.py`, append to the `SCHEMA` string (after the `gates_run_idx` index, before the closing `"""`):

```sql

CREATE TABLE IF NOT EXISTS previews (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    port INTEGER NOT NULL,
    started_at REAL NOT NULL
);
```

The `CHECK (id = 1)` pins the table to a single row — the "one preview at a time" invariant expressed in the schema.

- [ ] **Step 4: Add the accessor methods**

In `backend/app/store.py`, add to the `Store` class (after `pending_gates`, before the `# --- restart recovery ---` section):

```python
    # --- preview (one active local preview server) -------------------------

    def set_active_preview(
        self, *, run_id: int, pid: int, port: int, started_at: float
    ) -> None:
        """Upsert the single preview row. Overwrites any existing preview."""
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO previews (id, run_id, pid, port, started_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "run_id=excluded.run_id, pid=excluded.pid, "
                "port=excluded.port, started_at=excluded.started_at",
                (run_id, pid, port, started_at),
            )

    def get_active_preview(self):
        with connect(self.db_path) as conn:
            return conn.execute("SELECT * FROM previews WHERE id=1").fetchone()

    def clear_active_preview(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM previews WHERE id=1")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview_store.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/store.py backend/tests/test_preview_store.py
git commit -m "feat: previews table + active-preview accessors in Store"
```

---

## Task 3: Preview helpers — free-port finder, npm resolver, spawn, readiness

**Files:**
- Create: `backend/app/preview.py` (helpers only this task; class in Task 4)
- Test: `backend/tests/test_preview.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_preview.py`:

```python
"""PreviewManager + helpers. No real npm is ever spawned: spawn, readiness,
and kill are monkeypatched at the module level."""
from __future__ import annotations

import socket

import pytest

import app.preview as preview
from app.preview import PreviewError, _find_free_port


def test_find_free_port_returns_a_bindable_port():
    port = _find_free_port(range(4300, 4400))
    # We can actually bind it (it was free at selection time).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


def test_find_free_port_raises_when_range_exhausted():
    # An empty range can never yield a port.
    with pytest.raises(PreviewError):
        _find_free_port(range(0, 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.preview'`.

- [ ] **Step 3: Create `backend/app/preview.py` with helpers**

```python
"""Local preview of a completed run's generated app.

`PreviewManager` owns at most one `npm run dev` process. It picks a free port,
spawns the dev server, waits for the port to accept connections, and tracks the
PID in memory + the store (so a restart can reap an orphan). One preview at a
time; starting another stops the first. An idle reaper stops a preview after a
period with no status polls.

No real npm is spawned in tests: `_spawn_dev_server`, `_wait_until_ready`, and
`kill_process_tree` are monkeypatched.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .proc import kill_process_tree

PREVIEW_PORT_RANGE = range(4300, 4400)
PREVIEW_READY_TIMEOUT_S = 30.0
PREVIEW_IDLE_TIMEOUT_S = float(os.environ.get("PREVIEW_IDLE_TIMEOUT_S", "1800"))


class PreviewError(Exception):
    """A preview could not be started (npm missing, no port, never ready)."""


@dataclass
class PreviewInfo:
    run_id: int
    pid: int
    port: int
    url: str
    started_at: float
    last_active: float


def _find_free_port(port_range=PREVIEW_PORT_RANGE) -> int:
    """First port in `port_range` we can bind on localhost. Bind-test then
    release immediately; the dev server re-binds it a moment later (a tiny TOCTOU
    window, acceptable for a single-user local tool)."""
    for port in port_range:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise PreviewError(
        f"no free preview port available in range "
        f"{port_range.start}-{port_range.stop - 1}"
    )


def _resolve_npm() -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise PreviewError("npm not found on PATH")
    return npm


def _log_tail(workspace: Path, n: int = 800) -> str:
    log = workspace / "_preview.log"
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def _spawn_dev_server(workspace: Path, port: int) -> subprocess.Popen:
    """Spawn `npm run dev -- -p <port>` in `workspace`, logging to
    `_preview.log`. Returns the Popen handle (its .pid roots the tree we kill)."""
    npm = _resolve_npm()
    log = open(workspace / "_preview.log", "w", encoding="utf-8")
    return subprocess.Popen(
        [npm, "run", "dev", "--", "-p", str(port)],
        cwd=str(workspace),
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _wait_until_ready(
    proc: subprocess.Popen,
    port: int,
    workspace: Path,
    timeout: float = PREVIEW_READY_TIMEOUT_S,
) -> None:
    """Block until the port accepts a TCP connection, or raise PreviewError if
    the process exits first or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise PreviewError(
                "preview process exited before becoming ready:\n"
                + _log_tail(workspace)
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise PreviewError(f"preview did not become ready within {timeout:.0f}s")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview.py -v`
Expected: PASS (both helper tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/preview.py backend/tests/test_preview.py
git commit -m "feat: preview helpers (free port, npm resolve, spawn, readiness)"
```

---

## Task 4: `PreviewManager.start/stop/status/touch`

**Files:**
- Modify: `backend/app/preview.py` (append the class)
- Test: `backend/tests/test_preview.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_preview.py`:

```python
class _FakePopen:
    def __init__(self, pid=4321):
        self.pid = pid
        self._alive = True
    def poll(self):
        return None if self._alive else 0


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    from app.store import Store
    from app.preview import PreviewManager

    store = Store(db_path=tmp_path / "prev.db")
    killed = []
    spawned = []

    def fake_spawn(workspace, port):
        p = _FakePopen(pid=10000 + port)
        spawned.append((str(workspace), port, p.pid))
        return p

    monkeypatch.setattr("app.preview._spawn_dev_server", fake_spawn)
    monkeypatch.setattr("app.preview._wait_until_ready", lambda *a, **k: None)
    monkeypatch.setattr("app.preview._find_free_port", lambda *a, **k: 4300)
    monkeypatch.setattr("app.preview.kill_process_tree", lambda pid: killed.append(pid))

    return PreviewManager(store), store, killed, spawned


async def test_start_records_info_and_persists(mgr, tmp_path):
    manager, store, _killed, spawned = mgr
    info = await manager.start(7, tmp_path)
    assert info.run_id == 7 and info.port == 4300
    assert info.url == "http://localhost:4300"
    assert manager.status() is info
    assert len(spawned) == 1
    row = store.get_active_preview()
    assert row["run_id"] == 7 and row["pid"] == info.pid


async def test_start_same_run_is_idempotent(mgr, tmp_path):
    manager, _store, _killed, spawned = mgr
    await manager.start(7, tmp_path)
    await manager.start(7, tmp_path)  # no second spawn
    assert len(spawned) == 1


async def test_start_other_run_stops_previous(mgr, tmp_path):
    manager, store, killed, spawned = mgr
    first = await manager.start(7, tmp_path)
    second = await manager.start(8, tmp_path)
    assert first.pid in killed          # old tree was killed
    assert manager.status().run_id == 8
    assert len(spawned) == 2
    assert store.get_active_preview()["run_id"] == 8


async def test_stop_kills_and_clears(mgr, tmp_path):
    manager, store, killed, _spawned = mgr
    info = await manager.start(7, tmp_path)
    assert await manager.stop() is True
    assert info.pid in killed
    assert manager.status() is None
    assert store.get_active_preview() is None
    assert await manager.stop() is False  # nothing left to stop


async def test_ready_failure_kills_halfstarted(mgr, tmp_path, monkeypatch):
    manager, store, killed, _spawned = mgr
    from app.preview import PreviewError

    def boom(*a, **k):
        raise PreviewError("never ready")

    monkeypatch.setattr("app.preview._wait_until_ready", boom)
    with pytest.raises(PreviewError):
        await manager.start(7, tmp_path)
    assert killed  # the half-started tree was reaped
    assert manager.status() is None
    assert store.get_active_preview() is None


async def test_touch_bumps_last_active(mgr, tmp_path, monkeypatch):
    manager, _store, _killed, _spawned = mgr
    info = await manager.start(7, tmp_path)
    monkeypatch.setattr("app.preview.time.time", lambda: info.last_active + 5)
    manager.touch()
    assert manager.status().last_active == info.last_active + 5
```

> Note: all these tests are `async def` (relies on `asyncio_mode=auto`, as the existing async tests do).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview.py -v`
Expected: FAIL with `ImportError: cannot import name 'PreviewManager'`.

- [ ] **Step 3: Append `PreviewManager` to `preview.py`**

```python
import asyncio


class PreviewManager:
    """Owns at most one local preview process."""

    def __init__(self, store, *, idle_timeout_s: float = PREVIEW_IDLE_TIMEOUT_S):
        self._store = store
        self._idle_timeout_s = idle_timeout_s
        self._info: Optional[PreviewInfo] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    def status(self) -> Optional[PreviewInfo]:
        return self._info

    def touch(self) -> None:
        if self._info is not None:
            self._info.last_active = time.time()

    async def start(self, run_id: int, workspace: Path) -> PreviewInfo:
        async with self._lock:
            if self._info is not None and self._info.run_id == run_id:
                self._info.last_active = time.time()
                return self._info
            if self._info is not None:
                await self._stop_locked()

            port = _find_free_port()
            proc = await asyncio.to_thread(_spawn_dev_server, Path(workspace), port)
            try:
                await asyncio.to_thread(
                    _wait_until_ready, proc, port, Path(workspace)
                )
            except PreviewError:
                await asyncio.to_thread(kill_process_tree, proc.pid)
                raise

            now = time.time()
            info = PreviewInfo(
                run_id=run_id,
                pid=proc.pid,
                port=port,
                url=f"http://localhost:{port}",
                started_at=now,
                last_active=now,
            )
            self._info = info
            self._proc = proc
            self._store.set_active_preview(
                run_id=run_id, pid=proc.pid, port=port, started_at=now
            )
            return info

    async def stop(self) -> bool:
        async with self._lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> bool:
        if self._info is None:
            return False
        await asyncio.to_thread(kill_process_tree, self._info.pid)
        self._store.clear_active_preview()
        self._info = None
        self._proc = None
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview.py -v`
Expected: PASS (all start/stop/touch tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/preview.py backend/tests/test_preview.py
git commit -m "feat: PreviewManager start/stop/status/touch"
```

---

## Task 5: `PreviewManager.recover()` + idle reaper

**Files:**
- Modify: `backend/app/preview.py` (append methods to the class)
- Test: `backend/tests/test_preview.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_preview.py`:

```python
async def test_recover_kills_orphan_and_clears(mgr, tmp_path):
    manager, store, killed, _spawned = mgr
    # Simulate a record left by a dead process (no in-memory state).
    store.set_active_preview(run_id=3, pid=98765, port=4300, started_at=1.0)
    await manager.recover()
    assert 98765 in killed
    assert store.get_active_preview() is None


async def test_recover_is_noop_without_record(mgr):
    manager, _store, killed, _spawned = mgr
    await manager.recover()  # must not raise
    assert killed == []


async def test_idle_reap_once_stops_stale_preview(mgr, tmp_path, monkeypatch):
    manager, store, killed, _spawned = mgr
    info = await manager.start(7, tmp_path)
    # Make it look idle beyond the timeout.
    monkeypatch.setattr(
        "app.preview.time.time", lambda: info.last_active + manager._idle_timeout_s + 1
    )
    await manager.reap_idle_once()
    assert info.pid in killed
    assert manager.status() is None


async def test_idle_reap_once_keeps_fresh_preview(mgr, tmp_path):
    manager, _store, killed, _spawned = mgr
    await manager.start(7, tmp_path)
    await manager.reap_idle_once()  # just started -> not idle
    assert killed == []
    assert manager.status() is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview.py -k "recover or idle" -v`
Expected: FAIL with `AttributeError: 'PreviewManager' object has no attribute 'recover'`.

- [ ] **Step 3: Append `recover` + reaper to `PreviewManager`**

```python
    async def recover(self) -> None:
        """Startup sweep: kill the orphaned tree from a previous process and
        clear the persisted record. Safe if the PID is already gone."""
        row = self._store.get_active_preview()
        if row is None:
            return
        await asyncio.to_thread(kill_process_tree, row["pid"])
        self._store.clear_active_preview()

    async def reap_idle_once(self) -> None:
        """Stop the preview if it has been idle past the timeout. One pass."""
        info = self._info
        if info is not None and time.time() - info.last_active > self._idle_timeout_s:
            await self.stop()

    def start_reaper(self, interval_s: float = 30.0) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reaper_loop(interval_s))

    async def stop_reaper(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None

    async def _reaper_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self.reap_idle_once()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_preview.py -v`
Expected: PASS (all preview tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/preview.py backend/tests/test_preview.py
git commit -m "feat: PreviewManager restart recovery + idle reaper"
```

---

## Task 6: Server endpoints + lifespan wiring

**Files:**
- Modify: `backend/app/server.py` (import, instance, 3 endpoints, lifespan)
- Test: `backend/tests/test_server_preview.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_server_preview.py`:

```python
"""Preview endpoints: validation + happy path. PreviewManager.start/stop are
replaced with fakes so no npm spawns (mirrors test_server_recovery's style of
driving server functions directly with monkeypatched globals)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.server as srv
from app.preview import PreviewInfo
from app.store import Store


@pytest.fixture
def wired(tmp_path, monkeypatch):
    store = Store(db_path=tmp_path / "srv.db")
    monkeypatch.setattr(srv, "_store", store)
    return store


async def test_start_404_for_unknown_run(wired):
    with pytest.raises(HTTPException) as ei:
        await srv.start_preview(999)
    assert ei.value.status_code == 404


async def test_start_409_for_non_previewable_status(wired, tmp_path):
    store = wired
    rid = store.create_run(idea="x", workspace=tmp_path / "w")
    store.finish_run(rid, "errored")
    with pytest.raises(HTTPException) as ei:
        await srv.start_preview(rid)
    assert ei.value.status_code == 409


async def test_start_409_when_node_modules_missing(wired, tmp_path):
    store = wired
    ws = tmp_path / "w"
    (ws).mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")  # no node_modules
    rid = store.create_run(idea="x", workspace=ws)
    store.finish_run(rid, "built")
    with pytest.raises(HTTPException) as ei:
        await srv.start_preview(rid)
    assert ei.value.status_code == 409


async def test_deploy_failed_is_previewable_happy_path(wired, tmp_path, monkeypatch):
    store = wired
    ws = tmp_path / "w"
    (ws / "node_modules").mkdir(parents=True)
    (ws / "package.json").write_text("{}", encoding="utf-8")
    rid = store.create_run(idea="x", workspace=ws)
    store.finish_run(rid, "deploy_failed")

    info = PreviewInfo(
        run_id=rid, pid=111, port=4300,
        url="http://localhost:4300", started_at=1.0, last_active=1.0,
    )

    async def fake_start(run_id, workspace):
        return info

    monkeypatch.setattr(srv._preview, "start", fake_start)
    monkeypatch.setattr(srv._preview, "status", lambda: info)

    started = await srv.start_preview(rid)
    assert started == {
        "active": True, "run_id": rid, "port": 4300,
        "url": "http://localhost:4300", "started_at": 1.0,
    }

    got = await srv.get_preview(rid)
    assert got["active"] is True and got["port"] == 4300

    # GET for a different run -> not active here
    other = await srv.get_preview(rid + 1)
    assert other == {"active": False}


async def test_stop_only_stops_matching_run(wired, tmp_path, monkeypatch):
    store = wired
    info = PreviewInfo(
        run_id=5, pid=1, port=4300,
        url="http://localhost:4300", started_at=1.0, last_active=1.0,
    )
    monkeypatch.setattr(srv._preview, "status", lambda: info)

    async def fake_stop():
        return True

    monkeypatch.setattr(srv._preview, "stop", fake_stop)

    assert await srv.stop_preview(6) == {"stopped": False}  # different run
    assert await srv.stop_preview(5) == {"stopped": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_server_preview.py -v`
Expected: FAIL with `AttributeError: module 'app.server' has no attribute 'start_preview'`.

- [ ] **Step 3: Wire the import + instance into `server.py`**

In `backend/app/server.py`, add to the local imports (after `from .orchestrator import ...`):

```python
from .preview import PreviewManager, PreviewError, PreviewInfo
```

After `_gate_broker = GateBroker(_store)` (around `server.py:85`), add:

```python
# One shared preview manager: at most one local `npm run dev` at a time.
_preview = PreviewManager(_store)
# Run statuses whose workspace has a green build and is safe to preview.
_PREVIEWABLE_STATUSES: tuple[str, ...] = ("built", "deployed", "deploy_failed")
```

- [ ] **Step 4: Update the lifespan handler**

Replace the existing `lifespan` body (`server.py:93-96`) with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _recover_runs()
    await _preview.recover()        # reap any preview orphaned by a crash
    _preview.start_reaper()         # idle auto-stop loop
    try:
        yield
    finally:
        await _preview.stop_reaper()
        await _preview.stop()       # don't leave a dev server running on shutdown
```

- [ ] **Step 5: Add the three endpoints**

In `backend/app/server.py`, after the `cancel_run` endpoint (around `server.py:393`), add:

```python
# ---------------------------------------------------------------------------
# Local preview (run the generated app)
# ---------------------------------------------------------------------------


def _preview_dto(info: PreviewInfo) -> dict:
    return {
        "active": True,
        "run_id": info.run_id,
        "port": info.port,
        "url": info.url,
        "started_at": info.started_at,
    }


@app.post("/api/runs/{run_id}/preview")
async def start_preview(run_id: int):
    row = _store.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    if row["status"] not in _PREVIEWABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"run status '{row['status']}' is not previewable",
        )
    workspace = Path(row["workspace"])
    if not (workspace / "package.json").exists() or not (
        workspace / "node_modules"
    ).exists():
        raise HTTPException(
            status_code=409,
            detail="workspace has no installed app — run `npm install` in it first",
        )
    try:
        info = await _preview.start(run_id, workspace)
    except PreviewError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return _preview_dto(info)


@app.get("/api/runs/{run_id}/preview")
async def get_preview(run_id: int):
    info = _preview.status()
    if info is None or info.run_id != run_id:
        return {"active": False}
    _preview.touch()
    return _preview_dto(info)


@app.delete("/api/runs/{run_id}/preview")
async def stop_preview(run_id: int):
    info = _preview.status()
    if info is None or info.run_id != run_id:
        return {"stopped": False}
    stopped = await _preview.stop()
    return {"stopped": stopped}
```

- [ ] **Step 6: Run the full suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: PASS — all existing + new tests green.

- [ ] **Step 7: Commit**

```bash
git add backend/app/server.py backend/tests/test_server_preview.py
git commit -m "feat: preview endpoints + lifespan recover/reaper wiring"
```

---

## Task 7: Frontend types + API helpers

**Files:**
- Modify: `frontend/lib/types.ts` (append `PreviewDTO`)
- Modify: `frontend/lib/api.ts` (append 3 helpers)

- [ ] **Step 1: Add `PreviewDTO` to `types.ts`**

Append to `frontend/lib/types.ts`:

```typescript
export interface PreviewDTO {
  active: boolean;
  run_id?: number;
  port?: number;
  url?: string;
  started_at?: number;
}
```

- [ ] **Step 2: Add API helpers to `api.ts`**

In `frontend/lib/api.ts`, update the type import line to include `PreviewDTO`:

```typescript
import type { PreviewDTO, RunDTO } from './types';
```

Append these functions at the end of the file:

```typescript
export async function getPreview(runId: number): Promise<PreviewDTO> {
  return getJson<PreviewDTO>(`/runs/${runId}/preview`);
}

export async function startPreview(runId: number): Promise<PreviewDTO> {
  const res = await fetch(`${API}/runs/${runId}/preview`, { method: 'POST' });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as PreviewDTO;
}

export async function stopPreview(runId: number): Promise<{ stopped: boolean }> {
  const res = await fetch(`${API}/runs/${runId}/preview`, { method: 'DELETE' });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  return (await res.json()) as { stopped: boolean };
}
```

- [ ] **Step 3: Typecheck**

Run (from `frontend/`): `npm run build` (or `npx tsc --noEmit` if faster).
Expected: compiles — no type errors from the new code. (Build will still succeed even though the helpers are unused until Task 8.)

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts
git commit -m "feat(ui): PreviewDTO + preview API helpers"
```

---

## Task 8: `PreviewPanel` component + wire into run page

**Files:**
- Create: `frontend/components/PreviewPanel.tsx`
- Modify: `frontend/app/runs/[id]/page.tsx`

- [ ] **Step 1: Create `frontend/components/PreviewPanel.tsx`**

```tsx
'use client';

import { useEffect, useState } from 'react';
import { getPreview, startPreview, stopPreview } from '@/lib/api';
import type { PreviewDTO } from '@/lib/types';

export function PreviewPanel({ runId }: { runId: number }) {
  const [preview, setPreview] = useState<PreviewDTO>({ active: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Poll preview status so the panel reflects external changes (idle auto-stop,
  // another run taking over) and so each poll keeps this preview's idle timer warm.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const p = await getPreview(runId);
        if (alive) setPreview(p);
      } catch {
        /* transient poll errors are non-fatal */
      }
    };
    tick();
    const handle = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(handle);
    };
  }, [runId]);

  async function handleStart() {
    setBusy(true);
    setError(null);
    try {
      setPreview(await startPreview(runId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    setBusy(true);
    setError(null);
    try {
      await stopPreview(runId);
      setPreview({ active: false });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm text-zinc-300">Local preview</span>
        {preview.active ? (
          <button
            type="button"
            onClick={handleStop}
            disabled={busy}
            className="rounded border border-rose-700 hover:bg-rose-950/40 px-2 py-0.5 text-xs text-rose-300 disabled:opacity-50"
          >
            Stop preview
          </button>
        ) : (
          <button
            type="button"
            onClick={handleStart}
            disabled={busy}
            className="rounded border border-cyan-700 hover:bg-cyan-950/40 px-2 py-0.5 text-xs text-cyan-300 disabled:opacity-50"
          >
            {busy ? 'Starting preview…' : 'Preview locally'}
          </button>
        )}
      </div>

      {preview.active && preview.url && (
        <a
          href={preview.url}
          target="_blank"
          rel="noreferrer"
          className="text-cyan-400 underline text-sm"
        >
          {preview.url}
        </a>
      )}

      {!preview.active && !busy && (
        <p className="text-xs text-zinc-500">
          Launches the generated app with <code>npm run dev</code> and gives you a
          link. Stops itself after 30 minutes idle.
        </p>
      )}

      {error && <p className="text-xs text-rose-300">{error}</p>}
    </div>
  );
}
```

- [ ] **Step 2: Wire it into the run detail page**

In `frontend/app/runs/[id]/page.tsx`:

Add the import near the other imports (after the `ActivityStream` import):

```tsx
import { PreviewPanel } from '@/components/PreviewPanel';
```

Add a previewable-status check just before the `return` (after the `const finished = ...` line):

```tsx
  const previewable = ['built', 'deployed', 'deploy_failed'].includes(run.status);
```

Render the panel between the run-info card (the `</div>` closing the card at line ~114) and `<ActivityStream .../>`:

```tsx
      {previewable && <PreviewPanel runId={runId} />}
```

So the JSX tail reads:

```tsx
        <p className="text-xs text-zinc-500">workspace: {run.workspace}</p>
      </div>

      {previewable && <PreviewPanel runId={runId} />}

      <ActivityStream runId={runId} alreadyFinished={finished} />
    </div>
  );
```

- [ ] **Step 3: Typecheck / build**

Run (from `frontend/`): `npm run build`
Expected: compiles with no type errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/components/PreviewPanel.tsx frontend/app/runs/[id]/page.tsx
git commit -m "feat(ui): Preview-locally panel on the run detail page"
```

---

## Task 9: End-to-end verification + docs

**Files:**
- Modify: `CLAUDE.md` (status section), `SHIP-IT_BUILD_PLAN.md` (milestone note)

- [ ] **Step 1: Full backend suite green**

Run (from `backend/`): `.\.venv\Scripts\python.exe -m pytest -q`
Expected: all tests pass (32 prior + new preview/proc/store/server tests).

- [ ] **Step 2: Manual end-to-end check**

Start both servers (backend `uvicorn app.server:app --port 8000`, frontend `npm run dev`). In the dashboard, open a `built` run (e.g. run #9). Verify:
- the **Local preview** card shows a **Preview locally** button;
- clicking it shows "Starting preview…", then a clickable `http://localhost:43xx` link;
- the link opens the generated app;
- **Stop preview** returns the card to the button state;
- a non-previewable run (e.g. an `errored` one) shows **no** preview card.

Confirm a free port was chosen in 4300–4399 and `_preview.log` appears in the run's workspace.

- [ ] **Step 3: Update `CLAUDE.md`**

Add a bullet under "Current status" describing the preview feature:

```markdown
- **Preview-locally (DONE):** the run detail page can launch a completed run's
  generated app (`built`/`deployed`/`deploy_failed`) with `npm run dev` via a
  `PreviewManager` (`backend/app/preview.py`) — one preview at a time on a free
  port in 4300–4399, surfaced as a clickable link with a Stop button
  (`frontend/components/PreviewPanel.tsx`). PID/port persist in a one-row
  `previews` table so a restart sweep (`_preview.recover()`) reaps orphans;
  an idle reaper stops it after 30 min. Endpoints: `POST/GET/DELETE
  /api/runs/{id}/preview`. The tree-kill helper moved to shared `app/proc.py`.
```

Also add `app/proc.py` and `app/preview.py` to the repository-layout tree in `CLAUDE.md`.

- [ ] **Step 4: Update `SHIP-IT_BUILD_PLAN.md`**

Add a short note in the milestone area recording the preview feature as a post-M5 enhancement (status `DONE`), consistent with how the hardening note is recorded.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md SHIP-IT_BUILD_PLAN.md
git commit -m "docs: record Preview-locally feature"
```

---

## Self-Review Notes

- **Spec coverage:** scope/statuses (Task 6 `_PREVIEWABLE_STATUSES` incl. `deploy_failed`), one-at-a-time + idempotent + replace (Task 4), idle auto-stop (Task 5), PID-tracked restart sweep (Tasks 2+5+6), `npm run dev`/port-scan/readiness (Task 3), shared `proc.py` (Task 1), 3 endpoints (Task 6), frontend panel (Tasks 7–8), error handling (Task 6 validation + Task 3 PreviewError messages surfaced in Task 8 panel), testing (Tasks 1–6). All spec sections map to a task.
- **Type consistency:** `PreviewInfo` fields (`run_id, pid, port, url, started_at, last_active`) are used identically across `preview.py`, the `_preview_dto` server helper, and the `PreviewDTO` frontend type (which omits `pid`, by design). Method names (`start/stop/status/touch/recover/reap_idle_once/start_reaper/stop_reaper`) match between class, tests, and server wiring.
- **No placeholders:** every code step contains complete code.
- **Out of scope (unchanged):** no prod build mode, no multi-preview, no auto-install — none introduced.
