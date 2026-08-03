# Brainstorming: handling real projects in the pi web app

Status: ideas only, nothing implemented yet.
Context: the app (see [README.md](README.md)) is currently a chat UI
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
[AGENTS.md](AGENTS.md) (from a template — big for steering pi per
project), `.gitignore`, [README.md](README.md).

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

## 6. Open questions

1. Where do projects live? One workspace root (`/mnt/space/work/*`?) or
   arbitrary paths anywhere?
2. Parallel or serial? Is "agent works on A while I review B" a real
   need soon, or is one-at-a-time fine for now?
3. How much metadata? Just name+path, or descriptions/tags/templates?
4. Should the app itself remain a project in the list (dogfooding, as
   today), just as the default?
5. Sessions: per-project sidebar (pi's natural model) or a global
   session view across projects?

## 7. Suggested first increment

Registry JSON + `GET/POST /api/projects` + a project-switcher dropdown
that respawns pi with the new cwd and resumes the latest session.
Everything else (uploads, templates, process pool, per-tab projects)
layers on top.

Note: switching projects restarts the pi subprocess — per
[AGENTS.md](AGENTS.md), that kills the running chat session, so the UI
should warn before switching while a run is active.
