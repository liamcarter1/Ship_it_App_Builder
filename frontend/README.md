# Ship-It dashboard (Milestone 2)

A Next.js 14 (App Router) + TypeScript + Tailwind dashboard for watching
the Ship-It pipeline run in real time. It talks to the FastAPI backend at
`backend/app/server.py`.

## Run it

```bash
# terminal 1 — backend
cd backend && source .venv/bin/activate
uvicorn app.server:app --reload --port 8000

# terminal 2 — dashboard
cd frontend
npm install
npm run dev      # http://localhost:3000
```

`next.config.mjs` rewrites `/api/*` to `http://127.0.0.1:8000`, so the
dashboard calls same-origin URLs (no CORS surprises). Override with the
`NEXT_PUBLIC_BACKEND_URL` env var.

## What you can do

- **List runs** — newest first; auto-refreshes every 2s so a live run's
  status updates without a reload.
- **Start a run** — paste an idea, optionally tick *Deploy*, click *Ship
  it*. You get redirected to the live view.
- **Tail a run live** — `/runs/[id]` opens an `EventSource` against the
  SSE endpoint. The activity stream renders each `PipelineEvent` with a
  source-coloured label, exactly like the CLI printer.
- **Replay a finished run** — same view; the SSE handler replays all
  historical events from SQLite in id order, then closes.

## How the SSE stream works

The endpoint is `GET /api/runs/{id}/events/stream`. Server-side, the
handler polls `events` table every ~250 ms for rows with `id > last_seen`
and emits each as one SSE event named after its `PipelineEvent.kind`.
This is deliberately simple:

- works for runs started in the API process *and* runs started by the
  M1 CLI in another process — both write to the same SQLite file.
- avoids the replay-vs-live race window an in-memory bus tap would have.
- single source of truth = the same event log used for history.

Latency is ~250 ms, which feels live. If we ever need true push, M3+ can
swap in an in-process bus listener plus the polling fallback for
cross-process runs.

## Files of interest

- `app/page.tsx` — runs list + new-run form.
- `app/runs/[id]/page.tsx` — run detail + live view.
- `components/ActivityStream.tsx` — the `EventSource` consumer and
  per-kind renderer. Start here to understand how events flow into the UI.
- `lib/api.ts` — typed fetch helpers.
- `lib/types.ts` — DTOs that mirror `backend/app/events.py`. Keep them in
  sync as new event kinds land.

## What's next (Milestone 3)

Approval gates. The dashboard will render an inline approve/reject/notes
panel when the backend emits a `gate_open` event, and `POST` the response
to resume the pipeline. The SSE plumbing here is unchanged — gates are
just one more event kind.
