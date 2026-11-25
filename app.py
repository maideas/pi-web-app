"""Flask bridge between a browser UI and `pi --mode rpc`.

Architecture:
    Browser <--SSE/POST--> Flask <--JSONL stdin/stdout--> pi --mode rpc

Single-user assumption: one active pi subprocess (for the current
project), one event stream shared by all connected browser tabs.

Multi-project: a registry (projects-<port>.json in the app dir, one
per instance) lists known projects; the active one gets a `PiProcess`
spawned with the project directory as cwd. Switching projects terminates
that process and spawns a new one — all pi I/O goes through the
`PiProcess` class so a process pool (one pi per project) can replace
the single active instance later. Running several instances on
different ports is the cheap way to host agents in multiple projects
simultaneously; the port-keyed registry keeps their state separate.
On startup the most recently opened project (by lastOpened) is selected
and its latest session resumed.
"""

import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from flask import abort as http_abort

PI_CMD = ["pi", "--mode", "rpc"]
DIST_DIR = "web/dist"
# Auto-naming threshold: combined length of the session's user messages.
MIN_USER_CHARS = 120
# Upper bound for how long a /bash waiter thread polls for the command
# result before giving up — a live-but-unresponsive pi process would
# otherwise leak one thread per command. Generous on purpose: builds
# and test suites legitimately run for many minutes.
BASH_WAIT_TIMEOUT = 3600.0
APP_ROOT = Path(__file__).resolve().parent
# Machine-local configuration (PI_WEB_TRUSTED_HOSTS, HOST, PORT, ...) from
# an unmanaged .env next to app.py — gitignored, so each host can set its
# own names without touching tracked files. Real environment variables
# win, which keeps one-off overrides on the command line working.
load_dotenv(APP_ROOT / ".env")
# Keyed by port: each instance (each port) keeps its own project list,
# pinned sessions and remembered files, so several `PORT=... make serve`
# instances can run concurrently without racing on one file — switch_lock
# serializes registry writers within one process only, not across
# processes. The "5000" default matches the Makefile's PORT default.
# Note: a `make serve PORT=...` override only reaches this lookup because
# the Makefile exports PORT (command-line make variables are not part of
# recipe environments by default).
REGISTRY_PATH = APP_ROOT / f"projects-{os.environ.get('PORT', '5000')}.json"
# Everything the web UI may touch (projects, file browser, downloads)
# lives below this directory. Defaults to the parent of the app dir;
# override with PI_WEB_WORKSPACE. This confines the *web endpoints* —
# it is not a sandbox for the pi agent itself, which runs with full
# user privileges in the project cwd.
WORKSPACE_ROOT = Path(
    os.environ.get("PI_WEB_WORKSPACE", APP_ROOT.parent)
).resolve()


def contained(p: Path, base: Path | None = None) -> bool:
    """True if p (already resolved) is base (default WORKSPACE_ROOT) or lies below it."""
    root = WORKSPACE_ROOT if base is None else base
    return p == root or p.is_relative_to(root)

app = Flask(__name__, static_folder=DIST_DIR, static_url_path="")
# The machine's primary LAN address (falls back to loopback). Used as
# the default bind address so the server is reachable by its LAN IP
# without listening on all interfaces.
def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))  # no traffic sent; just picks a route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class DynamicTrustedHosts:
    """Dynamically resolve trusted Host headers, cached for a short TTL.

    Re-evaluates local LAN IPs and hostnames so DHCP IP renewals or
    multi-homed setups don't cause 400 Bad Request while still rejecting
    DNS-rebinding attacks from external hostnames. Independent of the
    bind address: it filters requests that already arrived, so the local
    names are trusted regardless of what HOST binds to.

    Flask evaluates TRUSTED_HOSTS on every request, and resolving the
    set involves DNS lookups (getfqdn/gethostbyname_ex can block for
    seconds on hosts with slow or broken reverse DNS) — so the set is
    cached and only rebuilt after TTL seconds instead of per request.

    Names this host cannot discover itself must be listed in
    PI_WEB_TRUSTED_HOSTS (comma-separated). Typical case: a
    systemd-nspawn container reached by its machine name via
    nss-mymachines -- the resolvable name lives on the host, not in the
    container, so the Host header would otherwise be rejected. A leading
    dot matches all subdomains (".example.lan").
    """

    TTL = 60.0  # seconds; short enough to track DHCP renewals

    def __init__(self):
        self._hosts: set[str] | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _resolve(self) -> set[str]:
        # No IPv6 literals: Werkzeug's host_is_trusted() cuts every
        # trusted entry at its first ':' (ref.partition(":")), so an
        # IPv6 address can never match and `Host: [::1]:5000` always
        # gets a 400. The loopback listener binds 127.0.0.1 only and
        # the LAN bind is IPv4, so nothing reaches us over IPv6 anyway.
        hosts = {"127.0.0.1", "localhost"}
        for extra in os.environ.get("PI_WEB_TRUSTED_HOSTS", "").split(","):
            if extra.strip():
                hosts.add(extra.strip())
        for name in (socket.gethostname(), socket.getfqdn()):
            if not name or name == "localhost":
                continue
            hosts.add(name)
            hosts.add(f"{name.partition('.')[0]}.local")
            try:
                hosts.update(socket.gethostbyname_ex(name)[2])
            except OSError:
                pass
        hosts.add(_lan_ip())
        bind_host = os.environ.get("HOST")
        if bind_host and bind_host not in ("0.0.0.0", "::"):
            hosts.add(bind_host)
        return hosts

    def __iter__(self):
        now = time.monotonic()
        with self._lock:
            if self._hosts is None or now >= self._expires:
                self._hosts = self._resolve()
                self._expires = now + self.TTL
            return iter(self._hosts)


# Reject requests whose Host header is not a local name or a local IP:
# a remote page could otherwise use DNS rebinding to become same-origin
# with this app and drive the agent (the CSRF header check doesn't help
# against that).
app.config["TRUSTED_HOSTS"] = DynamicTrustedHosts()
# Cap request bodies (image attachments are base64 in JSON): without a
# limit, a single request could buffer arbitrary amounts of memory.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

log = logging.getLogger("pi_web")

# ---------------------------------------------------------------------------
# pi subprocess management
# ---------------------------------------------------------------------------


