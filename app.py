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
"""

import json
import queue
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from flask import abort as http_abort

PI_CMD = ["pi", "--mode", "rpc"]
DIST_DIR = "web/dist"
APP_ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = APP_ROOT / "projects.json"

app = Flask(__name__, static_folder=DIST_DIR, static_url_path="")

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

    def send_command(self, cmd: dict) -> None:
        with self.stdin_lock:
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()

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
        except queue.Empty:
            return {"success": False, "error": "timeout", "type": "response", "id": cmd_id}
        finally:
            with self.pending_lock:
                self.pending.pop(cmd_id, None)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.subscribers_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.subscribers_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, event: dict) -> None:
        with self.subscribers_lock:
            for q in self.subscribers:
                q.put(event)

    def stop(self) -> None:
        """Terminate the subprocess; the reader thread exits on EOF."""
        try:
            self.proc.terminate()
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
            except Exception as exc:
                print("reader thread error:", exc)
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Project registry (projects.json in the app dir, gitignored)
# ---------------------------------------------------------------------------


def load_projects() -> list[dict]:
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def save_projects(projects: list[dict]) -> None:
    REGISTRY_PATH.write_text(json.dumps(projects, indent=2) + "\n")


def seed_registry() -> list[dict]:
    """First run: the app itself is the default project (dogfooding)."""
    projects = [
        {
            "id": uuid.uuid4().hex[:8],
            "name": APP_ROOT.name,
            "path": str(APP_ROOT),
            "created": datetime.now(timezone.utc).isoformat(),
        }
    ]
    save_projects(projects)
    return projects


def resume_latest_session(p: PiProcess) -> None:
    """Point a freshly spawned pi at the project's most recent session.

    pi starts a fresh session per launch; the session dir (derived from
    the fresh session's own path) holds all sessions for that cwd, so
    the latest one by mtime — excluding the just-created file — is the
    one to resume.
    """
    resp = p.rpc_request({"type": "get_state"})
    if not resp.get("success"):
        return
    sf = resp.get("data", {}).get("sessionFile")
    d = Path(sf).parent if sf else None
    if d is None or not d.is_dir():
        return
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    latest = next((f for f in files if str(f) != sf), None)
    if latest is not None:
        p.rpc_request({"type": "switch_session", "sessionPath": str(latest)})


def most_recent_project(projects: list[dict]) -> dict:
    """The project opened most recently (fallback: first in the list)."""
    return max(projects, key=lambda p: p.get("lastOpened") or "")


projects = load_projects() or seed_registry()

# Current project + its pi process. Guarded by switch_lock during switches.
current: dict = {"project": most_recent_project(projects), "pi": None}
current["pi"] = PiProcess(current["project"]["path"])
resume_latest_session(current["pi"])
switch_lock = threading.Lock()


def pi() -> PiProcess:
    return current["pi"]


def project_root() -> Path:
    return Path(current["project"]["path"])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


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
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "message" not in body:
        return jsonify({"success": False, "error": "missing message"}), 400
    cmd = {"type": "prompt", "message": body["message"]}
    if body.get("streamingBehavior"):
        cmd["streamingBehavior"] = body["streamingBehavior"]
    # Optional attached images: [{"data": base64, "mimeType": "image/png"}]
    images = body.get("images")
    if images:
        if not isinstance(images, list):
            return jsonify({"success": False, "error": "images must be a list"}), 400
        cmd["images"] = [
            {"type": "image", "data": img["data"], "mimeType": img["mimeType"]}
            for img in images
        ]
    resp = pi().rpc_request(cmd)
    return jsonify(resp)


@app.route("/abort", methods=["POST"])
def abort():
    return jsonify(pi().rpc_request({"type": "abort"}))


@app.route("/new_session", methods=["POST"])
def new_session():
    return jsonify(pi().rpc_request({"type": "new_session"}))


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
    body = request.get_json(force=True)
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
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "level" not in body:
        return jsonify({"success": False, "error": "missing level"}), 400
    return jsonify(pi().rpc_request({"type": "set_thinking_level", "level": body["level"]}))


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
        # Session display name lives in the latest "session_info" entry
        # (appended by pi on set_session_name), not in the header line.
        name = None
        try:
            with f.open() as fh:
                for line in fh:
                    if '"type":"session_info"' in line:
                        try:
                            name = json.loads(line).get("name")
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            mtime = f.stat().st_mtime
        except OSError:
            mtime = 0
        out.append(
            {
                "path": str(f),
                "name": name or f.stem,
                "mtime": mtime,
                "current": str(f) == current_sf,
            }
        )
    return jsonify({"sessions": out})


@app.route("/switch_session", methods=["POST"])
def switch_session():
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "path" not in body:
        return jsonify({"success": False, "error": "missing path"}), 400
    return jsonify(pi().rpc_request({"type": "switch_session", "sessionPath": body["path"]}))


@app.route("/commands")
def commands():
    """Slash commands invocable via prompt (extension commands, templates, skills)."""
    return jsonify(pi().rpc_request({"type": "get_commands"}))


@app.route("/compact", methods=["POST"])
def compact():
    body = request.get_json(force=True, silent=True)
    cmd = {"type": "compact"}
    if isinstance(body, dict) and body.get("customInstructions"):
        cmd["customInstructions"] = body["customInstructions"]
    return jsonify(pi().rpc_request(cmd))


@app.route("/set_session_name", methods=["POST"])
def set_session_name():
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "name" not in body:
        return jsonify({"success": False, "error": "missing name"}), 400
    return jsonify(pi().rpc_request({"type": "set_session_name", "name": body["name"]}))


@app.route("/export_html", methods=["POST"])
def export_html():
    return jsonify(pi().rpc_request({"type": "export_html"}))


@app.route("/ui-response", methods=["POST"])
def ui_response():
    """Relay an extension_ui_response from the browser to pi."""
    body = request.get_json(force=True)
    if not isinstance(body, dict) or body.get("type") != "extension_ui_response" or not body.get("id"):
        return jsonify({"success": False, "error": "invalid body"}), 400
    pi().send_command(body)
    return "", 204


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@app.route("/api/projects")
def list_projects():
    return jsonify(
        {
            "projects": [
                {**p, "current": p["id"] == current["project"]["id"]}
                for p in load_projects()
            ]
        }
    )


@app.route("/api/projects", methods=["POST"])
def create_project():
    """Register an existing directory or create a new one as a project.

    Body: {path, gitInit?}. If `path` exists it is registered as-is;
    otherwise it is created (with optional `git init`). The project name
    is the leaf directory name.
    """
    body = request.get_json(force=True)
    if not isinstance(body, dict) or not body.get("path"):
        return jsonify({"success": False, "error": "missing path"}), 400
    p = Path(body["path"]).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError:
        return jsonify({"success": False, "error": "invalid path"}), 400
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
    projects = load_projects()
    for existing in projects:
        if existing["path"] == str(p):
            return jsonify({"success": False, "error": "already registered"}), 409
    entry = {
        "id": uuid.uuid4().hex[:8],
        "name": p.name,
        "path": str(p),
        "created": datetime.now(timezone.utc).isoformat(),
    }
    projects.append(entry)
    save_projects(projects)
    return jsonify({"success": True, "project": entry})


@app.route("/api/projects/<pid>/open", methods=["POST"])
def open_project(pid):
    """Switch the active project: respawn pi with the project dir as cwd.

    Kills the running chat session (like an app restart). Other browser
    tabs learn about the switch via a `project_switched` event on the old
    process's stream and must reconnect.
    """
    entry = next((p for p in load_projects() if p["id"] == pid), None)
    if entry is None:
        return jsonify({"success": False, "error": "unknown project"}), 404
    if not Path(entry["path"]).is_dir():
        return jsonify({"success": False, "error": "project directory is gone"}), 400
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


# ---------------------------------------------------------------------------
# Project file browser / download
# ---------------------------------------------------------------------------

MAX_PREVIEW_BYTES = 512 * 1024


def safe_path(rel: str) -> Path:
    """Resolve rel under the current project root; reject escapes."""
    root = project_root()
    p = (root / rel.lstrip("/")).resolve()
    if p != root and root not in p.parents:
        http_abort(403)
    return p


@app.route("/api/list")
def api_list():
    root = project_root()
    d = safe_path(request.args.get("path", ""))
    if not d.is_dir():
        http_abort(404)
    entries = []
    try:
        for f in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            try:
                st = f.stat()
                entries.append(
                    {
                        "name": f.name,
                        "path": str(f.relative_to(root)),
                        "dir": f.is_dir(),
                        "size": None if f.is_dir() else st.st_size,
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
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
