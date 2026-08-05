# Code and Security Review — Post-Fix Verification

A follow-up review of the **pi-web-app** repository (Flask backend in
[`app.py`](app.py), Svelte 5 frontend in
[`web/src/App.svelte`](web/src/App.svelte)), verifying the remediations
from the earlier report
([code-and-security-review.md](code-and-security-review.md)) and
re-auditing the current code for remaining issues.

Verification methods: full source read, `py_compile`, `ruff check`,
`npm run build`, Flask test-client behavioral tests (CSRF guard, path
traversal, malformed payloads, registry round-trip), and node-level
rendering tests of the marked pipeline.

---

## Table of Contents

- [1. Executive Summary](#1-executive-summary)
- [2. Status of Previous Findings](#2-status-of-previous-findings)
- [3. New Findings in This Review](#3-new-findings-in-this-review)
- [4. Verified-Robust Areas](#4-verified-robust-areas)
- [5. Remaining Open Items (Accepted Risk)](#5-remaining-open-items-accepted-risk)
- [6. Action Summary](#6-action-summary)

---

## 1. Executive Summary

All findings from the previous review are **fixed and verified**,
including two gaps in the original remediations that this review closed:

- The XSS scheme filter originally covered only the file viewer's click
  handler; chat messages rendered `javascript:` links live. Now blocked
  at render time in the shared marked pipeline (covers both surfaces).
- Three endpoints still used `request.get_json(force=True, ...)`;
  removed.

One **new robustness bug** was found and fixed in this review: a
malformed `images` attachment in `POST /prompt` caused an unhandled
`KeyError` → HTTP 500. The endpoint now validates the attachment shape
and returns 400.

No critical or high-severity issues remain. The residual items in
[section 5](#5-remaining-open-items-accepted-risk) are low-severity and
consistent with the documented single-user, loopback-only threat model
(see [README.md](README.md) and [AGENTS.md](AGENTS.md)).

> **Note:** the `images` validation fix touches [`app.py`](app.py) and
> only takes effect after a Flask restart (which kills the pi chat
> session — see [AGENTS.md](AGENTS.md), "Restart semantics").

---

## 2. Status of Previous Findings

| Previous finding | Status | Verification |
| :--- | :--- | :--- |
| 🚨 CSRF via `force=True` / missing header check | ✅ **Fixed** | `csrf_protection` before-request hook rejects POST/PUT/DELETE/PATCH without `X-Requested-With: XMLHttpRequest` (403, tested). All `force=True` removed — including the three stragglers (`/compact`, `/api/auto_name`, `/api/projects/<pid>/last-file`) missed by the first fix pass. `text/plain` "simple request" vector: blocked (tested). Frontend `apiPost` sends the header; GETs and the SSE stream are unaffected. |
| ⚠️ XSS via `javascript:` URIs in markdown | ✅ **Fixed (extended)** | The original fix in `onMdClick` only guarded the file viewer; chat messages render `{@html ...}` with **no** click handler, so injected `javascript:` links still executed natively. Now a `link` renderer in the shared marked pipeline drops unsafe-scheme links at render time (keeps the link text); `https:`, `mailto:`, protocol-relative, and relative links render normally (tested against marked 18). `onMdClick` retains its scheme check as defense in depth. Raw inline/block HTML is escaped by the `html` renderer (tested: `<script>`, `<img onerror>` neutralized). |
| 🐛 Non-atomic `projects.json` writes | ✅ **Fixed** | `save_projects` writes to `projects.tmp` on the same filesystem, then `Path.replace()` (atomic rename). Round-trip tested; tmp file cleaned up; `projects.tmp` added to `.gitignore`. |
| 🐛 Chat message misordering after tool calls | ✅ **Fixed** | `currentAssistant()` now only returns the *last* entry if it is an assistant message; stray post-tool deltas open a fresh entry after the tool block instead of corrupting the pre-tool message. All call sites (`message_update`, `message_end`, streaming spinner) remain consistent. |
| 🐛 Orphaned subprocesses on termination failure | ✅ **Fixed (extended)** | `PiProcess.stop()`: `terminate()` → `wait(2 s)` → `kill()` fallback. This review added a final `wait(1 s)` after `kill()` so the force-killed child is reaped instead of lingering as a zombie (the daemon reader thread never reaps). |
| 🐛 Unbounded SSE subscriber queues | ✅ **Fixed** | `subscribe()` uses `Queue(maxsize=1000)`; `broadcast()` uses `put_nowait` and drops events for stalled clients. `rpc_request` response queues stay unbounded by design (one response each). |
| ⚡ DOM allocation in `escapeHtml` | ✅ **Fixed** | Single-pass regex replacement, all five characters (`&<>"'`) mapped correctly; used in the `code`, `html`, and `data-lang` attribute paths. |

---

## 3. New Findings in This Review

### 🐛 [MEDIUM] Unhandled 500 on Malformed Image Attachments — fixed

- **Location:** [`app.py`](app.py), `prompt()`
- **CWE:** CWE-20 (Improper Input Validation)

`POST /prompt` with `images: [{"foo": 1}]` raised `KeyError: 'data'`
inside the list comprehension → HTTP 500 with a stack trace in the log.
Not a security hole (the CSRF guard runs first, and Flask's debugger is
off), but an availability/robustness defect on a primary endpoint.

**Fix (applied):** validate that `images` is a list of dicts with
string `data` and `mimeType` before building the RPC command; malformed
payloads now return 400 (tested: `[{"foo":1}]` → 400,
`mimeType: 5` → 400, non-list → 400).

### ℹ️ Minor observations (not fixed, low value)

- `broadcast()` dropping events for a full queue silently desyncs that
  one stalled tab until it reconnects; a `queue full → close stream`
  signal would force a clean reconnect. Acceptable as-is.
- The reader thread logs via `print()`; Flask's `app.logger` would give
  timestamps. Cosmetic.
- The five remaining `ruff` findings (broad `except Exception` in the
  reader thread and session-name scanning) predate this review and are
  deliberate resilience catches around untrusted JSONL input.

---

## 4. Verified-Robust Areas

Checks performed in this review that found **no** issues:

- **Path traversal:** `safe_path()` (`/api/list`, `/api/file`,
  `/download/<path>`) and the inline containment check in `/api/dirs`
  resolve symlinks and enforce ancestry against the project root
  (resp. its parent for `/api/dirs`). Probes with `../../etc/passwd`,
  URL-encoded `..%2f`, and absolute-style paths all return 403
  (tested). Symlinks pointing outside the root resolve outside and are
  rejected by the same check.
- **RPC bridge concurrency:** `stdin_lock` serializes writes;
  `pending_lock` guards the correlation table with `finally` cleanup;
  `rpc_request` degrades to structured error responses on timeout (30 s)
  and broken pipe. The `pi()` respawn helper double-checks liveness
  under `switch_lock` (no duplicate respawn race).
- **Project switching:** `open_project`, `set_last_file`, and
  `detach_project` all serialize on `switch_lock`; the active project
  cannot be detached; `lastOpened` stamping and process swap happen
  atomically within the lock; the old process is stopped only after the
  new one is live and the `project_switched` event was broadcast.
- **Command execution:** all `subprocess` invocations (`pi --mode rpc`,
  `pi --print` for auto-naming, `git init`) use argv lists — no shell,
  no injection surface. The auto-naming prompt embeds user text only as
  a single argv element.
- **Rendering pipeline:** raw HTML tokens escaped; code blocks
  highlighted or escaped; `data-lang` attribute escaped; alert
  blockquote bodies re-parsed through the same hardened pipeline;
  session names, file names, tool output, and user messages rendered as
  Svelte text (auto-escaped), not `{@html}`.
- **SSE lifecycle:** keepalive comments every 15 s; `unsubscribe` in a
  `finally`; the frontend reconnects explicitly on project switches and
  via the `project_switched` event in other tabs.
- **Auto-naming:** serialized by `auto_name_lock` (non-blocking
  acquire), session-change re-check before applying the title,
  bounded digest and 60 s subprocess timeout.
- **Server exposure:** binds `127.0.0.1` only; Flask debug mode off.

Build/lint verification: `py_compile` ✅, `ruff` (5 pre-existing
findings, none new) ✅, `npm run build` ✅ (frontend rebuilt so
[`web/dist/`](web/README.md) matches the sources).

---

## 5. Remaining Open Items (Accepted Risk)

| Severity | Item | Notes |
| :--- | :--- | :--- |
| Low | **DNS rebinding** bypasses the header-based CSRF defense: a rebound hostname shares the origin and *can* set `X-Requested-With`. | Mitigation would be a `Host` allow-list (`127.0.0.1:5000`, `localhost:5000`) in `csrf_protection` — cheap to add if desired. Requires the victim to keep an attacker page open while it rebinds; low practical risk on a developer box. |
| Low | **No authentication** — anything running on the same host can drive the agent. | Inherent to the single-user, loopback-bound design documented in [README.md](README.md). Do not bind to non-loopback interfaces without adding auth. |
| Low | **Unrestricted project registration** — `/api/projects` accepts any local path (e.g. `$HOME`). | Design decision for a local tool; becomes a real issue only combined with network exposure (see above). |
| Info | **Werkzeug dev server** (`app.run`) is not a production server. | Fine for the intended local single-user use. |

---

## 6. Action Summary

| Priority | Item | Status |
| :--- | :--- | :--- |
| P0–P2 | All findings from [code-and-security-review.md](code-and-security-review.md) | ✅ Fixed & verified (two remediation gaps closed in the process) |
| P2 | 500 on malformed `/prompt` image attachments | ✅ Fixed in this review (400 with error message) — needs Flask restart |
| P3 | `Host` header check against DNS rebinding | Open — recommended, one-line addition to `csrf_protection` |
| P3 | Signal/close stalled SSE subscribers instead of silent drops | Open — optional polish |
