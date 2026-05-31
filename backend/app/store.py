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
    error TEXT,
    config TEXT
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
"""


def default_db_path() -> Path:
    return Path(
        os.environ.get(
            "SHIPIT_DB",
            str(Path(__file__).resolve().parents[1] / "shipit.db"),
        )
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that CREATE TABLE IF NOT EXISTS can't add to existing DBs."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "config" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN config TEXT")


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


class Store:
    """Thin wrapper that owns the DB path and exposes per-run helpers."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or default_db_path()

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

    def get_run_config(self, run_id: int) -> dict:
        row = self.get_run(run_id)
        if row is None or row["config"] is None:
            return {}
        return json.loads(row["config"])

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

    def list_events_after(self, run_id: int, after_id: int) -> list[sqlite3.Row]:
        """Events with id > after_id, in id order. Used by the SSE poller."""
        with connect(self.db_path) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id ASC",
                    (run_id, after_id),
                ).fetchall()
            )

    @staticmethod
    def decode_event(row: sqlite3.Row) -> dict:
        """Single chokepoint for `events` row -> JSON-ready dict.

        Used by both the FastAPI server (SSE + REST) and the CLI replay path.
        Keeping it in one place means meta validation, size limits, schema
        migrations, etc. only need to change here.
        """
        return {
            "id": row["id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "source": row["source"],
            "text": row["text"],
            "meta": json.loads(row["meta"] or "{}"),
        }


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