class PiProcess:
    """One `pi --mode rpc` subprocess plus its RPC bridging state.

    Owns the reader thread, the pending-command table (request/response
    correlation via UUIDs), and the SSE subscriber queues for its event
    stream.
    """

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.proc = subprocess.Popen(
            PI_CMD,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            # Never let one invalid byte in pi's output raise in the reader
            # (which would drop every event buffered in the same chunk).
            errors="replace",
            bufsize=1,
        )
        # Broadcast queues: every connected SSE client gets its own copy.
        self.subscribers: list[queue.Queue] = []
        self.subscribers_lock = threading.Lock()
        self.stdin_lock = threading.Lock()
        # Pending command responses, keyed by command id.
        self.pending: dict[str, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        # Set by stop() for intentional termination: the reader then
        # skips the pi_exited broadcast (switch flows notify tabs
        # themselves).
        self.stopped = False
        threading.Thread(target=self._reader, daemon=True).start()

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def send_command(self, cmd: dict) -> None:
        with self.stdin_lock:
            try:
                self.proc.stdin.write(json.dumps(cmd) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise RuntimeError("pi process is gone") from exc

    def rpc_request(self, cmd: dict, timeout: float = 30.0) -> dict:
        """Send a command and wait for its correlated response."""
        cmd_id = uuid.uuid4().hex
        cmd["id"] = cmd_id
        q: queue.Queue = queue.Queue()
        with self.pending_lock:
            self.pending[cmd_id] = q
        try:
            self.send_command(cmd)
            return q.get(timeout=timeout)
        except RuntimeError:
            return {"success": False, "error": "pi process unavailable", "type": "response", "id": cmd_id}
        except queue.Empty:
            return {"success": False, "error": "timeout", "type": "response", "id": cmd_id}
        finally:
            with self.pending_lock:
                self.pending.pop(cmd_id, None)

    def run_bash(self, command: str, exclude_from_context: bool, cmd_id: str | None = None) -> dict:
        """Start an RPC `bash` command without holding an HTTP request open.

        Returns immediately with the command id; a waiter thread forwards
        the correlated pi response to all SSE subscribers as a synthetic
        `bash_execution_end` event, so every tab (not just the /bash
        caller) learns the outcome, and arbitrarily long commands don't
        hit rpc_request's timeout. Streamed output arrives separately as
        pi's own `bash_execution_update` events, already broadcast by
        _reader and carrying the same id.
        """
        cmd_id = cmd_id or uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        with self.pending_lock:
            if cmd_id in self.pending:
                return {"success": False, "error": "duplicate command id"}
            self.pending[cmd_id] = q
        try:
            self.send_command(
                {
                    "type": "bash",
                    "id": cmd_id,
                    "command": command,
                    "excludeFromContext": exclude_from_context,
                }
            )
        except RuntimeError as exc:
            with self.pending_lock:
                self.pending.pop(cmd_id, None)
            return {"success": False, "error": str(exc)}

        def waiter() -> None:
            resp: dict | None = None
            deadline = time.monotonic() + BASH_WAIT_TIMEOUT
            while resp is None:
                if not self.is_alive():
                    resp = {"success": False, "error": "pi process exited"}
                elif time.monotonic() >= deadline:
                    # Live but unresponsive pi: stop leaking this thread.
                    # The pending entry is dropped below, so a very late
                    # real response has no waiter and is broadcast (and
                    # ignored) like any other unknown event.
                    resp = {
                        "success": False,
                        "error": "timed out waiting for the command result "
                        "(the command may still be running in pi)",
                    }
                else:
                    try:
                        resp = q.get(timeout=0.5)
                    except queue.Empty:
                        pass
            with self.pending_lock:
                self.pending.pop(cmd_id, None)
            self.broadcast(
                {
                    "type": "bash_execution_end",
                    "id": cmd_id,
                    "command": command,
                    "excludeFromContext": exclude_from_context,
                    "response": resp,
                }
            )

        threading.Thread(target=waiter, daemon=True).start()
        return {"success": True, "id": cmd_id}

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.subscribers_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.subscribers_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, event: dict) -> None:
        with self.subscribers_lock:
            overflowed = []
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    overflowed.append(q)
            # A full queue means the client stopped consuming; dropping
            # arbitrary mid-stream events would silently desync it (e.g.
            # a lost agent_end leaves the UI "streaming" forever). Kick
            # the subscriber instead: it gets a stream_overflow sentinel
            # (making room for it first), reconnects, and reloads state.
            for q in overflowed:
                self.subscribers.remove(q)
                log.warning("SSE subscriber overflowed; disconnecting it")
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait({"type": "stream_overflow"})
                except queue.Full:
                    pass

    def stop(self) -> None:
        """Terminate the subprocess; the reader thread exits on EOF."""
        # Mark as intentional *before* terminating, so the reader (which
        # notices the death up to ~2 s later) doesn't broadcast pi_exited
        # on top of the switch flow's own project_switched event.
        self.stopped = True
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
                self.proc.wait(timeout=1.0)  # reap; avoid a zombie
            except (OSError, subprocess.TimeoutExpired):
                log.warning("pi process did not die on kill; possible orphan")
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def _reader(self) -> None:
        """Read JSONL events from pi stdout. Split on \\n only (strict JSONL)."""
        while self.proc.poll() is None:
            try:
                for raw in self.proc.stdout:
                    line = raw.rstrip("\n\r")
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("dropping non-JSON line from pi stdout: %.120s", line)
                        continue
                    if not isinstance(event, dict):
                        log.warning("dropping non-object JSON from pi stdout: %.120s", line)
                        continue

                    # Route correlated command responses to waiting rpc_request calls.
                    if event.get("type") == "response" and event.get("id"):
                        with self.pending_lock:
                            q = self.pending.get(event["id"])
                        if q is not None:
                            q.put(event)
                            continue  # don't broadcast; consumed by the waiter

                    # Broadcast everything else to all SSE subscribers.
                    self.broadcast(event)
            except Exception:
                log.exception("reader thread error")
            time.sleep(0.2)
        # Process is gone. Fail all waiting RPC callers right away
        # instead of letting them run into their full timeout, and tell
        # the SSE subscribers: their stream is attached to this (now
        # dead) process and would otherwise keep looking alive via the
        # 15 s keepalives. Reconnecting hits /events, whose pi() call
        # respawns the process and resumes the latest session.
        # Suppressed for an intentional stop(): those flows (project
        # switch) broadcast their own event and the tabs reconnect on
        # that.
        with self.pending_lock:
            waiters = list(self.pending.items())
            self.pending.clear()
        for cmd_id, q in waiters:
            q.put(
                {
                    "success": False,
                    "error": "pi process exited",
                    "type": "response",
                    "id": cmd_id,
                }
            )
        if not self.stopped:
            self.broadcast({"type": "pi_exited"})
        # Release the pipe fd (GC would get it eventually).
        try:
            self.proc.stdout.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Project registry (projects-<port>.json in the app dir, gitignored)
# ---------------------------------------------------------------------------


def load_projects() -> list[dict]:
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def save_projects(projects: list[dict]) -> None:
    # Unique temp name (not a fixed .tmp): concurrent saves must never
    # write the same temp file. Writers additionally serialize their
    # load-modify-save cycles via switch_lock.
    fd, tmp = tempfile.mkstemp(dir=REGISTRY_PATH.parent, prefix=REGISTRY_PATH.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(projects, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # survive a crash right after the rename
        os.replace(tmp, REGISTRY_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_project_entry(path: Path) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "name": path.name,
        "path": str(path),
        "created": datetime.now(timezone.utc).isoformat(),
    }


def seed_registry() -> list[dict]:
    """First run: the app itself is the default project (dogfooding)."""
    projects = [new_project_entry(APP_ROOT)]
    save_projects(projects)
    return projects


def project_outside_workspace(entry: dict) -> bool:
    """True if a registry entry escapes WORKSPACE_ROOT (stale or hand-edited)."""
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return True  # malformed hand-edited entry: treat as untrusted
    try:
        return not contained(Path(path).expanduser().resolve())
    except OSError:
        return True


def _canonical_session_path(sf: str | None) -> str | None:
    """Resolve symlinks in a session file path (e.g. ~/.pi -> /mnt/...).

    pi's default session dir comes from $HOME (unresolved), while
    /switch_session validates and passes Path.resolve(). Canonicalizing
    everywhere keeps session paths consistent across /state, /sessions,
    lastSession, and the frontend's localStorage prompt-history keys,
    even for unflushed new sessions whose file does not exist yet.
    """
    if not sf:
        return None
    try:
        return str(Path(sf).resolve())
    except OSError:
        return sf


def resume_latest_session(p: PiProcess, project: dict | None = None) -> None:
    """Point a freshly spawned pi at the project's selected session.

    Prefers the session the user last picked (persisted in the project
    registry as "lastSession"). Falls back to the most recent session
    by mtime: pi starts a fresh session per launch; the session dir
    (derived from the fresh session's own path) holds all sessions for
    that cwd, so the latest one by mtime — excluding the just-created
    file — is the one to resume.
    """
    resp = p.rpc_request({"type": "get_state"})
    if not resp.get("success"):
        return
    sf = _canonical_session_path(resp.get("data", {}).get("sessionFile"))
    d = Path(sf).parent if sf else None
    if d is None or not d.is_dir():
        return
    remembered = (project or current["project"]).get("lastSession")
    if remembered:
        try:
            f = Path(remembered).resolve()
        except OSError:
            f = None
        # Only honor it if it still lives in this project's session dir.
        if f is not None and f.is_file() and f.parent == d and str(f) != sf:
            p.rpc_request({"type": "switch_session", "sessionPath": str(f)})
            return
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    latest = next((f for f in files if str(f) != sf), None)
    if latest is not None:
        p.rpc_request({"type": "switch_session", "sessionPath": str(latest)})


def remember_session(path: str | None) -> None:
    """Persist (or clear) the selected session for the current project.

    Loads the registry fresh: the module-level list and even
    current["project"] can be detached from what's on disk (project
    endpoints load/save their own copies), so saving the in-memory
    list would drop the change and clobber newer fields. The
    load-modify-save cycle runs under switch_lock like the project
    endpoints', so concurrent registry writers can't drop each other's
    updates (last-writer-wins on the whole file). switch_lock is not
    reentrant: never call this while holding it (resume_latest_session,
    which runs under the lock, must not call here).
    """
    project = current["project"]
    if path:
        project["lastSession"] = path
    else:
        project.pop("lastSession", None)
    with switch_lock:
        registry = load_projects()
        for entry in registry:
            if entry.get("id") == project["id"]:
                if path:
                    entry["lastSession"] = path
                else:
                    entry.pop("lastSession", None)
                break
        save_projects(registry)


def most_recent_project(projects: list[dict]) -> dict:
    """The project opened most recently (fallback: first in the list)."""
    return max(projects, key=lambda p: p.get("lastOpened") or "")


def startup_project(projects: list[dict]) -> dict:
    """The project to open on startup, confined to the workspace.

    Out-of-workspace registry entries (stale or hand-edited) are never
    opened, and neither are entries whose directory no longer exists —
    PiProcess could not spawn in a missing cwd, so a stale entry must
    not take the whole server down at startup. If no entry qualifies,
    the app dir itself is (re-)registered as a safe fallback.
    """
    usable = [
        p
        for p in projects
        if not project_outside_workspace(p) and Path(p["path"]).is_dir()
    ]
    if usable:
        return most_recent_project(usable)
    if not contained(APP_ROOT):
        raise SystemExit(
            f"no registered project inside workspace {WORKSPACE_ROOT} and the "
            "app dir is outside it too; register a project inside the "
            "workspace or adjust PI_WEB_WORKSPACE"
        )
    log.warning("no project inside workspace %s; falling back to app dir", WORKSPACE_ROOT)
    entry = new_project_entry(APP_ROOT)
    projects.append(entry)
    save_projects(projects)
    return entry


# Current project + its pi process. Guarded by switch_lock during switches.
current: dict = {"project": None, "pi": None}
switch_lock = threading.Lock()
# Serializes /api/auto_name: title generation takes seconds and two
# overlapping calls would both pass the "already named" check.
auto_name_lock = threading.Lock()


def _init_runtime() -> None:
    """Load/seed the registry and start pi for the startup project."""
    initial_projects = load_projects() or seed_registry()
    current["project"] = startup_project(initial_projects)
    current["pi"] = PiProcess(current["project"]["path"])
    resume_latest_session(current["pi"])


# Tests set PI_WEB_TESTING=1 before importing app so importing does not
# spawn a real pi subprocess or touch the live projects-<port>.json file.
if os.environ.get("PI_WEB_TESTING") != "1":
    _init_runtime()


def pi() -> PiProcess:
    """The current pi process, respawning it first if it died.

    Without this, a crashed pi would make every RPC call time out (30 s)
    or fail on a broken pipe until the next project switch or restart.
    """
    p = current["pi"]
    if p is not None and p.is_alive():
        return p
    with switch_lock:
        if current["pi"] is p and (p is None or not p.is_alive()):
            new_pi = PiProcess(current["project"]["path"])
            resume_latest_session(new_pi, current["project"])
            current["pi"] = new_pi
    return current["pi"]


def project_root() -> Path:
    return Path(current["project"]["path"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


# Sec-Fetch-Dest values of passive subresource loads a sandboxed HTML
# preview may legitimately issue against /raw/<token>/ (see
# request_origin_checks).
RAW_SUBRESOURCE_DESTS = frozenset({"image", "style", "script", "font", "video", "audio", "track"})
# Unguessable per-process token embedded in /raw/<token>/<path> URLs.
# Because the sandboxed HTML-preview iframe has an opaque origin, its
# relative asset fetches are labelled `Sec-Fetch-Site: cross-site` by
# browsers. Requiring this secret prefix (distributed only to the
# same-origin SPA via /api/projects and /state) lets the preview resolve
# relative assets while preventing remote web pages from probing project
# files or performing XSSI against /raw/.
RAW_TOKEN = secrets.token_urlsafe(16)


def _json_dict(force: bool = False) -> dict | None:
    """Parse a JSON request body and require a JSON object (dict)."""
    body = request.get_json(force=force, silent=True)
    return body if isinstance(body, dict) else None


def _is_valid_raw_subresource_path(path: str) -> bool:
    """True if path is `/raw/<RAW_TOKEN>/<non-empty>` (constant-time token check)."""
    if not path.startswith("/raw/"):
        return False
    rest = path[len("/raw/"):]
    token, sep, tail = rest.partition("/")
    return bool(sep and tail) and secrets.compare_digest(token, RAW_TOKEN)


@app.before_request
def request_origin_checks():
    """Block CSRF and cross-site requests.

    POST/PUT/DELETE/PATCH require a custom X-Requested-With header:
    cross-origin pages can't send it without a CORS preflight, which
    this app never grants (CSRF protection).

    All methods: requests that the browser marks as cross-site via
    `Sec-Fetch-Site: cross-site` are rejected. Host validation stops
    DNS rebinding (attacker-controlled *names*), but a remote page can
    still address the server by its raw LAN IP — and GET subresources
    (<img>/<script> tags) can't carry a custom header, so the CSRF
    check alone wouldn't cover them. This blocks such probing requests
    from modern browsers. The header is absent for non-browser clients
    (curl, pi tooling) and for the Vite dev proxy's forwarded requests
    (same-origin at the browser), so those are unaffected.

    Exceptions:

    - Top-level GET navigations (`Sec-Fetch-Mode: navigate`, e.g.
      following a link to the app from another site) are allowed — the
      attacker page can't read the response, and embedding in a frame
      is already blocked by `frame-ancestors 'none'`. Browsers report
      the same site for every port on the same raw IP (`same-site`), so
      this check doesn't separate other services on the same host.
    - no-cors subresource loads (images, stylesheets, scripts, fonts,
      media) of `/raw/<RAW_TOKEN>/*` files. The HTML preview iframe is
      sandboxed without allow-same-origin, so its document has an
      *opaque* origin — and browsers label every request such a document
      makes as `cross-site` (verified in Chromium), including the
      relative <img>/<link>/<script> assets the preview exists to
      resolve. Requiring the unguessable per-process RAW_TOKEN prefix
      allows those relative loads while blocking remote pages from
      probing file existence or performing XSSI.
    """
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"success": False, "error": "CSRF validation failed: missing custom header"}), 403
    if request.headers.get("Sec-Fetch-Site") == "cross-site" and request.method == "GET":
        mode = request.headers.get("Sec-Fetch-Mode")
        if mode == "navigate":
            return None
        if (
            mode == "no-cors"
            and _is_valid_raw_subresource_path(request.path)
            and request.headers.get("Sec-Fetch-Dest") in RAW_SUBRESOURCE_DESTS
        ):
            return None
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"success": False, "error": "cross-site request rejected"}), 403
    return None


@app.after_request
def security_headers(resp):
    """Apply defense-in-depth headers across all responses."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # Keep hashed frontend assets cacheable; prevent caching of dynamic
    # API/file responses unless the route explicitly set Cache-Control
    # (e.g. index.html and /events use `no-cache`).
    if not request.path.startswith("/assets/") and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


# SSE keepalive interval: also bounds how long a stale stream's thread
# lingers after its pi process is gone (see events()).
SSE_KEEPALIVE_S = 5
_SSE_TERMINAL_EVENTS = frozenset({"stream_overflow", "project_switched", "pi_exited"})


@app.route("/events")
def events():
    """Server-sent events stream of pi RPC events.

    Subscribes to the pi process active at connect time; on a project
    switch the client receives a `project_switched` event and must
    reconnect to pick up the new process's stream.
    """
    source = pi()
    q = source.subscribe()

    def gen():
        try:
            while True:
                try:
                    event = q.get(timeout=SSE_KEEPALIVE_S)
                    yield f"data: {json.dumps(event)}\n\n"
                    # Free the worker thread right away on terminal events
                    # instead of lingering in the keepalive loop (after
                    # stream_overflow, q is already unsubscribed and would
                    # otherwise stay pinned forever).
                    if isinstance(event, dict) and event.get("type") in _SSE_TERMINAL_EVENTS:
                        return
                except queue.Empty:
                    # The process this stream belongs to is gone (switch
                    # or crash): its subscribers were already told via
                    # project_switched / pi_exited and reconnect on
                    # their own. End the stream so the worker thread is
                    # freed within one keepalive interval — under
                    # gunicorn's gthread worker every open stream pins
                    # a thread, and stale ones stall the reconnect burst.
                    if source.stopped or not source.is_alive():
                        return
                    yield ": keepalive\n\n"
        finally:
            source.unsubscribe(q)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/prompt", methods=["POST"])
def prompt():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return jsonify({"success": False, "error": "missing message"}), 400
    cmd = {"type": "prompt", "message": body["message"]}
    if body.get("streamingBehavior"):
        if body["streamingBehavior"] not in ("steer", "followUp"):
            return jsonify({"success": False, "error": "invalid streamingBehavior"}), 400
        cmd["streamingBehavior"] = body["streamingBehavior"]
    # Optional attached images: [{"data": base64, "mimeType": "image/png"}]
    images = body.get("images")
    if images:
        if not isinstance(images, list) or not all(
            isinstance(img, dict) and isinstance(img.get("data"), str) and isinstance(img.get("mimeType"), str)
            for img in images
        ):
            return jsonify({"success": False, "error": "images must be a list of {data, mimeType}"}), 400
        cmd["images"] = [
            {"type": "image", "data": img["data"], "mimeType": img["mimeType"]}
            for img in images
        ]
    resp = pi().rpc_request(cmd)
    return jsonify(resp)


@app.route("/abort", methods=["POST"])
def abort():
    return jsonify(pi().rpc_request({"type": "abort"}))


@app.route("/bash", methods=["POST"])
def bash():
    """Run a shell command via pi (`!cmd` / `!!cmd` in the UI).

    Async: the response only carries the command id; the command result
    is delivered to all tabs as a `bash_execution_end` SSE event (see
    PiProcess.run_bash). A client-chosen id lets the UI correlate
    `bash_execution_update` chunks that may arrive before this response.
    """
    data = _json_dict(force=True)
    if data is None:
        return jsonify({"success": False, "error": "missing command"}), 400
    raw_cmd = data.get("command")
    command = raw_cmd.strip() if isinstance(raw_cmd, str) else ""
    if not command:
        return jsonify({"success": False, "error": "missing command"}), 400
    raw_id = data.get("id")
    client_id = raw_id.strip() if isinstance(raw_id, str) else ""
    cmd_id = client_id if re.fullmatch(r"[A-Za-z0-9-]{1,64}", client_id) else None
    return jsonify(pi().run_bash(command, bool(data.get("excludeFromContext")), cmd_id))


@app.route("/abort_bash", methods=["POST"])
def abort_bash():
    """Abort a running /bash command (pi cancels all running ones)."""
    return jsonify(pi().rpc_request({"type": "abort_bash"}))


@app.route("/new_session", methods=["POST"])
def new_session():
    resp = pi().rpc_request({"type": "new_session"})
    if resp.get("success"):
        # A fresh session is active; the mtime fallback picks it up.
        remember_session(None)
    return jsonify(resp)


@app.route("/messages")
def messages():
    resp = pi().rpc_request({"type": "get_messages"})
    return jsonify(resp)


@app.route("/state")
def state():
    resp = pi().rpc_request({"type": "get_state"})
    if resp.get("success") and isinstance(resp.get("data"), dict):
        sf = _canonical_session_path(resp["data"].get("sessionFile"))
        if sf:
            resp["data"]["sessionFile"] = sf
    if isinstance(resp, dict):
        resp["rawToken"] = RAW_TOKEN
    return jsonify(resp)


@app.route("/stats")
def stats():
    return jsonify(pi().rpc_request({"type": "get_session_stats"}))


@app.route("/models")
def models():
    return jsonify(pi().rpc_request({"type": "get_available_models"}))


@app.route("/set_model", methods=["POST"])
def set_model():
    body = _json_dict()
    if body is None or not isinstance(body.get("provider"), str) or not isinstance(body.get("modelId"), str):
        return jsonify({"success": False, "error": "missing provider or modelId"}), 400
    return jsonify(
        pi().rpc_request(
            {"type": "set_model", "provider": body["provider"], "modelId": body["modelId"]}
        )
    )


@app.route("/thinking_levels")
def thinking_levels():
    return jsonify(pi().rpc_request({"type": "get_available_thinking_levels"}))


@app.route("/set_thinking", methods=["POST"])
def set_thinking():
    body = _json_dict()
    if body is None or not isinstance(body.get("level"), str):
        return jsonify({"success": False, "error": "missing level"}), 400
    return jsonify(pi().rpc_request({"type": "set_thinking_level", "level": body["level"]}))


# Session display names live in the latest "session_info" entry of the
# JSONL (appended by pi on set_session_name), so resolving one means
# scanning the whole file. Cache by (mtime, size) — /sessions is called
# after every agent run and sessions can be many MB. Bounded: entries
# not touched by a recent lookup are evicted (see _prune_name_cache),
# so deleted sessions and switched-away projects don't accumulate.
_session_name_cache: dict[str, tuple[float, int, str | None]] = {}
_session_name_cache_lock = threading.Lock()
_SESSION_NAME_CACHE_MAX = 200


def _prune_name_cache(keep: set[str]) -> None:
    """Cap the cache: over the limit, drop entries outside `keep`."""
    with _session_name_cache_lock:
        if len(_session_name_cache) <= _SESSION_NAME_CACHE_MAX:
            return
        for key in [k for k in _session_name_cache if k not in keep]:
            _session_name_cache.pop(key, None)


def _session_name(f: Path, mtime: float, size: int) -> str | None:
    key = str(f)
    with _session_name_cache_lock:
        cached = _session_name_cache.get(key)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    name = None
    try:
        with f.open(errors="replace") as fh:
            for line in fh:
                # Substring test is only a cheap pre-filter: a chat
                # message containing the literal would match too, so the
                # parsed entry's type must be checked before its name is
                # trusted (an attacker-influenced message could otherwise
                # spoof or reset the display name).
                if '"type":"session_info"' in line:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug("unparseable session_info line in %s", f)
                        continue
                    if isinstance(entry, dict) and entry.get("type") == "session_info":
                        name = entry.get("name")
    except OSError:
        log.debug("cannot read session file %s", f)
    with _session_name_cache_lock:
        _session_name_cache[key] = (mtime, size, name)
    return name


@app.route("/sessions")
def sessions():
    state_resp = pi().rpc_request({"type": "get_state"})
    if not state_resp.get("success"):
        return jsonify({"sessions": [], "currentSession": None})
    current_sf = _canonical_session_path(state_resp.get("data", {}).get("sessionFile"))
    d = Path(current_sf).parent if current_sf else None
    if d is None or not d.is_dir():
        return jsonify({"sessions": [], "currentSession": current_sf})
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]
    except OSError:
        return jsonify({"sessions": [], "currentSession": current_sf})
    out = []
    for f in files:
        try:
            st = f.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = 0, -1
        name = _session_name(f, mtime, size)
        out.append(
            {
                "path": str(f),
                "name": name or f.stem,
                "mtime": mtime,
                "current": str(f) == current_sf,
            }
        )
    _prune_name_cache({s["path"] for s in out})
    return jsonify({"sessions": out, "currentSession": current_sf})


@app.route("/delete_sessions", methods=["POST"])
def delete_sessions():
    """Delete session files from disk (including the currently active one).

    Paths are validated against the current session directory so the
    endpoint can't be abused to delete arbitrary files. Deleting the
    active session's file is allowed — the frontend switches to another
    session (or a new one) right away.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("paths"), list):
        return jsonify({"success": False, "error": "missing paths"}), 400
    state_resp = pi().rpc_request({"type": "get_state"})
    sf = state_resp.get("data", {}).get("sessionFile") if state_resp.get("success") else None
    if not sf:
        return jsonify({"success": False, "error": "no active session"}), 500
    session_dir = Path(sf).parent.resolve()
    deleted, errors = [], []
    for p in body["paths"]:
        if not isinstance(p, str):
            continue
        if "\x00" in p:
            errors.append({"path": p, "error": "invalid path"})
            continue
        f = Path(p)
        try:
            resolved = f.resolve()
        except (OSError, ValueError):
            errors.append({"path": p, "error": "invalid path"})
            continue
        if resolved.parent != session_dir or resolved.suffix != ".jsonl":
            errors.append({"path": p, "error": "not a session file"})
            continue
        try:
            resolved.unlink()
            with _session_name_cache_lock:
                _session_name_cache.pop(str(resolved), None)
            deleted.append(p)
        except FileNotFoundError:
            deleted.append(p)  # already gone — fine
        except OSError as e:
            errors.append({"path": p, "error": str(e)})
    return jsonify({"success": not errors, "deleted": deleted, "errors": errors})


