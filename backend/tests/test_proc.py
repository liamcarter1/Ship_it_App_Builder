"""kill_process_tree lives in app.proc and is importable by both the
orchestrator and the preview manager. With psutil unavailable it is a no-op;
with a fake psutil it terminates the parent and its descendants."""
from __future__ import annotations

import app.proc as proc


def test_kill_is_noop_without_psutil(monkeypatch):
    monkeypatch.setattr(proc, "psutil", None)
    proc.kill_process_tree(12345)  # must not raise


def test_kill_terminates_parent_and_children(monkeypatch):
    calls = {"terminated": [], "killed": []}

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
        def children(self, recursive=False):
            return [FakeProc(999)] if self.pid == 1 else []
        def terminate(self):
            calls["terminated"].append(self.pid)
        def kill(self):
            calls["killed"].append(self.pid)

    class FakePsutil:
        Error = Exception
        def Process(self, pid):
            return FakeProc(pid)
        def wait_procs(self, procs, timeout=None):
            return (procs, [])  # all gone, none alive

    monkeypatch.setattr(proc, "psutil", FakePsutil())
    proc.kill_process_tree(1)
    assert set(calls["terminated"]) == {1, 999}
