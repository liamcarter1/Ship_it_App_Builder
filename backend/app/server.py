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
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .events import EventBus
from .orchestrator import Orchestrator, OrchestratorConfig, default_workspace_for_run
from .store import Store

# One shared store + a registry of in-flight orchestrator tasks (so we can
# show "live" status on /api/runs/{id} and `cancel` it later in M3+).
_store = Store()
_active_runs: dict[int, asyncio.Task] = {}

# How often the SSE poller checks SQLite for new rows. 250ms feels live to a
# human and keeps the DB load trivial (one indexed-range SELECT per tick).
_POLL_INTERVAL_S = 0.25

app = FastAPI(title="Ship-It backend")
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
    return {
        "id": row["id"],
        "ts": row["ts"],
        "kind": row["kind"],
        "source": row["source"],
        "text": row["text"],
        "meta": json.loads(row["meta"] or "{}"),
    }


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
            # then stop instead of polling forever.
            current = _store.get_run(run_id)
            if current is not None and current["status"] not in (None, "running"):
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


@app.post("/api/runs")
async def create_run(payload: NewRunPayload):
    idea = payload.idea.strip()
    workspace = default_workspace_for_run()
    workspace.mkdir(parents=True, exist_ok=True)

    # Create the row up front so the client can immediately open the SSE
    # stream for run_id while the pipeline kicks off in the background.
    run_id = _store.create_run(idea=idea, workspace=workspace)

    bus = EventBus()  # the store recorder will be added inside Orchestrator.run

    async def _run_pipeline() -> None:
        try:
            config = OrchestratorConfig(
                max_review_rounds=payload.max_rounds,
                deploy=payload.deploy,
            )
            orchestrator = Orchestrator(bus=bus, store=_store, config=config)
            await orchestrator.run(idea, workspace=workspace, run_id=run_id)
        except Exception:
            # Unexpected exception — Orchestrator.run() has already recorded
            # outcome.status='errored' in the store before re-raising, so the
            # dashboard will see the failure via SSE / the runs list.
            pass
        finally:
            _active_runs.pop(run_id, None)

    _active_runs[run_id] = asyncio.create_task(_run_pipeline())
    return {"run_id": run_id, "workspace": str(workspace)}


@app.get("/api/healthz")
async def healthz():
    return {"ok": True, "active_runs": len(_active_runs)}