@app.route("/switch_session", methods=["POST"])
def switch_session():
    """Switch pi to another session file.

    Like /delete_sessions, the path is validated against the current
    session directory (resolved, must be a *.jsonl directly in that
    dir) — otherwise the endpoint could point pi at any readable file
    on disk and leak its content via /messages.
    """
    body = _json_dict()
    if body is None or not isinstance(body.get("path"), str):
        return jsonify({"success": False, "error": "missing path"}), 400
    if "\x00" in body["path"]:
        return jsonify({"success": False, "error": "invalid path"}), 400
    state_resp = pi().rpc_request({"type": "get_state"})
    sf = state_resp.get("data", {}).get("sessionFile") if state_resp.get("success") else None
    if not sf:
        return jsonify({"success": False, "error": "no active session"}), 500
    session_dir = Path(sf).parent.resolve()
    try:
        resolved = Path(body["path"]).resolve()
    except (OSError, ValueError):
        return jsonify({"success": False, "error": "invalid path"}), 400
    if resolved.parent != session_dir or resolved.suffix != ".jsonl":
        return jsonify({"success": False, "error": "not a session file"}), 403
    resp = pi().rpc_request({"type": "switch_session", "sessionPath": str(resolved)})
    if resp.get("success"):
        remember_session(str(resolved))
    return jsonify(resp)


