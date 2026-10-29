# AGENTS.md — pi agent web UI

Project conventions for the pi web app. See [README.md](README.md) for
architecture and feature overview; [web/README.md](web/README.md) for
frontend notes.

## Running and building

- Backend: `python app.py` (Flask, port 5000 or `$PORT`, binds the
  primary LAN IP plus a loopback listener — `HOST=127.0.0.1` for
  loopback-only, `HOST=0.0.0.0` for all interfaces — serves `web/dist/`).
  Python deps: `pip install -r requirements.txt`; `make audit` additionally
  needs `requirements-dev.txt` (pip-audit) in the venv.
- `make serve` runs gunicorn with **one** worker (all bridge state and the
  pi subprocess are module globals) and a thread pool; each open browser
  tab pins one thread for its SSE stream and an exhausted pool stalls
  every request — keep `--threads` generous, never raise `-w`.
- Frontend: `cd web && npm run build` — **required after every change
  under `web/src/`**: `web/dist/` is gitignored, so Flask only serves
  what was last built. `npm run dev` gives a Vite dev server with a
  proxy to the backend for live reload.
- There is no test suite yet; verification is `make check` (py_compile,
  ruff, frontend build), plus manual testing against a running
  instance. `make audit` scans dependencies for known vulnerabilities
  (pip-audit + npm audit; needs network).

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
  respawns it on the next request and resumes the latest session. The
  reader thread broadcasts a `pi_exited` event to all SSE subscribers
  first, so every tab reconnects its stream (reconnecting is what
  triggers the respawn) and reloads its state; waiting RPC calls fail
  with `"pi process exited"` instead of timing out. After a project
  switch, every browser tab must reconnect its SSE stream —
  the initiating tab does this explicitly, other tabs via the
  `project_switched` event (the intentional stop suppresses
  `pi_exited`).

## Browser facts learned the hard way

- The HTML preview iframe is `sandbox`ed without `allow-same-origin`, so
  its document has an opaque origin and browsers send
  `Sec-Fetch-Site: cross-site` for *everything* it loads (verified in
  Chromium). The cross-site rejection in `request_origin_checks` therefore
  exempts passive `no-cors` subresource loads of `/raw/*` — don't tighten
  that without re-testing the HTML preview with relative assets.
- Chat markdown must never auto-load remote images (exfiltration channel
  for prompt-injected agent output): `renderMarkdown` only allows them
  with `{ remoteImages: true }`, which the file viewer passes. The whole
  rendered document goes through DOMPurify (`MD_SANITIZE`); keep the
  code-copy `<button>`, heading ids, `data-lang` and the alert `<svg>`
  allowed when changing that config.

## pi RPC facts learned the hard way

- `set_session_name` does **not** rewrite the session-file header; pi
  appends a `{"type":"session_info", ...}` entry to the JSONL. To
  resolve a session's display name, scan for the **last** `session_info`
  entry (empty name = explicitly cleared). The `/sessions` endpoint in
  [app.py](app.py) does this — don't "simplify" it back to reading the
  header.
- `/name`, `/rename`, `/new`, `/abort`, `/compact`, `/export` are intercepted by
  the frontend and mapped to RPC calls; other pi TUI commands don't
  exist in RPC mode and are blocked with a hint.
- `!cmd` / `!!cmd` run shell commands via the RPC `bash` command; `!!`
  passes `excludeFromContext: true` (documented only in pi's
  `rpc-mode.js`, not in `rpc.md`), so the output never enters the model
  context. The `bash` RPC response is a *correlated* response and never
  reaches SSE, so `POST /bash` is fire-and-forget: `PiProcess.run_bash`
  rebroadcasts the result as a synthetic `bash_execution_end` event for
  all tabs, correlated by the client-chosen command id (same id that
  pi's `bash_execution_update` chunks carry). `bashExecution` history
  entries emit no `message_start`/`message_end` events — reloads get
  them from `get_messages`.
- Extension commands (e.g. `/git-status`) run inside pi when sent as a
  prompt (response `disposition: "handled"`, no agent run). In RPC mode
  pi's stdout *is* the JSONL channel: anything an extension writes there
  raw is dropped by the reader (`dropping non-JSON line`), so command
  output must go through `ctx.ui.notify()`. The frontend renders notify
  requests as ephemeral chat entries (`role: 'notice'`, multi-line text
  as a collapsible block) — not part of the session, gone after reload.
  Fire-and-forget UI methods (`notify`, `setStatus`, `setWidget`,
  `setTitle`, `set_editor_text`) must not get an `extension_ui_response`;
  only dialogs (`select`, `confirm`, `input`, `editor`) are answered.

## Git

- Commit style: imperative subject line, no prefix conventions
  (see `git log`).
- `.env` holds machine-local settings (trusted hosts, bind address) and
  is gitignored; the tracked `.env.example` is the template documenting
  them — add new variables there as commented-out defaults (and mention
  them in the README).
- `web/dist/`, `build/`, `.venv/`, `__pycache__/` are gitignored —
  never commit build artifacts.

## Global rules

The user-level conventions in `~/.pi/agent/AGENTS.md` (build artifacts
in `build/`, shared tooling, etc.) apply here too.
