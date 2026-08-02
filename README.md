# pi agent web UI

A browser-based chat interface for the [pi coding agent](https://github.com/earendil-works/pi).
A small Flask backend ([`app.py`](app.py)) bridges the browser to a
`pi --mode rpc` subprocess: the browser talks SSE/REST to Flask, and Flask
talks JSONL (newline-delimited JSON) to pi's stdin/stdout.

Features:

- Chat with markdown rendering, syntax highlighting, and collapsible
  thinking/tool blocks
- Image and text-file attachments (text files are inlined into the prompt)
- Model and thinking-level selectors, live token/cost/context stats
- Session management: list, switch, create, and name sessions
- Slash commands with autocomplete: `/new`, `/abort`, `/compact`, `/name`,
  `/export` are mapped to their RPC equivalents; extension commands, prompt
  templates, and skills (as reported by pi's `get_commands`) are forwarded;
  unrecognized TUI-only commands are blocked with a hint
- Project file browser with preview (syntax-highlighted) and download
- Light/dark theme (persisted in localStorage)

**Limitation (by design):** single-user. One pi subprocess and one shared
event stream serve all connected browser tabs.

## Architecture

```
Browser  <--SSE / REST-->  Flask (app.py)  <--JSONL stdin/stdout-->  pi --mode rpc
```

- The Flask app spawns `pi --mode rpc` at startup. A reader thread parses
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
| [`web/src/app.css`](web/src/app.css) | All styles; light/dark themes via CSS custom properties |
| [`web/src/main.js`](web/src/main.js) | Svelte entry point (mounts `App`) |
| [`web/index.html`](web/index.html) | Vite HTML entry |
| [`web/vite.config.js`](web/vite.config.js) | Vite config; dev-server proxy for all backend endpoints |
| [`web/jsconfig.json`](web/jsconfig.json) | JS type-checking config (checkJs) for the frontend |
| [`web/package.json`](web/package.json) | Frontend dependencies (svelte, vite, marked, highlight.js) |
| [`requirements.txt`](requirements.txt) | Pinned Python dependencies (Flask) |
| [`web/public/`](web/public/) | Static assets (favicon, icons) copied into `dist/` |
| [`AGENTS.md`](AGENTS.md) | Project conventions for coding agents |

Generated content (not committed): `web/dist/`, `web/node_modules/`,
`.venv/`, `build/`, `__pycache__/`.

## Backend API (Flask → pi RPC bridge)

Chat and agent control:

| Endpoint | Method | Purpose |
|---|---|---|
| `/events` | GET (SSE) | Stream of pi events (15 s keepalive comments) |
| `/prompt` | POST | Send a prompt; optional `images` and `streamingBehavior` |
| `/abort` | POST | Abort the current run |
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
| `/export_html` | POST | Export session to an HTML file |
| `/ui-response` | POST | Relay extension UI dialog responses to pi |

File browser and static hosting:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/list?path=` | GET | List a directory under the project root |
| `/api/file?path=` | GET | Preview a file (UTF-8, max 512 KB) |
| `/download/<path>` | GET | Download a file |
| `/` | GET | Serve the built SPA from `web/dist/` |

Paths for `/api/*` and `/download/*` are confined to the project root
(traversal attempts get 403). POST endpoints validate their JSON bodies and
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

# Run (serves the UI at http://127.0.0.1:5000)
python app.py
```

Frontend development with hot module reload:

```sh
cd web && npm run dev   # proxies API calls to app.py on 127.0.0.1:5000
```

There is no automated test suite; verification is `python3 -m py_compile
app.py`, `ruff check app.py`, and `npm run build`.
