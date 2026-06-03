"""Local preview of a completed run's generated app.

`PreviewManager` owns at most one `npm run dev` process. It picks a free port,
spawns the dev server, waits for the port to accept connections, and tracks the
PID in memory + the store (so a restart can reap an orphan). One preview at a
time; starting another stops the first. An idle reaper stops a preview after a
period with no status polls.

No real npm is spawned in tests: `_spawn_dev_server`, `_wait_until_ready`, and
`kill_process_tree` are monkeypatched.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .proc import kill_process_tree

logger = logging.getLogger("shipit.preview")

PREVIEW_PORT_RANGE = range(4300, 4400)
PREVIEW_READY_TIMEOUT_S = 30.0
PREVIEW_IDLE_TIMEOUT_S = float(os.environ.get("PREVIEW_IDLE_TIMEOUT_S", "1800"))


class PreviewError(Exception):
    """A preview could not be started (npm missing, no port, never ready)."""


@dataclass
class PreviewInfo:
    run_id: int
    pid: int
    port: int
    url: str
    started_at: float
    last_active: float


def _find_free_port(port_range=PREVIEW_PORT_RANGE) -> int:
    """First port in `port_range` we can bind on localhost. Bind-test then
    release immediately; the dev server re-binds it a moment later (a tiny TOCTOU
    window, acceptable for a single-user local tool)."""
    for port in port_range:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise PreviewError(
        f"no free preview port available in range "
        f"{port_range.start}-{port_range.stop - 1}"
    )


def _resolve_npm() -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise PreviewError("npm not found on PATH")
    return npm


def _log_tail(workspace: Path, n: int = 800) -> str:
    log = workspace / "_preview.log"
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def _spawn_dev_server(workspace: Path, port: int) -> subprocess.Popen:
    """Spawn `npm run dev -- -p <port>` in `workspace`, logging to
    `_preview.log`. Returns the Popen handle (its .pid roots the tree we kill).

    Any OS-level failure (log file locked by another process, npm not
    executable, cwd vanished) is wrapped as PreviewError so the endpoint can
    surface a clean, actionable message instead of an opaque 500.
    """
    npm = _resolve_npm()
    try:
        log = open(workspace / "_preview.log", "w", encoding="utf-8")
    except OSError as e:
        raise PreviewError(f"could not open preview log file: {e}") from e
    try:
        return subprocess.Popen(
            [npm, "run", "dev", "--", "-p", str(port)],
            cwd=str(workspace),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    except OSError as e:
        log.close()
        raise PreviewError(f"could not start npm dev server: {e}") from e


def _wait_until_ready(
    proc: subprocess.Popen,
    port: int,
    workspace: Path,
    timeout: float = PREVIEW_READY_TIMEOUT_S,
) -> None:
    """Block until the port accepts a TCP connection, or raise PreviewError if
    the process exits first or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise PreviewError(
                "preview process exited before becoming ready:\n"
                + _log_tail(workspace)
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise PreviewError(f"preview did not become ready within {timeout:.0f}s")


class PreviewManager:
    """Owns at most one local preview process."""

    def __init__(self, store, *, idle_timeout_s: float = PREVIEW_IDLE_TIMEOUT_S):
        self._store = store
        self._idle_timeout_s = idle_timeout_s
        self._info: Optional[PreviewInfo] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    def status(self) -> Optional[PreviewInfo]:
        return self._info

    def touch(self) -> None:
        if self._info is not None:
            self._info.last_active = time.time()

    async def start(self, run_id: int, workspace: Path) -> PreviewInfo:
        async with self._lock:
            if self._info is not None and self._info.run_id == run_id:
                self._info.last_active = time.time()
                return self._info
            if self._info is not None:
                await self._stop_locked()

            port = _find_free_port()
            proc = await asyncio.to_thread(_spawn_dev_server, Path(workspace), port)
            try:
                await asyncio.to_thread(
                    _wait_until_ready, proc, port, Path(workspace)
                )
            except PreviewError:
                await asyncio.to_thread(kill_process_tree, proc.pid)
                raise

            now = time.time()
            info = PreviewInfo(
                run_id=run_id,
                pid=proc.pid,
                port=port,
                url=f"http://localhost:{port}",
                started_at=now,
                last_active=now,
            )
            self._info = info
            self._proc = proc
            self._store.set_active_preview(
                run_id=run_id, pid=proc.pid, port=port, started_at=now
            )
            return info

    async def stop(self) -> bool:
        async with self._lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> bool:
        if self._info is None:
            return False
        await asyncio.to_thread(kill_process_tree, self._info.pid)
        self._store.clear_active_preview()
        self._info = None
        self._proc = None
        return True

    async def recover(self) -> None:
        """Startup sweep: kill the orphaned tree from a previous process and
        clear the persisted record. Safe if the PID is already gone."""
        row = self._store.get_active_preview()
        if row is None:
            return
        await asyncio.to_thread(kill_process_tree, row["pid"])
        self._store.clear_active_preview()

    async def reap_idle_once(self) -> None:
        """Stop the preview if it has been idle past the timeout. One pass."""
        info = self._info
        if info is not None and time.time() - info.last_active > self._idle_timeout_s:
            await self.stop()

    def start_reaper(self, interval_s: float = 30.0) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reaper_loop(interval_s))

    async def stop_reaper(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None

    async def _reaper_loop(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            # Never let an unexpected error kill the loop — that would silently
            # disable idle auto-stop for the rest of the process lifetime.
            try:
                await self.reap_idle_once()
            except Exception:  # pragma: no cover - defensive safety net
                logger.exception("idle reaper pass failed; continuing")
