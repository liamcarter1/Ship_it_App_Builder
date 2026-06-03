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
    # Snapshot before patching: `info` aliases manager._info, so touch() mutates
    # it in place. Capture the target value as a constant so the patched clock
    # doesn't drift when touch() reads-then-writes last_active.
    bumped = info.last_active + 5
    monkeypatch.setattr("app.preview.time.time", lambda: bumped)
    manager.touch()
    assert manager.status().last_active == bumped
