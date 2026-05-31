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
