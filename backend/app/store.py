"""SQLite-backed run state for Ship-It.

Two tables: `runs` (one row per pipeline invocation) and `events` (every
PipelineEvent we observed). Keeping events durable lets the future dashboard
replay a run after the fact, and lets the CLI re-print a past run with
`--show-run <id>` without re-invoking any agent.

Connection model: short-lived `connect()` per write. SQLite handles the
locking; we only have one writer per process anyway. WAL is left off because
the workload is tiny and we want simple file copies to remain consistent.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .events import EventBus, Listener, PipelineEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    workspace TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    total_cost_usd REAL,
    deploy_url TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT,
    meta TEXT
);

CREATE INDEX IF NOT EXISTS events_run_id_idx ON events(run_id, ts);
"""


def default_db_path() -> Path:
    return Path(
        os.environ.get(
            "SHIPIT_DB",
            str(Path(__file__).resolve().parents[1] / "shipit.db"),
        )
    )


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


class Store:
    """Thin wrapper that owns the DB path and exposes per-run helpers."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or default_db_path()

    def create_run(self, idea: str, workspace: Path) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO runs (idea, workspace, started_at) VALUES (?, ?, ?)",
                (idea, str(workspace), time.time()),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        total_cost_usd: Optional[float] = None,
        deploy_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, total_cost_usd=?, deploy_url=?, error=? WHERE id=?",
                (status, time.time(), total_cost_usd, deploy_url, error, run_id),
            )

    def record_event(self, run_id: int, event: PipelineEvent) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO events (run_id, ts, kind, source, text, meta) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    event.ts,
                    event.kind,
                    event.source,
                    event.text,
                    json.dumps(event.meta, default=str),
                ),
            )

    def make_listener(self, run_id: int) -> Listener:
        """Return an EventBus listener that writes events under `run_id`."""

        def listener(event: PipelineEvent) -> None:
            self.record_event(run_id, event)

        return listener

    # --- read helpers (used by the CLI --show-run flag and future dashboard) ---

    def get_run(self, run_id: int) -> Optional[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def list_events(self, run_id: int) -> list[sqlite3.Row]:
        with connect(self.db_path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM events WHERE run_id=? ORDER BY id ASC",
                    (run_id,),
                ).fetchall()
            )


def attach_store_to_bus(bus: EventBus, store: Store, run_id: int) -> Listener:
    """Register the store as a listener on the bus.

    Returns the listener so the caller can later `bus.remove(listener)` once
    the run ends; without that, a long-lived bus would accumulate one stale
    recorder per past run and silently duplicate every future event into
    every prior `run_id`.
    """
    listener = store.make_listener(run_id)
    bus.add(listener)
    return listener
