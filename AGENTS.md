# AGENTS.md — pi agent web UI

Project conventions for the pi web app. See [README.md](README.md) for
architecture and feature overview; [web/README.md](web/README.md) for
frontend notes.

## Running and building

- Backend: `python app.py` (Flask, port 5000, serves `web/dist/`).
  Python deps: `pip install -r requirements.txt`.
- Frontend: `cd web && npm run build` — **required after every change
  under `web/src/`**: `web/dist/` is gitignored, so Flask only serves
  what was last built. `npm run dev` gives a Vite dev server with a
  proxy to the backend for live reload.
- There is no test suite yet; verification is `python3 -m py_compile
  app.py`, `ruff check app.py`, and `npm run build`, plus manual testing
  against a running instance.

## Terminology

- "Rendering" (in user requests) means the chat message rendering and
  the markdown rendering in the file viewer — both share the same
  marked + highlight.js pipeline and the `.md` styles in
  [web/src/app.css](web/src/app.css).

## Restart semantics (important)

- Changes to [app.py](app.py) require a Flask restart, which **kills the
  pi subprocess and the chat session it hosts**. Warn the user before
  restarting, or let them do it. Switching projects
  (`POST /api/projects/<id>/open`) also respawns the pi subprocess —
  same warning applies.
- pi caches slash commands (prompt templates, skills, extension
  commands) at subprocess startup; newly added templates only appear
  after a restart.
- If the pi subprocess dies unexpectedly, `pi()` in [app.py](app.py)
  respawns it on the next request and resumes the latest session. After
  a project switch, every browser tab must reconnect its SSE stream —
  the initiating tab does this explicitly, other tabs via the
  `project_switched` event.

## pi RPC facts learned the hard way

- `set_session_name` does **not** rewrite the session-file header; pi
  appends a `{"type":"session_info", ...}` entry to the JSONL. To
  resolve a session's display name, scan for the **last** `session_info`
  entry (empty name = explicitly cleared). The `/sessions` endpoint in
  [app.py](app.py) does this — don't "simplify" it back to reading the
  header.
- `/name`, `/new`, `/abort`, `/compact`, `/export` are intercepted by
  the frontend and mapped to RPC calls; other pi TUI commands don't
  exist in RPC mode and are blocked with a hint.

## Git

- Commit style: imperative subject line, no prefix conventions
  (see `git log`).
- `web/dist/`, `build/`, `.venv/`, `__pycache__/` are gitignored —
  never commit build artifacts.

## Global rules

The user-level conventions in `~/.pi/agent/AGENTS.md` (build artifacts
in `build/`, shared tooling, etc.) apply here too.
