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