@app.route("/commands")
def commands():
    """Slash commands invocable via prompt (extension commands, templates, skills)."""
    return jsonify(pi().rpc_request({"type": "get_commands"}))


@app.route("/compact", methods=["POST"])
def compact():
    body = request.get_json(silent=True)
    cmd = {"type": "compact"}
    if isinstance(body, dict) and body.get("customInstructions"):
        if not isinstance(body["customInstructions"], str):
            return jsonify({"success": False, "error": "invalid customInstructions"}), 400
        cmd["customInstructions"] = body["customInstructions"]
    # Compaction runs a model call over the whole context — far slower
    # than the default RPC timeout.
    return jsonify(pi().rpc_request(cmd, timeout=300))


@app.route("/set_session_name", methods=["POST"])
def set_session_name():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return jsonify({"success": False, "error": "missing name"}), 400
    return jsonify(pi().rpc_request({"type": "set_session_name", "name": body["name"]}))


@app.route("/export_html", methods=["POST"])
def export_html():
    # Rendering a large session to HTML can exceed the default timeout.
    return jsonify(pi().rpc_request({"type": "export_html"}, timeout=300))


@app.route("/ui-response", methods=["POST"])
def ui_response():
    """Relay an extension_ui_response from the browser to pi.

    Only the response fields pi's dialog methods define (value,
    confirmed, cancelled) are forwarded — whitelisting keeps the RPC
    stdin surface tight instead of relaying arbitrary client JSON.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("type") != "extension_ui_response" or not isinstance(body.get("id"), str):
        return jsonify({"success": False, "error": "invalid body"}), 400
    cmd = {"type": "extension_ui_response", "id": body["id"]}
    for key in ("value", "confirmed", "cancelled"):
        if key in body:
            cmd[key] = body[key]
    try:
        pi().send_command(cmd)
    except RuntimeError as exc:  # pipe broke between the liveness check and the write
        return jsonify({"success": False, "error": str(exc)}), 503
    return "", 204


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@app.route("/api/dirs")
def api_dirs():
    """Directory picker for the new-project dialog.

    Lists subdirectories (only directories, dotdirs excluded) confined
    to WORKSPACE_ROOT — the same scope create_project accepts.
    """
    base = WORKSPACE_ROOT
    rel = request.args.get("path", "")
    if "\x00" in rel:
        http_abort(400)
    try:
        d = (base / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        http_abort(400)
    if not contained(d, base):
        http_abort(403)
    if not d.is_dir():
        http_abort(404)
    entries = []
    try:
        for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            try:
                if f.is_dir() and not f.name.startswith("."):
                    entries.append({"name": f.name, "path": str(f.relative_to(base))})
            except OSError:
                continue
    except OSError:
        http_abort(500)
    return jsonify(
        {
            "base": str(base),
            "path": str(d.relative_to(base)) if d != base else "",
            "parent": None if d == base else str(d.parent.relative_to(base))
            if d.parent != base
            else "",
            "entries": entries,
        }
    )


@app.route("/api/auto_name", methods=["POST"])
def auto_name():
    """Name the current session from its content via a one-shot model call.

    Without {"force": true} this only names sessions that have no display
    name yet (the frontend calls it after run ends) — an existing name,
    whether generated or set manually, is never overwritten without an
    explicit request. Content threshold: the user messages combined
    must reach MIN_USER_CHARS — user requests drive the topic, so the
    digest and the threshold both ignore assistant replies. With
    {"force": true} (manual /rename) any non-empty content suffices;
    assistant text is used as a fallback when there is no user text.
    No-ops return success=false with a reason.

    Serialized via auto_name_lock (a second tab or a /rename during an
    auto-name would otherwise rename twice). If the session changes
    while the one-shot call runs, the title is discarded instead of
    being applied to the wrong session.
    """
    body = request.get_json(silent=True)
    force = isinstance(body, dict) and bool(body.get("force"))
    if not auto_name_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "naming already in progress"})
    try:
        return _auto_name_locked(force)
    finally:
        auto_name_lock.release()


def _auto_name_locked(force: bool):
    p = pi()
    state = p.rpc_request({"type": "get_state"})
    if not state.get("success"):
        return jsonify({"success": False, "error": "no state"})
    data = state.get("data", {})
    session_file = data.get("sessionFile")
    current_name = data.get("sessionName")
    if current_name and not force:
        return jsonify({"success": False, "error": "already named", "name": current_name})

    msgs = p.rpc_request({"type": "get_messages"})

    def text_of(m: dict) -> str:
        c = m.get("content")
        if isinstance(c, str):
            return c
        return " ".join(part.get("text", "") for part in (c or []) if part.get("type") == "text")

    messages = msgs.get("data", {}).get("messages", []) if msgs.get("success") else []
    # Skip textless messages: a turn can be thinking + tool calls only.
    user_texts = [t for m in messages if m.get("role") == "user" if (t := text_of(m)).strip()]
    if sum(len(t) for t in user_texts) < (1 if force else MIN_USER_CHARS):
        # A manual /rename works with any content length — but not with
        # none at all. Fall back to assistant text so an empty-on-the-user-
        # side session can still be named.
        asst_texts = [t for m in messages if m.get("role") == "assistant" if (t := text_of(m)).strip()]
        if not (force and (user_texts or asst_texts)):
            return jsonify({"success": False, "error": "not enough content"})
        user_texts = user_texts or asst_texts

    # Title from the user messages only, first one nearly in full, the
    # rest truncated — assistant replies mostly restate the requests
    # and would bloat the prompt for a six-word title.
    digest = f"User: {user_texts[0][:400]}"
    if len(user_texts) > 1:
        digest += "\n" + "\n".join(f"User: {t[:240]}" for t in user_texts[1:])
    # The digest (chat content) is piped via stdin — pi --print
    # prepends piped stdin to the argv message (with no separator, and
    # stdin gets trimmed) — so chat text never appears in the process
    # argv (visible to all local users via /proc/*/cmdline).
    prompt = (
        "\n\nGive the chat session above a short, meaningful title (at most "
        "6 words) covering all of the user's requests, not just the latest "
        "one. Reply with the title only: no quotes, no trailing punctuation, "
        "no explanation."
    )
    model = data.get("model") or {}
    # Strip everything irrelevant for a six-word title: the default coding
    # system prompt, AGENTS.md context, extensions, skills and templates
    # together cost ~3.5k input tokens per call; this runs at ~50. Thinking
    # is left at the model's default — some endpoints reject disabled
    # reasoning ("Reasoning is mandatory for this endpoint") and the few
    # extra tokens on a once-per-session call are not worth a failed run.
    cmd = [
        "pi",
        "--print",
        "--no-session",
        "--no-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--system-prompt",
        "You generate short chat-session titles.",
    ]
    if model.get("provider") and model.get("id"):
        cmd += ["--provider", model["provider"], "--model", model["id"]]
    cmd.append(prompt)
    try:
        # cwd: project-local pi config (model defaults) should apply.
        r = subprocess.run(
            cmd, input=digest, capture_output=True, text=True, errors="replace",
            timeout=60, check=False, cwd=p.cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return jsonify({"success": False, "error": f"naming call failed: {exc}"})
    first_line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    name = first_line.strip().strip('"\'*').rstrip(".!")[:60]
    if not name:
        return jsonify({"success": False, "error": "empty name"})
    # The user may have switched or started a session while the one-shot
    # call ran — don't apply the title to the wrong session.
    now = p.rpc_request({"type": "get_state"})
    if not now.get("success") or now.get("data", {}).get("sessionFile") != session_file:
        return jsonify({"success": False, "error": "session changed during naming"})
    resp = p.rpc_request({"type": "set_session_name", "name": name})
    return jsonify(
        {"success": bool(resp.get("success")), "name": name, "previous": current_name or None}
    )


@app.route("/api/projects")
def list_projects():
    cur_id = current["project"]["id"] if current["project"] else None
    return jsonify(
        {
            "rawToken": RAW_TOKEN,
            "projects": [
                {
                    **p,
                    "current": p.get("id") == cur_id,
                    # Flagged (not hidden) so the UI can grey them out
                    # and the user can still detach them.
                    "outsideWorkspace": project_outside_workspace(p),
                }
                for p in load_projects()
            ],
        }
    )


GIT_URL_RE = re.compile(r"^(https?|git|ssh)://|^git@[A-Za-z0-9][\w.-]*:")


def _register_project_locked(p: Path):
    """Add an existing directory to the registry; caller must hold switch_lock."""
    projects = load_projects()
    for existing in projects:
        if existing.get("path") == str(p):
            return jsonify({"success": False, "error": "already registered"}), 409
    entry = new_project_entry(p)
    projects.append(entry)
    save_projects(projects)
    return jsonify({"success": True, "project": entry})


def register_project(p: Path):
    """Add an existing directory to the registry (id-checked).

    Serialized via switch_lock like the other registry writers, so a
    registration racing a session switch or a lastSession/lastFile
    write can't drop either side's update (last-writer-wins on the
    whole file). No caller holds the lock (the git clone in
    create_project runs outside it).
    """
    with switch_lock:
        return _register_project_locked(p)


@app.route("/api/projects", methods=["POST"])
def create_project():
    """Register an existing directory or create a new one as a project.

    Body: {path, gitInit?} or {gitUrl, folder?}. With `path`: an
    existing directory is registered as-is, a missing one is created
    (with optional `git init`). With `gitUrl`: the repo is cloned into
    the workspace (into `folder` if given, else the repo name derived
    from the URL). The project name is the leaf directory name.
    """
    body = _json_dict()
    if body is None:
        return jsonify({"success": False, "error": "missing path"}), 400
    raw_git_url = body.get("gitUrl")
    if raw_git_url is not None and not isinstance(raw_git_url, str):
        return jsonify({"success": False, "error": "invalid git URL"}), 400
    git_url = (raw_git_url or "").strip()
    if git_url:
        if not GIT_URL_RE.match(git_url) or any(ord(c) < 32 or c in " \t\n\r`$;&|" for c in git_url):
            return jsonify({"success": False, "error": "invalid git URL"}), 400
        raw_folder = body.get("folder")
        if raw_folder is not None and not isinstance(raw_folder, str):
            return jsonify({"success": False, "error": "invalid folder name"}), 400
        folder = (raw_folder or "").strip()
        if not folder:
            # Derive the folder from the URL: .../group/repo.git -> repo
            folder = re.sub(r"\.git$", "", git_url.rstrip("/").rsplit("/", 1)[-1])
        if not re.fullmatch(r"[\w.-]+", folder) or folder in (".", ".."):
            return jsonify({"success": False, "error": "invalid folder name"}), 400
        p = (WORKSPACE_ROOT / folder).resolve(strict=False)
        if p == WORKSPACE_ROOT or not contained(p):
            return jsonify({"success": False, "error": "path outside workspace"}), 403
        if p.exists():
            return jsonify({"success": False, "error": f"{p.name} already exists"}), 409
        # Clone into a private temporary directory inside WORKSPACE_ROOT
        # and rename atomically on success so a failed/racing clone can
        # never delete an existing directory at `p`.
        tmp_dir = Path(tempfile.mkdtemp(dir=WORKSPACE_ROOT, prefix=".clone-"))
        tmp_repo = tmp_dir / "repo"
        try:
            # Never block on interactive prompts (credentials, unknown SSH
            # host keys): under `make serve` from a terminal git would
            # otherwise sit on the tty for the full timeout.
            proc = subprocess.run(
                ["git", "clone", "--", git_url, str(tmp_repo)],
                capture_output=True, text=True, errors="replace", timeout=600, check=False,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"},
            )
            if proc.returncode != 0:
                detail = (proc.stderr or "").strip().splitlines()
                return jsonify({"success": False, "error": f"git clone failed: {detail[-1] if detail else 'unknown error'}"}), 400
            with switch_lock:
                if p.exists():
                    return jsonify({"success": False, "error": f"{p.name} already exists"}), 409
                os.rename(tmp_repo, p)
                return _register_project_locked(p)
        except subprocess.TimeoutExpired:
            return jsonify({"success": False, "error": "git clone timed out"}), 504
        except OSError as exc:
            return jsonify({"success": False, "error": f"git clone failed: {exc}"}), 500
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    raw_path = body.get("path")
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return jsonify({"success": False, "error": "missing path"}), 400
    p = Path(raw_path).expanduser()
    try:
        p = p.resolve(strict=False)
    except (OSError, ValueError):
        return jsonify({"success": False, "error": "invalid path"}), 400
    # Containment after resolve(): `..` and symlinks are already
    # collapsed, so a workspace-internal symlink pointing outside is
    # rejected too. The workspace root itself is not a valid project.
    if p == WORKSPACE_ROOT or not contained(p):
        return jsonify({"success": False, "error": "path outside workspace"}), 403
    if p.exists():
        if not p.is_dir():
            return jsonify({"success": False, "error": "not a directory"}), 400
    else:
        try:
            p.mkdir(parents=True)
        except OSError as exc:
            return jsonify({"success": False, "error": f"cannot create: {exc}"}), 400
        if body.get("gitInit"):
            subprocess.run(["git", "init", "-b", "main"], cwd=p, capture_output=True, check=False)
    return register_project(p)


@app.route("/api/projects/<pid>/open", methods=["POST"])
def open_project(pid):
    """Switch the active project: respawn pi with the project dir as cwd.

    Kills the running chat session (like an app restart). Other browser
    tabs learn about the switch via a `project_switched` event on the old
    process's stream and must reconnect. Stamps `lastOpened` in the
    registry (restored on next app start) and resumes the project's
    latest session.
    """
    with switch_lock:
        # Validate under the lock: the registry may change while waiting
        # for it (e.g. a concurrent detach removes this project, or its
        # directory vanishes) — only the entry read here counts.
        # The new process is spawned FIRST and current[] swapped only
        # after a successful spawn: if PiProcess raises (cwd gone between
        # the is_dir() check and the spawn, resource exhaustion), the app
        # stays consistently on the old project instead of showing the
        # new one in the file browser while chat still runs in the old
        # cwd.
        projects = load_projects()
        entry = next((p for p in projects if p["id"] == pid), None)
        if entry is None:
            return jsonify({"success": False, "error": "unknown project"}), 404
        if not Path(entry["path"]).is_dir():
            return jsonify({"success": False, "error": "project directory is gone"}), 400
        if project_outside_workspace(entry):
            return jsonify({"success": False, "error": "project is outside the workspace"}), 403
        if current["project"]["id"] == pid:
            return jsonify({"success": True, "project": entry})
        old = current["pi"]
        new_pi = PiProcess(entry["path"])
        try:
            # Resume the project's session before publishing new_pi or
            # notifying other tabs, so no request can observe the
            # freshly spawned empty session.
            resume_latest_session(new_pi, entry)
            # Remember as most-recently-opened (restored on next app start).
            for p in projects:
                if p["id"] == pid:
                    p["lastOpened"] = datetime.now(timezone.utc).isoformat()
            save_projects(projects)
        except Exception:
            new_pi.stop()
            raise
        current["project"] = entry
        current["pi"] = new_pi
        if old is not None:
            old.broadcast({"type": "project_switched", "project": entry})
            old.stop()
    return jsonify({"success": True, "project": entry})


@app.route("/api/projects/<pid>/last-file", methods=["POST"])
def set_last_file(pid):
    """Remember the file currently open in the viewer for a project.

    Stored as `lastFile` in the registry; the frontend restores it when
    the project is opened (falling back to README.md).
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("path"), str):
        return jsonify({"success": False, "error": "missing path"}), 400
    with switch_lock:
        projects = load_projects()
        entry = next((p for p in projects if p["id"] == pid), None)
        if entry is None:
            return jsonify({"success": False, "error": "unknown project"}), 404
        entry["lastFile"] = body["path"]
        save_projects(projects)
    return jsonify({"success": True})


