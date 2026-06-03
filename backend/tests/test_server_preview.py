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
