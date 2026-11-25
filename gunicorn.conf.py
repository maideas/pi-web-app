"""Gunicorn configuration for `make serve`.

Mirrors app.py's bind semantics: loads machine-local `.env` (real
environment variables take precedence), honors `HOST` and `PORT`, and
binds a permanent loopback listener alongside the primary LAN IP when
`HOST` is neither loopback nor a wildcard address.

Worker count is fixed at 1 because the pi subprocess and all bridge
state live in module globals; threads are kept generous because each
open browser tab pins one thread for its SSE stream.
"""

import os
import socket
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

_SINGLE_BIND_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::")


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def resolve_binds(host: str | None = None, port: int | str | None = None) -> list[str]:
    """Return gunicorn bind specs matching app.py's HOST/PORT rules."""
    resolved_port = int(os.environ.get("PORT", "5000") if port is None else port)
    resolved_host = (os.environ.get("HOST") if host is None else host) or _lan_ip()
    if resolved_host in _SINGLE_BIND_HOSTS:
        return [f"{resolved_host}:{resolved_port}"]
    return [f"{resolved_host}:{resolved_port}", f"127.0.0.1:{resolved_port}"]


workers = 1
threads = 64
bind = resolve_binds()