@app.route("/api/projects/<pid>/detach", methods=["POST"])
def detach_project(pid):
    """Remove a project from the registry (the directory stays on disk).

    Detaching the currently active project is allowed: the pi process
    keeps running in the old directory (something must host it), but no
    project is marked current anymore — the frontend clears the chat and
    blocks prompts until the user actively opens another project. Other
    tabs learn via a `project_detached` broadcast.
    """
    with switch_lock:
        projects = load_projects()
        entry = next((p for p in projects if p["id"] == pid), None)
        if entry is None:
            return jsonify({"success": False, "error": "unknown project"}), 404
        projects.remove(entry)
        save_projects(projects)
        was_current = current["project"]["id"] == pid
        if was_current:
            current["pi"].broadcast({"type": "project_detached", "project": entry})
    return jsonify({"success": True, "wasCurrent": was_current})


# ---------------------------------------------------------------------------
# Project file browser / download
# ---------------------------------------------------------------------------

MAX_PREVIEW_BYTES = 512 * 1024


def git_status_map(root: Path) -> dict[str, str] | None:
    """Map project-relative paths to a git status:
    modified/added/untracked/ignored.

    Returns None (instead of {}) when the project is not inside a git
    work tree, so callers can distinguish "clean repo" from "no repo".

    Runs one repo-wide `git status --porcelain -z` and re-roots the paths
    onto the project root (which may be a subdirectory of the repo).
    Deleted files are
    skipped: they don't appear in directory listings anyway. An untracked
    directory is reported by git as a single `?? dir/` entry, which marks
    the whole subtree via the prefix aggregation in api_list; the same
    holds for ignored directories (`!! dir/` via --ignored).
    """
    root_resolved = root.resolve()
    try:
        top = subprocess.run(
            ["git", "-C", str(root_resolved), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, errors="replace", timeout=5, check=False,
        )
        if top.returncode != 0:
            return None
        repo = Path(top.stdout.strip()).resolve()
        out = subprocess.run(
            ["git", "-C", str(root_resolved), "status", "--porcelain", "--ignored", "-z"],
            capture_output=True, text=True, errors="replace", timeout=10, check=False,
        )
        if out.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None

    statuses: dict[str, str] = {}
    fields = out.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, rel = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # rename/copy: skip the extra origin-path field
        if xy == "??":
            status = "untracked"
        elif xy == "!!":
            status = "ignored"
        elif "D" in xy:
            continue  # deleted: not present in listings
        elif "A" in xy:
            status = "added"
        else:
            status = "modified"
        # Re-root repo-relative onto project-relative without dereferencing
        # leaf symlinks (an external symlink would otherwise escape
        # root_resolved and crash relative_to, and an internal symlink
        # would mark its target instead of itself).
        p = Path(os.path.normpath(repo / rel))
        if p == root_resolved or not p.is_relative_to(root_resolved):
            continue
        statuses[str(p.relative_to(root_resolved)) + ("/" if rel.endswith("/") else "")] = status
    return statuses


# Which status wins when aggregating a directory's children. "ignored"
# ranks lowest and, unlike the others, never propagates upward from
# children: git collapses a fully ignored directory into a single
# `!! dir/` entry, so only an exact match (or an ignored ancestor dir)
# means the directory itself is ignored. Containing ignored children
# (e.g. dist/ inside a tracked dir) must not mark it as ignored.
_GIT_RANK = {"modified": 3, "added": 2, "untracked": 1, "ignored": 0}


def git_dir_status(statuses: dict[str, str], rel: str) -> str | None:
    """Strongest status of any path under directory `rel`, or None."""
    prefix = rel + "/"
    best = None
    for path, status in statuses.items():
        self_or_ancestor = (
            path.rstrip("/") == rel
            or (path.endswith("/") and prefix.startswith(path))
        )
        if status == "ignored":
            under = self_or_ancestor
        else:
            under = self_or_ancestor or path.startswith(prefix)
        if under and (best is None or _GIT_RANK[status] > _GIT_RANK[best]):
            best = status
    return best


def git_file_status(statuses: dict[str, str], rel: str) -> str | None:
    """Status of file `rel`; inherits from an untracked/ignored ancestor dir."""
    direct = statuses.get(rel)
    if direct:
        return direct
    for path, status in statuses.items():
        if path.endswith("/") and rel.startswith(path):
            return status
    return None


def safe_path(rel: str) -> tuple[Path, Path]:
    """Resolve rel under a single resolved snapshot of the project root.

    Returns (root, resolved_target) so callers never call project_root()
    a second time (avoiding symlink mismatches and project-switch races).
    Defense-in-depth: also rejects a project root that itself escaped
    the workspace (bad registry entry via some future code path).
    """
    if not isinstance(rel, str) or "\x00" in rel:
        http_abort(400)  # e.g. {"path": 123} or embedded NUL must not 500
    root = project_root().resolve()
    if not contained(root):
        http_abort(403)
    try:
        p = (root / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        http_abort(400)
    if not contained(p, root):
        http_abort(403)
    return root, p


def _safe_delete_target(rel: str) -> tuple[Path, Path]:
    """Resolve the delete target's parent under project root without
    dereferencing the leaf entry itself, so deleting a symlink unlinks
    the symlink instead of wiping its target.
    """
    if not isinstance(rel, str) or "\x00" in rel:
        http_abort(400)
    root = project_root().resolve()
    if not contained(root):
        http_abort(403)
    stripped = rel.lstrip("/")
    if not stripped:
        return root, root
    rel_path = Path(stripped)
    if any(part == ".." for part in rel_path.parts):
        http_abort(403)
    if not rel_path.parts or rel_path.name in ("", "."):
        return root, root
    try:
        parent = (root / rel_path.parent).resolve()
    except (OSError, ValueError):
        http_abort(400)
    if not contained(parent, root):
        http_abort(403)
    return root, parent / rel_path.name


@app.route("/api/list")
def api_list():
    root, d = safe_path(request.args.get("path", ""))
    if not d.is_dir():
        http_abort(404)
    statuses = git_status_map(root)
    in_repo = statuses is not None
    if statuses is None:
        statuses = {}
    entries = []
    try:
        for f in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            try:
                st = f.stat()
                rel = str(f.relative_to(root))
                git = git_dir_status(statuses, rel) if f.is_dir() else git_file_status(statuses, rel)
                if git is None and in_repo:
                    git = "clean"  # tracked, no local changes
                entries.append(
                    {
                        "name": f.name,
                        "path": rel,
                        "dir": f.is_dir(),
                        "size": None if f.is_dir() else st.st_size,
                        "git": git,
                    }
                )
            except OSError:
                continue
    except OSError:
        http_abort(500)
    return jsonify(
        {
            "path": str(d.relative_to(root)) if d != root else "",
            "parent": None if d == root else str(d.parent.relative_to(root))
            if d.parent != root
            else "",
            "entries": entries,
        }
    )


@app.route("/api/file")
def api_file():
    root, p = safe_path(request.args.get("path", ""))
    if not p.is_file():
        http_abort(404)
    try:
        size = p.stat().st_size
        if size > MAX_PREVIEW_BYTES:
            return jsonify({"path": str(p.relative_to(root)), "text": None, "reason": f"too large ({size} bytes)"})
        data = p.read_bytes()
    except OSError:
        http_abort(500)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"path": str(p.relative_to(root)), "text": None, "reason": "binary file"})
    return jsonify({"path": str(p.relative_to(root)), "text": text, "reason": None})


