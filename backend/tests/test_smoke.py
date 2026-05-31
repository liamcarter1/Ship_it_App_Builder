from app.store import Store


def test_app_package_imports(tmp_path):
    store = Store(db_path=tmp_path / "smoke.db")
    assert store.list_runs() == []
