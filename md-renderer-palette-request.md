# Palette request: pi-web-app light & dark palettes for md-to-html-renderer

> **Status: implemented** (md-to-html-renderer commit `449ce0e`, "add
> pi-web-app palette and token-driven heading scale"). The palette is
> live in this app via `/api/markdown_css`; its dark variant follows the
> app theme through the `data-theme` attribute on `<html>`. The venv
> housekeeping below was done as well. This file is kept as the record
> of the requested values.

**Goal.** Add a new palette (working name `pi-web-app`) to
[md-to-html-renderer](/mnt/space/work/md-to-html-renderer/README.md)
that makes rendered markdown look like the chat content of the
pi-web-app (this repository), in both its light and dark themes. The
palette will be used for the markdown preview in pi-web-app's file
viewer (currently served with the bundled `github` palette, see
[`app.py`](app.py), `/api/markdown_css`).

**Format.** Follow the existing palette structure: one JSON file per
palette under `static/palettes/` (see `skeleton.json` there for the
required tokens) with `base`, `light`, and `dark` sections, then run
`tools/make_palette.py` to generate the CSS. All values below are
extracted from [`web/src/app.css`](web/src/app.css) in this repository —
that file is the ground truth.

## Base tokens

Taken from the chat message styling (`.msg`, `.msg.assistant`):

| Token | Value | Source |
|---|---|---|
| `font-body` | `system-ui, sans-serif` | `:root` font-family |
| `font-mono` | `ui-monospace, monospace` | `.msg.assistant code` |
| `font-size` | `16px` (1rem) | inherited app font size |
| `font-size-code` | `90%` | `.msg.assistant code` (`0.9em`) |
| `font-size-small` | `13.6px` (0.85rem) | `.thinking-block` |
| `line-height` | `1.5` | `.msg` |
| `line-height-code` | `1.5` | match body |
| `radius` | `6px` | `.msg.assistant pre` |
| `radius-large` | `8px` | `.msg` |
| `border-accent-width` | `0.25em` | keep skeleton default |
| `space-block` | `0.4em` | `.msg.assistant p` margin |
| `space-heading-top` | `0.6em` | chat heading margin (`0.6em 0 0.3em`) |
| `page-max-width` | none / 100% | preview fills the viewer pane |
| `page-padding` | `0` | the app supplies its own padding |

## Heading scale (needs renderer support)

The chat uses a much flatter heading scale than the GitHub-style
`markdown.css` defaults (`h1: 2em` …). Desired sizes (from
`.msg.assistant h1–h6`), with `line-height: 1.3` and margins
`0.6em 0 0.3em`:

| Element | Chat size | GitHub default |
|---|---|---|
| h1 | `1.25rem` | 2em |
| h2 | `1.15rem` | 1.5em |
| h3 | `1.05rem` | 1.25em |
| h4 | `1rem` | 1em |
| h5 | `1rem` | 0.875em |
| h6 | `1rem` | 0.85em |

`markdown.css` currently hardcodes the em-based scale, so this either
needs new optional palette tokens (e.g. `--md-h1-size` … `--md-h6-size`,
falling back to the current defaults) or a per-palette override hook.
Whichever is cleaner — but without it the palette cannot match the
chat look.

## Light theme colors

From `:root[data-theme='light']`:

| Token | Value | App variable |
|---|---|---|
| `canvas-default` | `#FFFFFF` | `--panel` (assistant bubble) |
| `canvas-subtle` | `#F5F4F0` | `--panel-alt` |
| `canvas-inset` | `#F5F5F0` | `--code-bg` |
| `fg-default` | `#111111` | `--fg` |
| `fg-muted` | `#8A8580` | `--muted` |
| `fg-subtle` | `#B5B0AA` | `--muted2` |
| `fg-on-emphasis` | `#FFFFFF` | `--accent-fg` |
| `accent-fg` | `#0969da` | `--link` (adopted from the github palette) |
| `accent-emphasis` | `#64809E` | `--accent` |
| `accent-muted` | `#DCE5EF` | `--user-bg` |
| `border-default` | `#E5E2DD` | `--border` |
| `border-muted` | `#EFEDEA` | `--border-soft` |
| `neutral-muted` | `#EFEDEA` | `--border-soft` (closest match) |
| `success-fg` | `#1A8C4A` | `--ok` |
| `success-emphasis` | `#1A8C4A` | `--ok` |
| `danger-fg` | `#C41E3A` | `--err` |
| `danger-emphasis` | `#A31B1B` | `--danger` |
| `attention-fg` | `#6B5B2E` | `--sys-fg` |
| `attention-muted` | `#FFF8E7` | `--sys-bg` |
| `attention-emphasis` | `#E5C97B` | `--sys-border` |
| `done-fg`, `done-emphasis` | `#64809E` | no app equivalent; reuse `--accent` |
| `diff-add-bg` | `#DCEFE3` | suggested (no app equivalent) |
| `diff-del-bg` | `#F9DEDC` | suggested (no app equivalent) |

## Dark theme colors

From `:root` (default theme):

| Token | Value | App variable |
|---|---|---|
| `canvas-default` | `#26272B` | `--panel` (assistant bubble) |
| `canvas-subtle` | `#1F2124` | `--panel-alt` |
| `canvas-inset` | `#17181A` | `--code-bg` |
| `fg-default` | `#E4E4E7` | `--fg` |
| `fg-muted` | `#888888` | `--muted` |
| `fg-subtle` | `#777777` | `--muted2` |
| `fg-on-emphasis` | `#E4E4E7` | `--accent-fg` |
| `accent-fg` | `#4493F8` | `--link` (adopted from the github palette) |
| `accent-emphasis` | `#2B4A6F` | `--accent` |
| `accent-muted` | `#33465A` | `--user-bg` |
| `border-default` | `#333333` | `--border` |
| `border-muted` | `#2A2B2E` | `--border-soft` |
| `neutral-muted` | `#2A2B2E` | `--border-soft` (closest match) |
| `success-fg` | `#3FA659` | `--ok` |
| `success-emphasis` | `#3FA659` | `--ok` |
| `danger-fg` | `#E05252` | `--err` |
| `danger-emphasis` | `#7F2B2B` | `--danger` |
| `attention-fg` | `#F0C98A` | `--sys-fg` |
| `attention-muted` | `#3A2A1A` | `--sys-bg` |
| `attention-emphasis` | `#7F5A2B` | `--sys-border` |
| `done-fg`, `done-emphasis` | `#2B4A6F` | no app equivalent; reuse `--accent` |
| `diff-add-bg` | `#1E3A2A` | suggested (no app equivalent) |
| `diff-del-bg` | `#3A1E1E` | suggested (no app equivalent) |

## Syntax highlighting tokens (`syn-*`)

The chat highlights code with highlight.js using the **github** theme in
light mode and the **github-dark** theme in dark mode (see
[`web/src/App.svelte`](web/src/App.svelte), the `hljsDark`/`hljsLight`
imports). Use those two themes as the source for all `syn-*` token
values (keyword, string, comment, entity, constant, variable, markup
variants, …) so that fenced code in the markdown preview matches code
blocks in the chat exactly.

## Housekeeping: virtual environment for md-to-html-renderer

The `md-to-html-renderer` project was previously named `database` and
has no virtual environment of its own yet — the old project directory
`/mnt/space/work/database/` (including its `.venv`) is still around,
but `database` no longer exists as a project name. Please:

1. Create a fresh `.venv` inside
   `/mnt/space/work/md-to-html-renderer/` (`python3 -m venv .venv`),
   install the package editable with extras (`.venv/bin/python -m pip
   install -e '.[all]'`) plus its test dependencies, and run the test
   suite once to confirm the setup.
2. Do **not** reuse or move the old `/mnt/space/work/database/.venv` —
   moved venvs break console-script shebangs (this exact mistake
   previously made `pi-web-app/.venv/bin/pip` install into the wrong
   environment; it had to be recreated from scratch).
3. Once the new venv works, the leftover `/mnt/space/work/database/`
   directory can be removed (confirm with the user before deleting).

## Acceptance check

Render a markdown sample (headings h1–h6, paragraphs, inline code, a
fenced code block, a table, a blockquote, links, bold/italic, a task
list) once with `data-palette="pi-web-app"` in light and in dark, and
compare side by side with the same content as a chat message in
pi-web-app: fonts, sizes, heading scale, code backgrounds, and link
colors should match; syntax highlighting should match the chat's
hljs github/github-dark look.