@app.route("/api/diff")
def api_diff():
    """Full-context git diff for one file (worktree vs HEAD).

    Uses a huge -U value so the complete file content is included as
    context — the frontend renders it as an in-file diff view. Untracked
    files are diffed against /dev/null so new content shows as additions.
    Returns {path, diff, error}; diff == "" means no changes.
    """
    root, p = safe_path(request.args.get("path", ""))
    if not p.is_file():
        http_abort(404)
    rel = str(p.relative_to(root))
    try:
        size = p.stat().st_size
    except OSError as e:
        return jsonify({"path": rel, "diff": None, "error": str(e)})
    if size > MAX_PREVIEW_BYTES:
        return jsonify({"path": rel, "diff": None, "error": f"file too large ({size} bytes)"})

    def run(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, errors="replace", timeout=10, check=False,
        )

    try:
        tracked = run("ls-files", "--error-unmatch", "--", rel).returncode == 0
        if tracked:
            out = run("diff", "-U999999", "HEAD", "--", rel)
            if out.returncode not in (0, 1):  # e.g. unborn HEAD (no commits)
                out = run("diff", "-U999999", "--", rel)
        else:
            # --no-index exits 1 when the files differ; that's the normal case.
            out = run("diff", "-U999999", "--no-index", "--", os.devnull, rel)
        if out.returncode not in (0, 1):
            msg = (out.stderr or "git diff failed").strip()
            return jsonify({"path": rel, "diff": None, "error": msg})
    except (OSError, subprocess.TimeoutExpired) as e:
        return jsonify({"path": rel, "diff": None, "error": str(e)})
    diff = out.stdout
    if len(diff) > MAX_PREVIEW_BYTES:
        diff = diff[:MAX_PREVIEW_BYTES] + "\n…(truncated)\n"
    return jsonify({"path": rel, "diff": diff, "error": None})


