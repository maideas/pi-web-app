"""Flask bridge between a browser UI and `pi --mode rpc`.

Architecture:
    Browser <--SSE/POST--> Flask <--JSONL stdin/stdout--> pi --mode rpc

Single-user assumption: one pi subprocess, one event stream shared by all
connected browser tabs.
"""

import json
import queue
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from flask import abort as http_abort

PI_CMD = ["pi", "--mode", "rpc"]
DIST_DIR = "web/dist"

app = Flask(__name__, static_folder=DIST_DIR, static_url_path="")

# ---------------------------------------------------------------------------
# pi subprocess management
# ---------------------------------------------------------------------------

proc = subprocess.Popen(
    PI_CMD,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
    bufsize=1,
)

# Broadcast queue: every connected SSE client gets its own copy.
subscribers: list[queue.Queue] = []
subscribers_lock = threading.Lock()
stdin_lock = threading.Lock()

# Pending command responses, keyed by command id.
pending: dict[str, queue.Queue] = {}
pending_lock = threading.Lock()


def send_command(cmd: dict) -> None:
    with stdin_lock:
        proc.stdin.write(json.dumps(cmd) + "\n")
        proc.stdin.flush()


def rpc_request(cmd: dict, timeout: float = 30.0) -> dict:
    """Send a command and wait for its correlated response."""
    cmd_id = uuid.uuid4().hex
    cmd["id"] = cmd_id
    q: queue.Queue = queue.Queue()
    with pending_lock:
        pending[cmd_id] = q
    try:
        send_command(cmd)
        return q.get(timeout=timeout)
    except queue.Empty:
        return {"success": False, "error": "timeout", "type": "response", "id": cmd_id}
    finally:
        with pending_lock:
            pending.pop(cmd_id, None)


def reader_thread() -> None:
    """Read JSONL events from pi stdout. Split on \\n only (strict JSONL)."""
    import time

    while proc.poll() is None:
        try:
            for line in proc.stdout:
                line = line.rstrip("\n\r")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Route correlated command responses to waiting rpc_request calls.
                if event.get("type") == "response" and event.get("id"):
                    with pending_lock:
                        q = pending.get(event["id"])
                    if q is not None:
                        q.put(event)
                        continue  # don't broadcast; response is consumed by the waiter

                # Broadcast everything else to all SSE subscribers.
                with subscribers_lock:
                    for q in subscribers:
                        q.put(event)
        except Exception as exc:
            print("reader thread error:", exc)
        time.sleep(0.2)


threading.Thread(target=reader_thread, daemon=True).start()

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.route("/events")
def events():
    """Server-sent events stream of pi RPC events."""
    q: queue.Queue = queue.Queue()
    with subscribers_lock:
        subscribers.append(q)

    def gen():
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with subscribers_lock:
                if q in subscribers:
                    subscribers.remove(q)

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
    resp = rpc_request(cmd)
    return jsonify(resp)


@app.route("/abort", methods=["POST"])
def abort():
    return jsonify(rpc_request({"type": "abort"}))


@app.route("/new_session", methods=["POST"])
def new_session():
    return jsonify(rpc_request({"type": "new_session"}))


@app.route("/messages")
def messages():
    resp = rpc_request({"type": "get_messages"})
    return jsonify(resp)


@app.route("/state")
def state():
    return jsonify(rpc_request({"type": "get_state"}))


@app.route("/stats")
def stats():
    return jsonify(rpc_request({"type": "get_session_stats"}))


@app.route("/models")
def models():
    return jsonify(rpc_request({"type": "get_available_models"}))


@app.route("/set_model", methods=["POST"])
def set_model():
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "provider" not in body or "modelId" not in body:
        return jsonify({"success": False, "error": "missing provider or modelId"}), 400
    return jsonify(
        rpc_request(
            {"type": "set_model", "provider": body["provider"], "modelId": body["modelId"]}
        )
    )


@app.route("/thinking_levels")
def thinking_levels():
    return jsonify(rpc_request({"type": "get_available_thinking_levels"}))


