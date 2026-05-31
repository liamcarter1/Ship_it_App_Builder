import asyncio

import pytest

from app.events import EventBus
from app.gates import GateBroker, GateDecision
from app.orchestrator import Orchestrator, OrchestratorConfig
from app.store import Store

GATED = ("spec", "code", "deploy")


@pytest.fixture
def store(tmp_path):
    return Store(db_path=tmp_path / "t.db")


async def test_resume_tail_built(store, tmp_path):
    rid = store.create_run(idea="i", workspace=tmp_path / "ws", config={"deploy": False})
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(rid, "code", payload={"workspace": str(tmp_path / "ws")})
    store.claim_run_for_resume(rid)

    config = OrchestratorConfig(deploy=False, gate_broker=broker, gated_stages=GATED)
    orch = Orchestrator(bus=EventBus(), store=store, config=config)

    async def approve_soon():
        await asyncio.sleep(0.03)
        broker.resolve(rid, "code", GateDecision(approve=True))

    asyncio.create_task(approve_soon())
    outcome = await orch.resume_tail(rid, from_gate="code")
    assert outcome.status == "built"
    assert store.get_run(rid)["status"] == "built"


async def test_resume_tail_rejected(store, tmp_path):
    rid = store.create_run(idea="i", workspace=tmp_path / "ws", config={"deploy": False})
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(rid, "code")
    config = OrchestratorConfig(deploy=False, gate_broker=broker, gated_stages=GATED)
    orch = Orchestrator(bus=EventBus(), store=store, config=config)

    async def reject_soon():
        await asyncio.sleep(0.03)
        broker.resolve(rid, "code", GateDecision(approve=False, notes="no"))

    asyncio.create_task(reject_soon())
    outcome = await orch.resume_tail(rid, from_gate="code")
    assert outcome.status == "rejected_at_code"


async def test_resume_tail_deploy(store, tmp_path):
    rid = store.create_run(idea="i", workspace=tmp_path / "ws", config={"deploy": True})
    broker = GateBroker(store, poll_interval=0.01)
    broker.open(rid, "deploy")
    config = OrchestratorConfig(deploy=True, gate_broker=broker, gated_stages=GATED)
    orch = Orchestrator(bus=EventBus(), store=store, config=config)

    async def fake_deploy(workspace, outcome):
        return {"status": "deployed", "url": "https://x.vercel.app"}

    orch._stage_deployer = fake_deploy  # type: ignore[assignment]

    async def approve_soon():
        await asyncio.sleep(0.03)
        broker.resolve(rid, "deploy", GateDecision(approve=True))

    asyncio.create_task(approve_soon())
    outcome = await orch.resume_tail(rid, from_gate="deploy")
    assert outcome.status == "deployed"
    assert store.get_run(rid)["deploy_url"] == "https://x.vercel.app"
