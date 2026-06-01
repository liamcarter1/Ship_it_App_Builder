"""FastAPI server for the Ship-It dashboard (Milestone 2).

Routes (all under `/api`):

    GET  /api/runs                       list runs (newest first)
    GET  /api/runs/{id}                  one run's metadata
    GET  /api/runs/{id}/events           all events for that run (one-shot JSON)
    GET  /api/runs/{id}/events/stream    SSE: replay history then live tail
    POST /api/runs                       start a new pipeline run; returns {run_id}
    GET  /api/healthz                    liveness probe

The SSE endpoint polls SQLite every ~250ms for new events past the last id
the client has seen. This is deliberately simple:

- works for runs started in THIS process (via POST /api/runs) AND for runs
  started by the M1 CLI in another process — both write to the same DB.
- avoids the classic replay-vs-live race (we'd otherwise need to subscribe
  to the bus before snapshotting SQLite and then dedupe).
- single source of truth = the same event log the dashboard browses for
  historical runs.

Start the server with:

    cd backend
    uvicorn app.server:app --reload --port 8000

Configure the allowed dashboard origin via the CORS_ORIGINS env var
(comma-separated). Default: http://localhost:3000.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load the repo's .env so `uvicorn app.server:app` picks up ANTHROPIC_API_KEY
# (and any model/CORS overrides) the same way the M1 CLI does. Without this the
# server starts fine but every run fails at the first query() with an auth
# error. Search backend/.env first, then the repo-root .env.
_here = Path(__file__).resolve()
for _candidate in (_here.parents[1] / ".env", _here.parents[2] / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()

# Windows: the Claude Agent SDK spawns the `claude` CLI as a child process,
# and asyncio can only spawn subprocesses on a ProactorEventLoop. Some
# uvicorn/Windows configurations run on a SelectorEventLoop, where
# create_subprocess_exec raises an empty-message NotImplementedError — which
# the SDK surfaces as "CLIConnectionError: Failed to start Claude Code:".
# Forcing the Proactor policy at import time (before uvicorn creates its loop)
# keeps the dashboard's runs working the same way the CLI already does.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logger = logging.getLogger("shipit.server")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from .events import EventBus, PipelineEvent
from .gates import GateBroker, GateDecision
from .orchestrator import Orchestrator, OrchestratorConfig, default_workspace_for_run
from .store import Store

# One shared store + a registry of in-flight orchestrator tasks (so we can
# show "live" status on /api/runs/{id} and resolve pending gates).
_store = Store()
_active_runs: dict[int, asyncio.Task] = {}
# Shared DB-backed broker. Gate state lives in the `gates` table, so a
# server restart no longer orphans paused runs — the startup sweep
# (_recover_runs) resumes code/deploy-gated runs and interrupts the rest.
_gate_broker = GateBroker(_store)
# Set of gate names the server enforces. Order doesn't matter; presence does.
_GATED_STAGES: tuple[str, ...] = ("spec", "code", "deploy")

# How often the SSE poller checks SQLite for new rows. 250ms feels live to a
# human and keeps the DB load trivial (one indexed-range SELECT per tick).
_POLL_INTERVAL_S = 0.25

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _recover_runs()
    yield


app = FastAPI(title="Ship-It backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _row_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _event_row_to_dict(row) -> dict:
    return Store.decode_event(row)


def _resume_disposition(open_gate: Optional[str]) -> str:
    """Decide what to do with a `running` run found at startup, based on the
    gate it was paused at. Only the cheap-tail gates (code/deploy) resume."""
    return "resume" if open_gate in ("code", "deploy") else "interrupt"


def _config_from_row(row) -> OrchestratorConfig:
    raw = json.loads(row["config"]) if row["config"] else {}
    return OrchestratorConfig(
        max_review_rounds=raw.get("max_review_rounds", 3),
        deploy=raw.get("deploy", False),
        gate_broker=_gate_broker,
        gated_stages=_GATED_STAGES,
        planner_model=raw.get("planner_model"),
        scaffolder_model=raw.get("scaffolder_model"),
        coder_model=raw.get("coder_model"),
        reviewer_model=raw.get("reviewer_model"),
        deployer_model=raw.get("deployer_model"),
        stage_idle_timeout_s=raw.get("stage_idle_timeout_s", 180.0),
        stage_idle_timeout_build_s=raw.get("stage_idle_timeout_build_s", 420.0),
        stage_total_timeout_s=raw.get("stage_total_timeout_s", 900.0),
    )


def _launch_resume(run_id: int, row, open_gate: str) -> None:
    bus = EventBus()
    config = _config_from_row(row)

    async def _run() -> None:
        try:
            orchestrator = Orchestrator(bus=bus, store=_store, config=config)
            await orchestrator.resume_tail(run_id, from_gate=open_gate)
        except Exception:
            logger.exception("resume of run %s failed", run_id)
        finally:
            _active_runs.pop(run_id, None)

    _active_runs[run_id] = asyncio.create_task(_run())


async def _recover_runs() -> None:
    """On startup, reconsider every run left 'running' by a dead process:
    resume the cheap-tail gates, mark the rest interrupted."""
    _store.reset_stale_resuming()
    for row in _store.running_runs():
        run_id = row["id"]
        if not _store.claim_run_for_resume(run_id):
            continue  # another worker won the claim
        pending = _store.pending_gates(run_id)
        open_gate = pending[0] if pending else None
        if _resume_disposition(open_gate) == "resume":
            fresh = _store.get_run(run_id)  # re-read (status is now 'resuming')
            _launch_resume(run_id, fresh, open_gate)
            logger.info("resumed run %s at gate %s", run_id, open_gate)
        else:
            _store.cancel_open_gates(run_id, notes="server restarted")
            _store.mark_interrupted(run_id, error="server restarted before resumable gate")
            _store.record_event(
                run_id,
                PipelineEvent(
                    kind="run_interrupted",
                    source="orchestrator",
                    text="server restarted",
                    meta={"run_id": run_id},
                ),
            )
            logger.info("marked run %s interrupted (gate=%s)", run_id, open_gate)


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@app.get("/api/runs")
async def list_runs():
    rows = _store.list_runs(limit=100)
    out = []
    for r in rows:
        d = _row_to_dict(r) or {}
        d["live"] = r["id"] in _active_runs
        out.append(d)
    return out


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int):
    row = _store.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    d = _row_to_dict(row) or {}
    d["live"] = run_id in _active_runs
    return d


@app.get("/api/runs/{run_id}/events")
async def get_events(run_id: int):
    if _store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [_event_row_to_dict(r) for r in _store.list_events(run_id)]


@app.get("/api/runs/{run_id}/events/stream")
async def stream_events(run_id: int):
    run = _store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def gen():
        last_id = 0
        # Phase 1: replay everything we have, in id order.
        for r in _store.list_events(run_id):
            payload = _event_row_to_dict(r)
            yield f"event: {payload['kind']}\nid: {payload['id']}\ndata: {json.dumps(payload, default=str)}\n\n"
            last_id = payload["id"]

        # Phase 2: poll for new rows until pipeline_end is observed OR the
        # run is no longer flagged 'running' in the DB.
        while True:
            new_rows = _store.list_events_after(run_id, last_id)
            saw_end = False
            for r in new_rows:
                payload = _event_row_to_dict(r)
                yield f"event: {payload['kind']}\nid: {payload['id']}\ndata: {json.dumps(payload, default=str)}\n\n"
                last_id = payload["id"]
                if payload["kind"] == "pipeline_end":
                    saw_end = True
            if saw_end:
                return
            # If the DB says the run is over but we somehow didn't see
            # pipeline_end (older row, manual edit), do one trailing poll
            # then stop instead of polling forever. 'resuming' is NOT
            # terminal — a run claimed by the startup sweep is mid-resume and
            # will still emit pipeline_resumed/pipeline_end, so keep streaming.
            current = _store.get_run(run_id)
            if current is not None and current["status"] not in (None, "running", "resuming"):
                trailing = _store.list_events_after(run_id, last_id)
                for r in trailing:
                    payload = _event_row_to_dict(r)
                    yield f"event: {payload['kind']}\nid: {payload['id']}\ndata: {json.dumps(payload, default=str)}\n\n"
                    last_id = payload["id"]
                yield f"event: stream_end\ndata: {json.dumps({'reason': 'run finished'})}\n\n"
                return
            await asyncio.sleep(_POLL_INTERVAL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Disable proxy buffering (nginx etc.) so events flush immediately.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Write endpoints
# ---------------------------------------------------------------------------


class NewRunPayload(BaseModel):
    idea: str = Field(..., min_length=3, max_length=500)
    max_rounds: int = Field(3, ge=1, le=10)
    deploy: bool = False
    # Optional per-worker model overrides. Empty / null = SDK default.
    # Accepts either an alias (e.g. "sonnet", "haiku") or a full model id
    # (e.g. "claude-haiku-4-5-20251001"). The dashboard's Advanced section
    # exposes these so cost can be tuned per role.
    planner_model: Optional[str] = Field(None, max_length=100)
    scaffolder_model: Optional[str] = Field(None, max_length=100)
    coder_model: Optional[str] = Field(None, max_length=100)
    reviewer_model: Optional[str] = Field(None, max_length=100)
    deployer_model: Optional[str] = Field(None, max_length=100)


@app.post("/api/runs")
async def create_run(payload: NewRunPayload):
    idea = payload.idea.strip()
    workspace = default_workspace_for_run()
    workspace.mkdir(parents=True, exist_ok=True)

    # Create the row up front so the client can immediately open the SSE
    # stream for run_id while the pipeline kicks off in the background.
    config_dict = {
        "deploy": payload.deploy,
        "max_review_rounds": payload.max_rounds,
        "planner_model": payload.planner_model or None,
        "scaffolder_model": payload.scaffolder_model or None,
        "coder_model": payload.coder_model or None,
        "reviewer_model": payload.reviewer_model or None,
        "deployer_model": payload.deployer_model or None,
    }
    run_id = _store.create_run(idea=idea, workspace=workspace, config=config_dict)

    bus = EventBus()  # the store recorder will be added inside Orchestrator.run

    async def _run_pipeline() -> None:
        try:
            config = OrchestratorConfig(
                max_review_rounds=config_dict["max_review_rounds"],
                deploy=config_dict["deploy"],
                gate_broker=_gate_broker,
                gated_stages=_GATED_STAGES,
                planner_model=config_dict["planner_model"],
                scaffolder_model=config_dict["scaffolder_model"],
                coder_model=config_dict["coder_model"],
                reviewer_model=config_dict["reviewer_model"],
                deployer_model=config_dict["deployer_model"],
            )
            orchestrator = Orchestrator(bus=bus, store=_store, config=config)
            await orchestrator.run(idea, workspace=workspace, run_id=run_id)
        except Exception:
            # Unexpected exception — Orchestrator.run() has already recorded
            # outcome.status='errored' in the store before re-raising, so the
            # dashboard will see the failure via SSE / the runs list. Log the
            # full traceback to the server console too, so failures aren't
            # silently swallowed (the dashboard only shows the short message).
            logger.exception("run %s failed", run_id)
        finally:
            # NOTE: do NOT cancel open gates here. With DB-backed gates a task
            # that dies at a gate (e.g. graceful shutdown) must leave the gate
            # 'open' so the startup sweep can resume it. Explicit cancellation
            # only happens via POST /cancel.
            _active_runs.pop(run_id, None)

    _active_runs[run_id] = asyncio.create_task(_run_pipeline())
    return {"run_id": run_id, "workspace": str(workspace)}


# ---------------------------------------------------------------------------
# Gates (Milestone 3)
# ---------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/gates")
async def list_gates(run_id: int):
    """The names of gates currently awaiting a human decision for this run."""
    if _store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"pending": _gate_broker.pending_for(run_id)}


class GateResolution(BaseModel):
    decision: Literal["approve", "reject"]
    notes: Optional[str] = Field(None, max_length=2000)


@app.post("/api/runs/{run_id}/gate/{name}")
async def post_gate(run_id: int, name: str, body: GateResolution):
    if name not in _GATED_STAGES:
        raise HTTPException(status_code=400, detail=f"unknown gate '{name}'")
    if _store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    decision = GateDecision(approve=(body.decision == "approve"), notes=body.notes)
    if not _gate_broker.resolve(run_id, name, decision):
        # Either no gate ever opened, or it was already resolved (idempotent
        # PATCH-style retry, or two dashboards racing to approve).
        raise HTTPException(status_code=404, detail="no pending gate")
    return {"resolved": True, "decision": body.decision}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    """Reject every pending gate for this run, which aborts the pipeline
    cleanly at the current pause point. The run will end up as
    `rejected_at_<gate>` in the store."""
    if _store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    cancelled = _gate_broker.cancel_all(run_id, notes="cancelled from dashboard")
    return {"cancelled_gates": cancelled}


@app.get("/api/healthz")
async def healthz():
    return {
        "ok": True,
        "active_runs": len(_active_runs),
        "pending_gates": sum(len(_gate_broker.pending_for(rid)) for rid in _active_runs),
    }
