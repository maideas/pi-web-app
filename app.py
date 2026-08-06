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
import os
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
# Auto-naming threshold: combined length of the session's user messages.
MIN_USER_CHARS = 120
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
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
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
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(projects, indent=2) + "\n")
    tmp.replace(REGISTRY_PATH)


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
    if not isinstance(body, dict) or "message" not in body:
        return jsonify({"success": False, "error": "missing message"}), 400
    cmd = {"type": "prompt", "message": body["message"]}
    if body.get("streamingBehavior"):
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


@app.route("/delete_sessions", methods=["POST"])
def delete_sessions():
    """Delete session files from disk (never the currently active one).

    Paths are validated against the current session directory so the
    endpoint can't be abused to delete arbitrary files.
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
        if str(resolved) == sf:
            errors.append({"path": p, "error": "cannot delete the current session"})
            continue
        try:
            resolved.unlink()
            deleted.append(p)
        except FileNotFoundError:
            deleted.append(p)  # already gone — fine
        except OSError as e:
            errors.append({"path": p, "error": str(e)})
    return jsonify({"success": not errors, "deleted": deleted, "errors": errors})


@app.route("/switch_session", methods=["POST"])
def switch_session():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "path" not in body:
        return jsonify({"success": False, "error": "missing path"}), 400
    return jsonify(pi().rpc_request({"type": "switch_session", "sessionPath": body["path"]}))


@app.route("/commands")
def commands():
    """Slash commands invocable via prompt (extension commands, templates, skills)."""
    return jsonify(pi().rpc_request({"type": "get_commands"}))


@app.route("/compact", methods=["POST"])
def compact():
    body = request.get_json(silent=True)
    cmd = {"type": "compact"}
    if isinstance(body, dict) and body.get("customInstructions"):
        cmd["customInstructions"] = body["customInstructions"]
    return jsonify(pi().rpc_request(cmd))


@app.route("/set_session_name", methods=["POST"])
def set_session_name():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "name" not in body:
        return jsonify({"success": False, "error": "missing name"}), 400
    return jsonify(pi().rpc_request({"type": "set_session_name", "name": body["name"]}))


@app.route("/export_html", methods=["POST"])
def export_html():
    return jsonify(pi().rpc_request({"type": "export_html"}))


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
    to the parent directory of the current project root.
    """
    base = project_root().parent
    d = (base / request.args.get("path", "").lstrip("/")).resolve()
    if d != base and base not in d.parents:
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
    body = request.get_json(silent=True)
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
    process's stream and must reconnect. Stamps `lastOpened` in the
    registry (restored on next app start) and resumes the project's
    latest session.
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

    The currently active project cannot be detached — switch to another
    project first.
    """
    with switch_lock:
        if current["project"]["id"] == pid:
            return jsonify({"success": False, "error": "cannot detach the current project"}), 400
        projects = load_projects()
        entry = next((p for p in projects if p["id"] == pid), None)
        if entry is None:
            return jsonify({"success": False, "error": "unknown project"}), 404
        projects.remove(entry)
        save_projects(projects)
    return jsonify({"success": True})


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
# ranks lowest: a directory only shows as ignored if nothing under it
# has a stronger status.
_GIT_RANK = {"modified": 3, "added": 2, "untracked": 1, "ignored": 0}


def git_dir_status(statuses: dict[str, str], rel: str) -> str | None:
    """Strongest status of any path under directory `rel`, or None."""
    prefix = rel + "/"
    best = None
    for path, status in statuses.items():
        under = (
            path.rstrip("/") == rel
            or path.startswith(prefix)
            or (path.endswith("/") and prefix.startswith(path))
        )
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
    """Unified git diff for one file (worktree vs HEAD).

    Untracked files are diffed against /dev/null so new content shows as
    additions. Returns {path, diff, error}; diff == "" means no changes.
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
            out = run("diff", "HEAD", "--", rel)
            if out.returncode not in (0, 1):  # e.g. unborn HEAD (no commits)
                out = run("diff", "--", rel)
        else:
            # --no-index exits 1 when the files differ; that's the normal case.
            out = run("diff", "--no-index", "--", os.devnull, rel)
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


@app.route("/raw/<path:rel>")
def raw(rel):
    """Serve a project file inline (image preview in the file viewer).

    Unlike /download this sends the real Content-Type so <img> tags can
    render it. `CSP: sandbox` keeps scripts in directly-opened SVGs from
    running in the app's origin; as an <img> source they are inert anyway.
    """
    p = safe_path(rel)  # containment check
    if not p.is_file():
        http_abort(404)
    resp = send_from_directory(project_root(), p.relative_to(project_root()))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
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
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True)
