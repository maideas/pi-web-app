<p align="center">
  <img src="web/public/logo.svg" width="128" alt="pi web UI logo">
</p>

<p align="center">
  <img src="doc/pi-agent-web-ui-screenshot.png" width="720" alt="pi agent web UI screenshot">
</p>

# pi agent web UI

A browser-based chat interface for the [pi coding agent](https://github.com/earendil-works/pi).
A small Flask backend ([`app.py`](app.py)) bridges the browser to a
`pi --mode rpc` subprocess: the browser talks SSE/REST to Flask, and Flask
talks JSONL (newline-delimited JSON) to pi's stdin/stdout.

Features:

- Chat with markdown rendering, syntax highlighting, collapsible
  thinking/tool blocks, and copy buttons for messages and code blocks
- Shell-style prompt history recall (ArrowUp/ArrowDown, per project)
  and a message navigator for jumping to earlier user messages
- Steerable: the input stays usable while the agent works — send to
  queue a steering message, or abort
- Image and text-file attachments
- Model and thinking-level selectors, live token/cost/context stats
- Multiple projects and sessions: switch projects; create, register,
  or clone (git URL) project directories; restore the last
  session and open file; sessions live in a collapsible sidebar
  (collapse state remembered per project), are auto-named from chat
  content, and can be deleted from a per-session menu
- Slash commands with autocomplete, mapped to their RPC equivalents
- Shell commands: `!cmd` runs a command in the project directory with
  live-streamed output and adds the result to the model context with
  the next prompt; `!!cmd` runs it locally only (the model never sees
  it). Results persist in the session and render after reload
- Project file browser with markdown (incl. working in-page anchor
  links), HTML, and image preview, git status badges, an in-file
  git diff view (full file, changed lines highlighted), download, and delete
- Text edit mode for the file viewer: syntax-highlighted editor
  (highlighted underlay + transparent textarea) with Ctrl+S save, Esc
  quit, and optimistic concurrency against agent-side edits — a save
  that would clobber changes made on disk is rejected with a conflict,
  and the editor offers overwrite / reload / keep editing
- Light, cream, and dark themes

**Limitation (by design):** single-user. One pi subprocess and one shared
event stream serve all connected browser tabs.

**Security model:** the server listens on all interfaces (`0.0.0.0` by
default, so both loopback and host/LAN IPs are reachable) and has no
authentication — it assumes a trusted network. Anyone who can reach the
port (other local users, but also other machines on the LAN) can drive
the agent with full tool access and run arbitrary shell commands
directly (`/bash` endpoint). Set `HOST=127.0.0.1` to restrict the
server to loopback, and do not port-forward or reverse-proxy it without
adding authentication. Host-header validation (`TRUSTED_HOSTS`:
dynamically evaluated per request against loopback, localhost, machine
hostnames, mDNS `.local`, and all active interface/route IPs) protects
against DNS-rebinding attacks from remote web pages while remaining
resilient across DHCP address changes and multi-homed network setups.
Names the server cannot discover itself must be added explicitly via
`PI_WEB_TRUSTED_HOSTS` (comma-separated; a leading dot matches all
subdomains), otherwise such requests get a `400 Bad Request`. The
typical case is a systemd-nspawn container addressed by its machine name
via `nss-mymachines`: that name is only resolvable on the host, so the
container has no way to learn it. Such machine-local settings belong in
an unmanaged `.env` next to [`app.py`](app.py) — gitignored, loaded via
python-dotenv, real environment variables take precedence; it documents
the available variables in comments.

**Workspace containment:** all web endpoints that touch the filesystem
(project registration, directory picker, file browser, preview, diff,
delete, download) are confined to a workspace root — the parent
directory of the app by default, overridable with the
`PI_WEB_WORKSPACE` environment variable. Registry entries outside the
workspace (stale or hand-edited) are flagged in the UI, cannot be
opened, and can only be detached. This is an honest UI boundary, not a
sandbox for the agent: pi itself runs with full user privileges and its
bash tool can read and write anywhere the OS user can.

## Architecture

```
Browser  <--SSE / REST-->  Flask (app.py)  <--JSONL stdin/stdout-->  pi --mode rpc
```

- The Flask app spawns `pi --mode rpc` at startup (in the most recently
  opened project's directory). If the pi process dies unexpectedly, it is
  respawned automatically on the next request and resumes the latest
  session. A reader thread parses
  pi's stdout, routes correlated command responses to waiting HTTP handlers
  (request/response correlation via UUIDs), and broadcasts all other events
  to SSE subscribers.
- The frontend is a Svelte 5 + Vite single-page app in [`web/`](web/),
  built into `web/dist/`, which Flask serves at `/`. See
  [web/README.md](web/README.md) for frontend-specific notes.
- Styling is plain CSS with theme variables ([`web/src/app.css`](web/src/app.css));
  highlight.js themes are scoped per app theme so they don't clash.

## Project layout

| Path | Description |
|---|---|
| [`app.py`](app.py) | Flask backend: pi subprocess management, RPC bridging, REST/SSE API, static hosting |
| [`web/src/App.svelte`](web/src/App.svelte) | Entire SPA: chat, toolbar, sessions, slash commands, file browser/viewer |
| [`web/src/app.css`](web/src/app.css) | All styles; light/cream/dark themes via CSS custom properties |
| [`web/src/main.js`](web/src/main.js) | Svelte entry point (mounts `App`) |
| [`web/index.html`](web/index.html) | Vite HTML entry |
| [`web/vite.config.js`](web/vite.config.js) | Vite config; dev-server proxy for the backend endpoints |
| [`web/jsconfig.json`](web/jsconfig.json) | JS type-checking config (checkJs) for the frontend |
| [`web/package.json`](web/package.json) | Frontend dependencies (svelte, vite, marked, dompurify, highlight.js, github-slugger) |
| [`Makefile`](Makefile) | Build/run shortcuts (`build`, `run`, `dev`) |
| [`doc/brainstorming-about-project-handling.md`](doc/brainstorming-about-project-handling.md) | Design notes and decisions for multi-project support (registry/switch/clone/detach increments implemented; process pool still open) |
| [`doc/theme-preview.md`](doc/theme-preview.md) | Demo document for comparing the themes in the file viewer |
| [`LICENSE`](LICENSE) | License |
| [`requirements.txt`](requirements.txt) | Pinned Python dependencies (Flask, python-dotenv) |
| [`web/public/`](web/public/) | Static assets (favicon, logo, icons) copied into `dist/` |
| [`AGENTS.md`](AGENTS.md) | Project conventions for coding agents |

Generated content (not committed): `web/dist/`, `web/node_modules/`,
`.venv/`, `build/`, `__pycache__/`, `projects.json`.

## Backend API (Flask → pi RPC bridge)

Chat and agent control:

| Endpoint | Method | Purpose |
|---|---|---|
| `/events` | GET (SSE) | Stream of pi events (15 s keepalive comments) |
| `/prompt` | POST | Send a prompt; optional `images` and `streamingBehavior` |
| `/abort` | POST | Abort the current run |
| `/bash` | POST | Run a shell command in the project cwd; async — returns the command id, streamed output arrives as `bash_execution_update` SSE events, the result as a synthetic `bash_execution_end` event |
| `/abort_bash` | POST | Abort running `/bash` commands (pi cancels all of them) |
| `/new_session` | POST | Start a new session |
| `/messages` | GET | Current session message history |
| `/state` | GET | Agent state (model, thinking level, session file, ...) |
| `/stats` | GET | Session stats (tokens, cost, context usage, tool calls) |

Configuration:

| Endpoint | Method | Purpose |
|---|---|---|
| `/models` | GET | Available models |
| `/set_model` | POST | Switch model (`provider`, `modelId`) |
| `/thinking_levels` | GET | Available thinking levels |
| `/set_thinking` | POST | Set thinking level |

Sessions and slash commands:

| Endpoint | Method | Purpose |
|---|---|---|
| `/sessions` | GET | List recent session files (newest first, max 50) |
| `/switch_session` | POST | Switch to a session file |
| `/commands` | GET | Slash commands invocable via prompt (pi `get_commands`) |
| `/compact` | POST | Compact context; optional `customInstructions` |
| `/set_session_name` | POST | Set session display name |
| `/delete_sessions` | POST | Delete session files from disk (paths confined to the session dir; deleting the active session is allowed) |
| `/api/auto_name` | POST | Name the session from its content via a one-shot pi call (no-op if already named or too little content; `force: true` regenerates) |
| `/export_html` | POST | Export session to an HTML file |
| `/ui-response` | POST | Relay extension UI dialog responses to pi |

Projects:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/projects` | GET | List registered projects (marks the current one; flags entries outside the workspace) |
| `/api/dirs?path=` | GET | Directory picker for the new-project dialog: directories only, confined to the workspace root |
| `/api/projects` | POST | Register an existing directory or create a new one (`path`, optional `gitInit`), or clone a repo into the workspace (`gitUrl`, optional `folder`; the folder defaults to the repo name from the URL); the project name is the leaf directory name; paths must lie inside the workspace (403 otherwise) |
| `/api/projects/<id>/open` | POST | Switch the active project: respawn pi with the project dir as cwd (403 if the project is outside the workspace) |
| `/api/projects/<id>/detach` | POST | Remove a project from the registry (directory stays on disk; refuses the current project) |
| `/api/projects/<id>/last-file` | POST | Remember the project's open viewer file (`lastFile`) |

File browser and static hosting:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/list?path=` | GET | List a directory under the project root |
| `/api/file?path=` | GET | Preview a file (UTF-8, max 512 KB) |
| `/api/diff?path=` | GET | Full-context git diff (`-U…`) for one file (worktree vs HEAD; untracked vs /dev/null), rendered by the frontend as an in-file diff with changed lines highlighted |
| `/api/file/delete` | POST | Delete a file under the project root |
| `/api/file/save` | POST | Save file content from edit mode (atomic write, permission-preserving; rejects with `conflict: true` when the disk content no longer matches the client's `base`, unless `force`) |
| `/raw/<path>` | GET | Serve a file inline (image/HTML preview; nosniff + CSP sandbox) |
| `/download/<path>` | GET | Download a file |
| `/` | GET | Serve the built SPA from `web/dist/` |

Paths for `/api/list`, `/api/file`, `/api/diff`, `/api/file/delete`,
`/api/file/save`, `/raw/*`, and `/download/*` are confined to
the current project's root, which itself must lie inside the workspace
root (traversal attempts get 403; symlinks are resolved before the
containment check). POST endpoints require an
`X-Requested-With: XMLHttpRequest` header (CSRF protection, 403 otherwise),
validate their JSON bodies and
return 400 on missing fields; RPC calls time out after 30 s with a
`{"success": false, "error": "timeout"}` response.

## Usage

Prerequisites: Python 3, Node.js/npm, and `pi` on `PATH`.

```sh
# Backend dependencies (once)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Frontend build
cd web && npm install && npm run build && cd ..

# Run (serves the UI on port 5000 on all interfaces, i.e. loopback and
# the LAN; HOST=127.0.0.1 restricts to loopback, PORT=... changes the port)
# Optional: PI_WEB_WORKSPACE=/path/to/workspace confines projects and
# the file browser to that directory (default: parent of the app dir).
# Optional: PI_WEB_TRUSTED_HOSTS=name1,name2 accepts extra Host headers
# (e.g. a container name resolved by nss-mymachines on the host).
# All of these can also live in a local, gitignored .env file.
python app.py
```

Or use the [Makefile](Makefile): `make build`, `make run` (rebuilds the
frontend and starts the server in one step), `make dev` (Vite dev server).

Frontend development with hot module reload:

```sh
cd web && npm run dev   # proxies API calls to app.py on 127.0.0.1:5000
```
(Keep `python app.py` running alongside the dev server.)

Two things to know:

- `web/dist/` is gitignored, so Flask only serves what was last built.
  Re-run `npm run build` in [`web/`](web/) after every change under
  `web/src/` (or use `npm run dev` for live reload).
- Restarting [`app.py`](app.py) kills the pi subprocess and the chat
  session it hosts. Switching projects does the same by design (the UI
  warns first if a run is active); other open tabs get a
  `project_switched` event and reconnect. pi also caches slash commands
  (prompt templates, skills, extension commands) at subprocess startup,
  so newly added templates only appear after a restart or project
  switch.

There is no automated test suite; verification is `python3 -m py_compile
app.py`, `ruff check app.py`, and `npm run build`.
