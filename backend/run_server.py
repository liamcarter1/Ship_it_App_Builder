"""Windows-safe launcher for the Ship-It dashboard backend.

Why this exists
---------------
The Claude Agent SDK starts the `claude` CLI as a child process. asyncio can
only spawn subprocesses on a **ProactorEventLoop**; on a SelectorEventLoop
(used by some uvicorn/Windows setups) `create_subprocess_exec` raises an
empty-message `NotImplementedError`, which the SDK surfaces as
"CLIConnectionError: Failed to start Claude Code:".

Setting the policy inside `app/server.py` is too late: uvicorn creates its
event loop via `asyncio.run(...)` and only imports the app *inside* that
already-running loop. The policy must be set in the process **before** uvicorn
starts, which is exactly what this launcher does.

Run it instead of the bare `uvicorn` command:

    python run_server.py
    python run_server.py --port 8001        # override the port

On macOS/Linux this is a thin wrapper around `uvicorn.run` and the policy line
is a no-op, so it's safe to use everywhere.

Note: reload mode is intentionally off here — the reloader runs the server in
a child process that wouldn't inherit this policy. For day-to-day use that's
fine; if you want autoreload while developing the backend, restart manually.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="Run the Ship-It dashboard backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Imported here, after the policy is set, so uvicorn's loop is created with
    # the Proactor policy already in force.
    import uvicorn

    uvicorn.run("app.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
