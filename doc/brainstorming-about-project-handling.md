# Brainstorming: handling real projects in the pi web app

Status: **first increment implemented** (see §7): project registry
(`projects.json` in the app dir, gitignored), `GET/POST /api/projects`,
`POST /api/projects/<id>/open`, a project switcher + new-project dialog
in the toolbar, and the file browser rooted at the current project.
Detach, per-project last-file restore, and workspace containment
(`PI_WEB_WORKSPACE`) landed afterwards; the git-clone flavor (§3.3)
followed, and detaching the *currently active* project is now allowed
(the frontend blocks prompts until another project is opened).
Decisions for the §6 open questions are recorded there.
Context: the app (see [README.md](../README.md)) is currently a chat UI
around a single `pi --mode rpc` process bound to one directory. It
should become a **tool for working on many projects**: create new
projects, pick one from a list, store project metadata, and bring data
in.

## 1. Core architecture: how many pi processes?

Right now Flask spawns one `pi --mode rpc` at startup, bound to its
cwd. Three models for multi-project support:

**A. Restart the pi subprocess on project switch (simplest)**

- Keep the single-process design. Switching projects = kill pi,
  respawn with the project directory as cwd, then resume the last
  session for that project.
- Pros: minimal change to the RPC bridge; pi's session storage is
  already per-cwd, so sessions naturally group by project.
- Cons: no parallel work in two projects; respawn cost per switch.

**B. One pi subprocess per project, kept alive (pool)**

- Flask manages a dict `project_id -> PiProcess`, each with its own
  reader thread and event routing; SSE events get tagged with the
  project id.
- Pros: true parallelism — an agent can grind on project A while you
  chat in project B; notifications when a background project finishes.
- Cons: more bookkeeping (correlation ids namespaced per process),
  memory, lifecycle rules (idle timeout? max concurrent?).

**C. Detached pi sessions, attach on demand**

- Projects are just directories + metadata; pi processes are ephemeral.

Current leaning: start with **A** (small delta from today), design the
project registry so **B** can slot in later. The registry and UI
questions below are independent of that choice.

## 2. Project registry: where to store the list

Options:

- **JSON file** next to the app (e.g. `~/.pi/web-projects.json` or
  `projects.json` in the app dir):
  `[{id, name, path, created, lastOpened, description}]`. Simple,
  editable by hand, trivially backed up.
- **Scan a workspace root** (e.g. `~/work/*`) and treat every directory
  (or every git repo) as a project. Zero bookkeeping, but no metadata
  and no control.
- **Hybrid (current favorite):** registry file for explicit projects +
  a "browse/scan" endpoint that suggests directories under a
  configurable root for quick adding.

pi already knows things per project: sessions live under
`~/.pi/agent/sessions/<encoded-cwd>/`. So `lastOpened`, session counts,
and last activity can be *derived* from the filesystem rather than
duplicated in the registry. The registry only needs what pi doesn't
know: display name, description/tags, pinned status.

## 3. Creating a new project through the UI

A "New Project" dialog with three flavors:

1. **Empty directory** — pick parent dir + name → `mkdir`, optionally
   `git init`.
2. **From existing directory** — browse the filesystem (a
   directory-picker variant of the existing file browser) → register.
3. **Clone from git URL** — `git clone <url>` into the workspace root,
   streaming clone output to the UI.

Each flavor could offer scaffolding checkboxes: create
[AGENTS.md](../AGENTS.md) (from a template — big for steering pi per
project), `.gitignore`, [README.md](../README.md).

Backend sketch: `POST /api/projects {name, path|parent, template?}` →
creates dir, registers, respawns/attaches pi there, responds with the
new project; the UI navigates to it.

## 4. Selecting / opening a project

UI: a project switcher in the header (dropdown or sidebar; a sidebar
would also hold per-project session lists). Opening a project means:

- Backend: switch cwd of the pi process (respawn under model A) →
  `get_sessions` → return project info + sessions.
- Frontend: load that project's sessions, auto-resume the most recent
  (or show a picker), refresh the file browser root, update the URL
  (`/p/<project-id>` makes projects bookmarkable — cheap with a tiny
  router).

Open design point: is "current project" server-global or per browser
tab? Single-user design says server-global is fine; with model B,
per-tab becomes interesting.

## 5. Bringing data in

- **Files into the project:** drag-and-drop upload into a chosen
  directory (extend the existing attachment upload to "upload into
  project tree"). Endpoint sketch:
  `POST /api/projects/<id>/files?path=...`.
- **Existing data elsewhere:** register a project over an existing
  directory — no copying, pi works in place.
- **Context for the agent:** per-project AGENTS.md is the natural
  mechanism; an edit mode in the file viewer covers it.
- **Cross-project reference:** pi can read outside its cwd, so project
  A referencing code in project B works without special support —
  document rather than build.

## 6. Open questions — decided for the first increment

1. Where do projects live? → **Arbitrary paths anywhere.** No fixed
   workspace root; registering an existing directory in place (§5) is
   too useful to give up. (Since superseded: a workspace root was added
   later — `PI_WEB_WORKSPACE`, default the parent of the app dir — and
   all project registration and opening is confined to it.)
2. Parallel or serial? → **Serial (model A).** All pi I/O goes through
   a `PiProcess` wrapper class in [app.py](../app.py), so a process pool
   (model B) can slot in later without reworking the endpoints.
3. How much metadata? → **Minimal:** `{id, name, path, created}` plus
   `lastOpened` (set on every switch, drives startup restore) and
   `lastFile` (the file open in the viewer, restored on project open).
   Skip tags/templates until they hurt.
4. Should the app itself remain a project? → **Yes.** The registry is
   seeded with the app's own directory as the default project on first
   run.
5. Sessions: per-project or global? → **Per-project** (pi's natural
   model; sessions already live per-cwd). A global cross-project view
   is a possible later extra.

## 7. First increment — DONE

Implemented as suggested: registry JSON + `GET/POST /api/projects` +
a project-switcher dropdown that respawns pi with the new cwd and
loads that project's sessions. The new-project popup is a directory
picker (`GET /api/dirs`, directories only, confined to the workspace
root — web users usually don't know absolute paths)
covering §3 flavors 1 (create a folder, optional `git init`) and 2
(select an existing directory); the git-clone flavor, uploads,
templates, process pool, and per-tab projects are deferred.

Additional details: on project switch the old process broadcasts a
`project_switched` event before being terminated, so other open
browser tabs can re-initialize and reconnect their SSE stream. Each
switch stamps `lastOpened` in the registry; on startup the app respawns
pi in the most recently opened project and resumes its latest session
(by session-file mtime), and the same resume happens on every switch —
covering the "auto-resume the most recent" option from §4. Projects
can also be detached again (registry only, files stay on disk) via a
manage-projects popup (`POST /api/projects/<id>/detach`); the currently
active project cannot be detached.

Note: switching projects restarts the pi subprocess — per
[AGENTS.md](../AGENTS.md), that kills the running chat session, so the UI
should warn before switching while a run is active.
