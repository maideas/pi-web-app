"""Flask bridge between a browser UI and `pi --mode rpc`.

Architecture:
    Browser <--SSE/POST--> Flask <--JSONL stdin/stdout--> pi --mode rpc

Single-user assumption: one active pi subprocess (for the current
project), one event stream shared by all connected browser tabs.

Multi-project: a registry (projects.json in the app dir) lists known
projects; the active one gets a `PiProcess` spawned with the project
directory as cwd. Switching projects terminates that process and spawns
a new one — all pi I/O goes through the `PiProcess` class so a process
pool (one pi per project) can replace the single active instance later.
On startup the most recently opened project (by lastOpened) is selected
and its latest session resumed.
"""

import json
import logging
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
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
APP_ROOT = Path(__file__).resolve().parent
# Machine-local configuration (PI_WEB_TRUSTED_HOSTS, HOST, PORT, ...) from
# an unmanaged .env next to app.py — gitignored, so each host can set its
# own names without touching tracked files. Real environment variables
# win, which keeps one-off overrides on the command line working.
load_dotenv(APP_ROOT / ".env")
REGISTRY_PATH = APP_ROOT / "projects.json"
# Everything the web UI may touch (projects, file browser, downloads)
# lives below this directory. Defaults to the parent of the app dir;
# override with PI_WEB_WORKSPACE. This confines the *web endpoints* —
# it is not a sandbox for the pi agent itself, which runs with full
# user privileges in the project cwd.
WORKSPACE_ROOT = Path(
    os.environ.get("PI_WEB_WORKSPACE", APP_ROOT.parent)
).resolve()


def contained(p: Path, base: Path = WORKSPACE_ROOT) -> bool:
    """True if p (already resolved) is base or lies below it."""
    return p == base or p.is_relative_to(base)

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
    """Dynamically resolve trusted Host headers on each request.

    Re-evaluates local LAN IPs and hostnames so DHCP IP renewals or
    multi-homed setups don't cause 400 Bad Request while still rejecting
    DNS-rebinding attacks from external hostnames. Independent of the
    bind address: it filters requests that already arrived, so the local
    names are trusted regardless of what HOST binds to.

    Names this host cannot discover itself must be listed in
    PI_WEB_TRUSTED_HOSTS (comma-separated). Typical case: a
    systemd-nspawn container reached by its machine name via
    nss-mymachines -- the resolvable name lives on the host, not in the
    container, so the Host header would otherwise be rejected. A leading
    dot matches all subdomains (".example.lan").
    """

    def __iter__(self):
        hosts = {"127.0.0.1", "localhost", "::1"}
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
        return iter(hosts)


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
            bufsize=1,
        )
        # Broadcast queues: every connected SSE client gets its own copy.
        self.subscribers: list[queue.Queue] = []
        self.subscribers_lock = threading.Lock()
        self.stdin_lock = threading.Lock()
        # Pending command responses, keyed by command id.
        self.pending: dict[str, queue.Queue] = {}
        self.pending_lock = threading.Lock()
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
            while resp is None:
                if not self.is_alive():
                    resp = {"success": False, "error": "pi process exited"}
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
        import time

        while self.proc.poll() is None:
            try:
                for line in self.proc.stdout:
                    line = line.rstrip("\n\r")
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("dropping non-JSON line from pi stdout: %.120s", line)
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
        # Process is gone: release the pipe fd (GC would get it eventually).
        try:
            self.proc.stdout.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Project registry (projects.json in the app dir, gitignored)
# ---------------------------------------------------------------------------


def load_projects() -> list[dict]:
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def save_projects(projects: list[dict]) -> None:
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    with tmp.open("w") as fh:
        fh.write(json.dumps(projects, indent=2) + "\n")
        fh.flush()
        os.fsync(fh.fileno())  # survive a crash right after the rename
    tmp.replace(REGISTRY_PATH)


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
    try:
        return not contained(Path(entry["path"]).expanduser().resolve())
    except OSError:
        return True


def resume_latest_session(p: PiProcess) -> None:
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
    sf = resp.get("data", {}).get("sessionFile")
    d = Path(sf).parent if sf else None
    if d is None or not d.is_dir():
        return
    remembered = current["project"].get("lastSession")
    if remembered:
        f = Path(remembered)
        # Only honor it if it still lives in this project's session dir.
        if f.is_file() and f.parent == d and str(f) != sf:
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
    list would drop the change and clobber newer fields.
    """
    project = current["project"]
    if path:
        project["lastSession"] = path
    else:
        project.pop("lastSession", None)
    registry = load_projects()
    for entry in registry:
        if entry["id"] == project["id"]:
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
    opened; if no entry qualifies, the app dir itself is (re-)registered
    as a safe fallback.
    """
    inside = [p for p in projects if not project_outside_workspace(p)]
    if inside:
        return most_recent_project(inside)
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


