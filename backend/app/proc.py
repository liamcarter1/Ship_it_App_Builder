"""Shared process-tree teardown.

Used by the orchestrator (reaping a stalled stage's child claude/npm/Bash
tree) and the preview manager (stopping a `npm run dev` server). Lives here so
neither module imports the other just for this helper.
"""
from __future__ import annotations

try:
    import psutil
except ImportError:  # pragma: no cover - psutil should be installed
    psutil = None


def kill_process_tree(pid: int) -> None:
    """Terminate `pid` and all its descendants.

    Windows safety net: a bare `TerminateProcess` reaps only the immediate
    process, orphaning any `npm`/`node`/Bash grandchildren. psutil walks the
    tree and kills them too. Must be called while the tree is still intact
    (descendants reachable from `pid`). No-op if psutil is unavailable.
    """
    if psutil is None:
        return
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    try:
        victims = parent.children(recursive=True)
    except psutil.Error:
        victims = []
    victims.append(parent)
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    try:
        _gone, alive = psutil.wait_procs(victims, timeout=3)
    except psutil.Error:
        alive = victims
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
