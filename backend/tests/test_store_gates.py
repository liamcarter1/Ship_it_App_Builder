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