projects = load_projects() or seed_registry()

# Current project + its pi process. Guarded by switch_lock during switches.
current: dict = {"project": startup_project(projects), "pi": None}
current["pi"] = PiProcess(current["project"]["path"])
resume_latest_session(current["pi"])
switch_lock = threading.Lock()
# Serializes /api/auto_name: title generation takes seconds and two
# overlapping calls would both pass the "already named" check.
auto_name_lock = threading.Lock()


def pi() -> PiProcess:
    """The current pi process, respawning it first if it died.

    Without this, a crashed pi would make every RPC call time out (30 s)
    or fail on a broken pipe until the next project switch or restart.
    """
    p = current["pi"]
    if p.is_alive():
        return p
    with switch_lock:
        if current["pi"] is p and not p.is_alive():
            current["pi"] = PiProcess(current["project"]["path"])
            resume_latest_session(current["pi"])
    return current["pi"]


def project_root() -> Path:
    return Path(current["project"]["path"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.before_request
def csrf_protection():
    """Mitigate CSRF attacks: require custom X-Requested-With header on POSTs."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"success": False, "error": "CSRF validation failed: missing custom header"}), 403


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
                    event = q.get(timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            source.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream")


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
    data = request.get_json(force=True, silent=True) or {}
    command = str(data.get("command") or "").strip()
    if not command:
        return jsonify({"success": False, "error": "missing command"})
    client_id = str(data.get("id") or "").strip()
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
    return jsonify(pi().rpc_request({"type": "get_state"}))


@app.route("/stats")
def stats():
    return jsonify(pi().rpc_request({"type": "get_session_stats"}))


@app.route("/models")
def models():
    return jsonify(pi().rpc_request({"type": "get_available_models"}))


@app.route("/set_model", methods=["POST"])
def set_model():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "provider" not in body or "modelId" not in body:
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
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "level" not in body:
        return jsonify({"success": False, "error": "missing level"}), 400
    return jsonify(pi().rpc_request({"type": "set_thinking_level", "level": body["level"]}))


# Session display names live in the latest "session_info" entry of the
# JSONL (appended by pi on set_session_name), so resolving one means
# scanning the whole file. Cache by (mtime, size) — /sessions is called
# after every agent run and sessions can be many MB.
_session_name_cache: dict[str, tuple[float, int, str | None]] = {}


def _session_name(f: Path, mtime: float, size: int) -> str | None:
    key = str(f)
    cached = _session_name_cache.get(key)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    name = None
    try:
        with f.open() as fh:
            for line in fh:
                if '"type":"session_info"' in line:
                    try:
                        name = json.loads(line).get("name")
                    except json.JSONDecodeError:
                        log.debug("unparseable session_info line in %s", f)
    except OSError:
        log.debug("cannot read session file %s", f)
    _session_name_cache[key] = (mtime, size, name)
    return name


@app.route("/sessions")
def sessions():
    state_resp = pi().rpc_request({"type": "get_state"})
    if not state_resp.get("success"):
        return jsonify({"sessions": []})
    sf = state_resp.get("data", {}).get("sessionFile")
    d = Path(sf).parent if sf else None
    if d is None or not d.is_dir():
        return jsonify({"sessions": []})
    current_sf = sf
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]
    except OSError:
        return jsonify({"sessions": []})
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
    return jsonify({"sessions": out})


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
        f = Path(p)
        try:
            resolved = f.resolve()
        except OSError:
            errors.append({"path": p, "error": "invalid path"})
            continue
        if resolved.parent != session_dir or resolved.suffix != ".jsonl":
            errors.append({"path": p, "error": "not a session file"})
            continue
        try:
            resolved.unlink()
            _session_name_cache.pop(str(resolved), None)
            deleted.append(p)
        except FileNotFoundError:
            deleted.append(p)  # already gone — fine
        except OSError as e:
            errors.append({"path": p, "error": str(e)})
    return jsonify({"success": not errors, "deleted": deleted, "errors": errors})


@app.route("/switch_session", methods=["POST"])
def switch_session():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("path"), str):
        return jsonify({"success": False, "error": "missing path"}), 400
    resp = pi().rpc_request({"type": "switch_session", "sessionPath": body["path"]})
    if resp.get("success"):
        remember_session(body["path"])
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
    """Relay an extension_ui_response from the browser to pi."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("type") != "extension_ui_response" or not body.get("id"):
        return jsonify({"success": False, "error": "invalid body"}), 400
    pi().send_command(body)
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
    d = (base / request.args.get("path", "").lstrip("/")).resolve()
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
    digest and the threshold both ignore assistant replies. No-ops
    return success=false with a reason.

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
    if sum(len(t) for t in user_texts) < MIN_USER_CHARS:
        return jsonify({"success": False, "error": "not enough content"})

    # Title from the user messages only, first one nearly in full, the
    # rest truncated — assistant replies mostly restate the requests
    # and would bloat the prompt for a six-word title.
    digest = f"User: {user_texts[0][:400]}"
    if len(user_texts) > 1:
        digest += "\n" + "\n".join(f"User: {t[:240]}" for t in user_texts[1:])
    prompt = (
        "Give this chat session a short, meaningful title (at most 6 words) "
        "covering all of the user's requests, not just the latest one. "
        "Reply with the title only: no quotes, no trailing punctuation, no explanation.\n\n"
        + digest
    )
    model = data.get("model") or {}
    # Strip everything irrelevant for a six-word title: the default coding
    # system prompt, AGENTS.md context, extensions, skills, templates and
    # thinking together cost ~3.5k input tokens per call; this runs at ~50.
    cmd = [
        "pi",
        "--print",
        "--no-session",
        "--no-tools",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--thinking",
        "off",
        "--system-prompt",
        "You generate short chat-session titles.",
    ]
    if model.get("provider") and model.get("id"):
        cmd += ["--provider", model["provider"], "--model", model["id"]]
    cmd.append(prompt)
    try:
        # cwd: project-local pi config (model defaults) should apply.
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False, cwd=project_root()
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
    return jsonify(
        {
            "projects": [
                {
                    **p,
                    "current": p["id"] == current["project"]["id"],
                    # Flagged (not hidden) so the UI can grey them out
                    # and the user can still detach them.
                    "outsideWorkspace": project_outside_workspace(p),
                }
                for p in load_projects()
            ]
        }
    )


GIT_URL_RE = re.compile(r"^(https?|git|ssh)://|^git@[\w.-]+:")


def register_project(p: Path):
    """Add an existing directory to the registry (id-checked)."""
    projects = load_projects()
    for existing in projects:
        if existing["path"] == str(p):
            return jsonify({"success": False, "error": "already registered"}), 409
    entry = new_project_entry(p)
    projects.append(entry)
    save_projects(projects)
    return jsonify({"success": True, "project": entry})


@app.route("/api/projects", methods=["POST"])
def create_project():
    """Register an existing directory or create a new one as a project.

    Body: {path, gitInit?} or {gitUrl, folder?}. With `path`: an
    existing directory is registered as-is, a missing one is created
    (with optional `git init`). With `gitUrl`: the repo is cloned into
    the workspace (into `folder` if given, else the repo name derived
    from the URL). The project name is the leaf directory name.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"success": False, "error": "missing path"}), 400
    git_url = (body.get("gitUrl") or "").strip()
    if git_url:
        if not GIT_URL_RE.match(git_url) or any(c in git_url for c in " \t\n`$;&|"):
            return jsonify({"success": False, "error": "invalid git URL"}), 400
        folder = (body.get("folder") or "").strip()
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
        try:
            proc = subprocess.run(
                ["git", "clone", "--", git_url, str(p)],
                capture_output=True, text=True, timeout=600, check=False,
            )
        except subprocess.TimeoutExpired:
            return jsonify({"success": False, "error": "git clone timed out"}), 504
        if proc.returncode != 0:
            shutil.rmtree(p, ignore_errors=True)  # don't leave a half clone
            detail = (proc.stderr or "").strip().splitlines()
            return jsonify({"success": False, "error": f"git clone failed: {detail[-1] if detail else 'unknown error'}"}), 400
        return register_project(p)
    if not body.get("path"):
        return jsonify({"success": False, "error": "missing path"}), 400
    p = Path(body["path"]).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError:
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
            subprocess.run(["git", "init"], cwd=p, capture_output=True, check=False)
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
    entry = next((p for p in load_projects() if p["id"] == pid), None)
    if entry is None:
        return jsonify({"success": False, "error": "unknown project"}), 404
    if not Path(entry["path"]).is_dir():
        return jsonify({"success": False, "error": "project directory is gone"}), 400
    if project_outside_workspace(entry):
        return jsonify({"success": False, "error": "project is outside the workspace"}), 403
    with switch_lock:
        if current["project"]["id"] == pid:
            return jsonify({"success": True, "project": entry})
        old = current["pi"]
        old.broadcast({"type": "project_switched", "project": entry})
        # Remember as most-recently-opened (restored on next app start).
        projects = load_projects()
        for p in projects:
            if p["id"] == pid:
                p["lastOpened"] = datetime.now(timezone.utc).isoformat()
        save_projects(projects)
        current["project"] = entry
        current["pi"] = PiProcess(entry["path"])
        resume_latest_session(current["pi"])
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
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if top.returncode != 0:
            return None
        repo = Path(top.stdout.strip())
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--ignored", "-z"],
            capture_output=True, text=True, timeout=10, check=False,
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
        # Re-root repo-relative onto project-relative; skip paths outside.
        p = (repo / rel).resolve()
        if p == root:
            continue
        if root != repo and root not in p.parents:
            continue
        statuses[str(p.relative_to(root)) + ("/" if rel.endswith("/") else "")] = status
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


def safe_path(rel: str) -> Path:
    """Resolve rel under the current project root; reject escapes.

    Defense-in-depth: also rejects a project root that itself escaped
    the workspace (bad registry entry via some future code path).
    """
    root = project_root().resolve()
    if not contained(root):
        http_abort(403)
    p = (root / rel.lstrip("/")).resolve()
    if not contained(p, root):
        http_abort(403)
    return p


@app.route("/api/list")
def api_list():
    root = project_root()
    d = safe_path(request.args.get("path", ""))
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
    root = project_root()
    p = safe_path(request.args.get("path", ""))
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
    root = project_root()
    p = safe_path(request.args.get("path", ""))
    if not p.is_file():
        http_abort(404)
    rel = str(p.relative_to(root))

    def run(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=10, check=False,
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
    p = safe_path((request.get_json(silent=True) or {}).get("path", ""))
    if not p.is_file():
        http_abort(404)
    try:
        p.unlink()
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
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        return jsonify({"success": False, "error": "invalid name"}), 400
    p = safe_path(data.get("path", ""))
    if not p.is_dir():
        http_abort(404)
    target = p / name
    if not contained(target.resolve(), project_root().resolve()):
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
    return jsonify({"success": True, "path": str(target.relative_to(project_root()))})


@app.route("/api/file/save", methods=["POST"])
def api_file_save():
    """Save file content from the viewer's edit mode.

    Optimistic concurrency against agent-side edits: the client sends
    the `base` text it loaded before editing; if the file on disk no
    longer matches (e.g. pi modified it via a tool), the save is
    rejected with conflict:true and the frontend offers overwrite /
    reload / keep editing. `force` skips the check.
    """
    data = request.get_json(silent=True) or {}
    p = safe_path(data.get("path", ""))
    if not p.is_file():
        http_abort(404)
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"success": False, "error": "missing text"}), 400
    payload = text.encode("utf-8")
    if len(payload) > MAX_PREVIEW_BYTES:
        return jsonify({"success": False, "error": f"too large ({len(payload)} bytes)"}), 400
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
        os.chmod(tmp, stat.S_IMODE(p.stat().st_mode))
        os.replace(tmp, p)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})


@app.route("/raw/<path:rel>")
def raw(rel):
    """Serve a project file inline (image preview in the file viewer).

    Unlike /download this sends the real Content-Type so <img> tags can
    render it. `CSP: sandbox allow-scripts allow-forms` keeps directly-opened files
    (SVG, HTML) out of the app's origin — no cookies/storage/API access —
    while still letting the HTML preview iframe run the page's own JS
    (the stricter of header and iframe sandbox attribute wins, so plain
    `sandbox` here would override the iframe's allow-scripts).
    """
    p = safe_path(rel)  # containment check
    if not p.is_file():
        http_abort(404)
    resp = send_from_directory(project_root(), p.relative_to(project_root()))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms"
    return resp


@app.route("/download/<path:rel>")
def download(rel):
    p = safe_path(rel)  # containment check
    if not p.is_file():
        http_abort(404)
    return send_from_directory(project_root(), p.relative_to(project_root()), as_attachment=True)


# ---------------------------------------------------------------------------
# Static frontend (production build of the Svelte app)
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    resp = send_from_directory(DIST_DIR, "index.html")
    # Defense-in-depth against sanitizer regressions: the app renders
    # agent- and file-controlled markdown. 'unsafe-inline' styles are
    # needed for Svelte style attributes and the injected hljs themes.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: http: https:; "
        "style-src 'self' 'unsafe-inline'; frame-src 'self'; object-src 'none'"
    )
    return resp


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST") or _lan_ip()
    if host not in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        # A single bind to the LAN IP would make localhost:5000 stop
        # working (Vite dev proxy, local tools): serve loopback on a
        # second, permanent listener. All app state lives in module
        # globals, so the extra HTTP listener shares everything.
        from werkzeug.serving import make_server

        loopback = make_server("127.0.0.1", port, app, threaded=True)
        threading.Thread(target=loopback.serve_forever, daemon=True).start()
    app.run(host=host, port=port, threaded=True)
