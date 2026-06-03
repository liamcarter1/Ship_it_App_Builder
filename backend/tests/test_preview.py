"""PreviewManager + helpers. No real npm is ever spawned: spawn, readiness,
and kill are monkeypatched at the module level."""
from __future__ import annotations

import socket

import pytest

import app.preview as preview
from app.preview import PreviewError, _find_free_port


def test_find_free_port_returns_a_bindable_port():
    port = _find_free_port(range(4300, 4400))
    # We can actually bind it (it was free at selection time).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


def test_find_free_port_raises_when_range_exhausted():
    # An empty range can never yield a port.
    with pytest.raises(PreviewError):
        _find_free_port(range(0, 0))