@app.route("/set_thinking", methods=["POST"])
def set_thinking():
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "level" not in body:
        return jsonify({"success": False, "error": "missing level"}), 400
    return jsonify(rpc_request({"type": "set_thinking_level", "level": body["level"]}))


@app.route("/sessions")
def sessions():
    state_resp = rpc_request({"type": "get_state"})
    if not state_resp.get("success"):
        return jsonify({"sessions": []})
    sf = state_resp.get("data", {}).get("sessionFile")
    d = Path(sf).parent if sf else None
    if d is None or not d.is_dir():
        return jsonify({"sessions": []})
    current = sf
    try:
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]
    except OSError:
        return jsonify({"sessions": []})
    out = []
    for f in files:
        # Session display name (if any) lives in the header line.
        name = None
        try:
            with f.open() as fh:
                header = json.loads(fh.readline())
                name = header.get("name")
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
                "current": str(f) == current,
            }
        )
    return jsonify({"sessions": out})


@app.route("/switch_session", methods=["POST"])
def switch_session():
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "path" not in body:
        return jsonify({"success": False, "error": "missing path"}), 400
    return jsonify(rpc_request({"type": "switch_session", "sessionPath": body["path"]}))


@app.route("/commands")
def commands():
    """Slash commands invocable via prompt (extension commands, templates, skills)."""
    return jsonify(rpc_request({"type": "get_commands"}))


@app.route("/compact", methods=["POST"])
def compact():
    body = request.get_json(force=True, silent=True)
    cmd = {"type": "compact"}
    if isinstance(body, dict) and body.get("customInstructions"):
        cmd["customInstructions"] = body["customInstructions"]
    return jsonify(rpc_request(cmd))


@app.route("/set_session_name", methods=["POST"])
def set_session_name():
    body = request.get_json(force=True)
    if not isinstance(body, dict) or "name" not in body:
        return jsonify({"success": False, "error": "missing name"}), 400
    return jsonify(rpc_request({"type": "set_session_name", "name": body["name"]}))


@app.route("/export_html", methods=["POST"])
def export_html():
    return jsonify(rpc_request({"type": "export_html"}))


@app.route("/ui-response", methods=["POST"])
def ui_response():
    """Relay an extension_ui_response from the browser to pi."""
    body = request.get_json(force=True)
    if not isinstance(body, dict) or body.get("type") != "extension_ui_response" or not body.get("id"):
        return jsonify({"success": False, "error": "invalid body"}), 400
    send_command(body)
    return "", 204


# ---------------------------------------------------------------------------
# Project file browser / download
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
MAX_PREVIEW_BYTES = 512 * 1024


def safe_path(rel: str) -> Path:
    """Resolve rel under ROOT; reject anything escaping the project root."""
    p = (ROOT / rel.lstrip("/")).resolve()
    if p != ROOT and ROOT not in p.parents:
        http_abort(403)
    return p


@app.route("/api/list")
def api_list():
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
                        "path": str(f.relative_to(ROOT)),
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
            "path": str(d.relative_to(ROOT)) if d != ROOT else "",
            "parent": None if d == ROOT else str(d.parent.relative_to(ROOT))
            if d.parent != ROOT
            else "",
            "entries": entries,
        }
    )


@app.route("/api/file")
def api_file():
    p = safe_path(request.args.get("path", ""))
    if not p.is_file():
        http_abort(404)
    try:
        size = p.stat().st_size
        if size > MAX_PREVIEW_BYTES:
            return jsonify({"path": str(p.relative_to(ROOT)), "text": None, "reason": f"too large ({size} bytes)"})
        data = p.read_bytes()
    except OSError:
        http_abort(500)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"path": str(p.relative_to(ROOT)), "text": None, "reason": "binary file"})
    return jsonify({"path": str(p.relative_to(ROOT)), "text": text, "reason": None})


@app.route("/download/<path:rel>")
def download(rel):
    p = safe_path(rel)  # containment check
    if not p.is_file():
        http_abort(404)
    return send_from_directory(ROOT, p.relative_to(ROOT), as_attachment=True)


# ---------------------------------------------------------------------------
# Static frontend (production build of the Svelte app)
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return send_from_directory(DIST_DIR, "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