@app.route("/api/file/delete", methods=["POST"])
def api_file_delete():
    """Delete a file, symlink, or directory together with all its contents.

    Deleting the project root itself is rejected — that would wipe the
    whole project (and the pi session state inside it). Symlinks are
    unlinked directly without dereferencing their targets.
    """
    data = _json_dict()
    if data is None:
        return jsonify({"success": False, "error": "missing path"}), 400
    root, target = _safe_delete_target(data.get("path", ""))
    if target == root:
        return jsonify({"success": False, "error": "cannot delete the project root"}), 403
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            http_abort(404)
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


@app.route("/api/file/create", methods=["POST"])
def api_file_create():
    """Create an empty file or directory (`dir`: true) inside the project.

    Fails if the target already exists (409) so the browser buttons can
    never clobber existing content. The name must not contain path
    separators — creation happens directly in the given directory.
    """
    data = _json_dict()
    if data is None:
        return jsonify({"success": False, "error": "invalid body"}), 400
    raw_name = data.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        return jsonify({"success": False, "error": "invalid name"}), 400
    root, p = safe_path(data.get("path", ""))
    if not p.is_dir():
        http_abort(404)
    target = p / name
    try:
        resolved_target = target.resolve()
    except (OSError, ValueError):
        http_abort(400)
    if not contained(resolved_target, root):
        http_abort(403)
    try:
        if data.get("dir"):
            target.mkdir()
        else:
            target.touch(exist_ok=False)
    except FileExistsError:
        return jsonify({"success": False, "error": f"{name} already exists"}), 409
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "path": str(target.relative_to(root))})


