"""Integration test for the FastAPI startup recovery sweep (`_recover_runs`).

Drives the *actual* server sweep against a real temp SQLite DB — no LLM/SDK
calls — exercising both dispositions:

- a run paused at the **code** gate is claimed and resumed to `built`;
- a run paused at the **spec** gate is marked `interrupted` and gets a
  `run_interrupted` event recorded.

This is the deterministic, cost-free stand-in for the manual "kill uvicorn and
restart" drill: it proves the recovery logic the lifespan handler calls.
"""
from __future__ import annotations

import asyncio

import pytest

import app.server as srv
from app.gates import GateBroker, GateDecision
from app.store import Store


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the server module's globals at a throwaway store/broker."""
    store = Store(db_path=tmp_path / "recover.db")
    broker = GateBroker(store, poll_interval=0.01)
    monkeypatch.setattr(srv, "_store", store)
    monkeypatch.setattr(srv, "_gate_broker", broker)
    monkeypatch.setattr(srv, "_active_runs", {})
    return store, broker


async def test_sweep_interrupts_spec_gate_run(wired, tmp_path):
    store, broker = wired
    rid = store.create_run(idea="b", workspace=tmp_path / "wb", config={"deploy": False})
    broker.open(rid, "spec")  # died before the resumable tail

    await srv._recover_runs()

    row = store.get_run(rid)
    assert row["status"] == "interrupted"
    assert store.pending_gates(rid) == []  # gate rejected by the sweep
    kinds = [Store.decode_event(r)["kind"] for r in store.list_events(rid)]
    assert "run_interrupted" in kinds


async def test_sweep_resumes_code_gate_run(wired, tmp_path):
    store, broker = wired
    rid = store.create_run(idea="a", workspace=tmp_path / "wa", config={"deploy": False})
    broker.open(rid, "code")  # paused at the resumable code gate

    await srv._recover_runs()

    # The sweep claimed the run and launched a resume task.
    assert rid in srv._active_runs
    task = srv._active_runs[rid]
    assert store.get_run(rid)["status"] == "resuming"

    # Approve the still-open code gate; the resume task should drive it home.
    broker.resolve(rid, "code", GateDecision(approve=True))
    await asyncio.wait_for(task, timeout=5.0)
    assert store.get_run(rid)["status"] == "built"


async def test_sweep_is_a_noop_on_empty_db(wired):
    store, _ = wired
    await srv._recover_runs()  # no running runs → must not raise
    assert store.running_runs() == []
