import asyncio

import pytest

from app.gates import GateBroker, GateDecision
from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "t.db")


@pytest.fixture
def run_id(store, tmp_path):
    return store.create_run(idea="i", workspace=tmp_path / "ws")


async def test_round_trip(store, run_id):
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(run_id, "code", payload={"hi": 1})
    assert broker.pending_for(run_id) == ["code"]

    async def approve_soon():
        await asyncio.sleep(0.03)
        assert broker.resolve(run_id, "code", GateDecision(approve=True, notes="ok"))

    asyncio.create_task(approve_soon())
    decision = await broker.wait(run_id, "code", timeout=2.0)
    assert decision.approve is True
    assert decision.notes == "ok"
    assert broker.pending_for(run_id) == []


async def test_wait_times_out(store, run_id):
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(run_id, "code")
    with pytest.raises(asyncio.TimeoutError):
        await broker.wait(run_id, "code", timeout=0.05)


def test_resolve_missing_returns_false(store, run_id):
    broker = GateBroker(store)
    assert broker.resolve(run_id, "code", GateDecision(approve=True)) is False


def test_cancel_all(store, run_id):
    broker = GateBroker(store)
    broker.open(run_id, "code")
    broker.open(run_id, "deploy")
    assert set(broker.cancel_all(run_id, notes="x")) == {"code", "deploy"}
    assert broker.pending_for(run_id) == []
