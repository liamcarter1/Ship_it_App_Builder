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