@app.route("/api/file/save", methods=["POST"])
def api_file_save():
    """Save file content from the viewer's edit mode.

    Optimistic concurrency against agent-side edits: the client sends
    the `base` text it loaded before editing; if the file on disk no
    longer matches (e.g. pi modified it via a tool), the save is
    rejected with conflict:true and the frontend offers overwrite /
    reload / keep editing. `force` skips the check.
    """
    data = _json_dict()
    if data is None:
        return jsonify({"success": False, "error": "invalid body"}), 400
    _root, p = safe_path(data.get("path", ""))
    if not p.is_file():
        http_abort(404)
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"success": False, "error": "missing text"}), 400
    payload = text.encode("utf-8")
    if len(payload) > MAX_PREVIEW_BYTES:
        return jsonify({"success": False, "error": f"too large ({len(payload)} bytes)"}), 400
    # Capture the permission bits up front: if the file vanishes between
    # here and the write (agent-side delete), the failure mode is a
    # conflict the frontend can handle, not a 500 from the chmod.
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
    except OSError:
        return jsonify({"success": False, "conflict": True})
    if not data.get("force"):
        try:
            disk = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            disk = None
        if disk is None or disk != data.get("base"):
            return jsonify({"success": False, "conflict": True})
    # Atomic write via temp file + rename, so a concurrently running
    # agent never reads a half-written file; keep the original
    # permission bits (os.replace would otherwise apply the umask).
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


@app.route("/raw/<token>/<path:rel>")
def raw(token, rel):
    """Serve a project file inline (image/HTML preview in the file viewer).

    Requires the per-process RAW_TOKEN prefix so remote pages cannot
    guess URLs to probe project files or perform XSSI. Unlike /download
    this sends the real Content-Type so <img> tags can render it.
    `CSP: sandbox allow-scripts allow-forms` keeps directly-opened files
    (SVG, HTML) out of the app's origin — no cookies/storage/API access —
    while still letting the HTML preview iframe run the page's own JS
    (the stricter of header and iframe sandbox attribute wins, so plain
    `sandbox` here would override the iframe's allow-scripts).
    """
    if not secrets.compare_digest(token, RAW_TOKEN):
        http_abort(403)
    root, p = safe_path(rel)  # containment check
    if not p.is_file():
        http_abort(404)
    resp = send_from_directory(root, p.relative_to(root))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms"
    return resp


@app.route("/download/<path:rel>")
def download(rel):
    root, p = safe_path(rel)  # containment check
    if not p.is_file():
        http_abort(404)
    return send_from_directory(root, p.relative_to(root), as_attachment=True)


# ---------------------------------------------------------------------------
# Static frontend (production build of the Svelte app)
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    resp = send_from_directory(DIST_DIR, "index.html")
    # Always revalidate index.html: it references the hashed JS/CSS
    # bundles, so a stale copy keeps loading an outdated build. (The
    # hashed assets themselves remain safely cacheable.)
    resp.headers["Cache-Control"] = "no-cache"
    # Defense-in-depth against sanitizer regressions: the app renders
    # agent- and file-controlled markdown. 'unsafe-inline' styles are
    # needed for Svelte style attributes and the injected hljs themes.
    # img-src: remote images are allowed so README badges render in the
    # file viewer; the chat renderer never auto-loads them (prompt-
    # injected agent output could exfiltrate data via image URLs).
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; frame-src 'self'; object-src 'none'; "
        # No page may embed the app (clickjacking): a remote page could
        # otherwise iframe the LAN URL and overlay-trick clicks into it.
        "frame-ancestors 'none'"
    )
    resp.headers["X-Frame-Options"] = "DENY"  # legacy UAs without CSP2
    return resp


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST") or _lan_ip()
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        # A single bind to the LAN IP would make localhost:5000 stop
        # working (Vite dev proxy, local tools): serve loopback on a
        # second, permanent listener. All app state lives in module
        # globals, so the extra HTTP listener shares everything.
        from werkzeug.serving import make_server

        loopback = make_server("127.0.0.1", port, app, threaded=True)
        threading.Thread(target=loopback.serve_forever, daemon=True).start()
    app.run(host=host, port=port, threaded=True)
