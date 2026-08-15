# web — browser UI for the pi agent bridge

Svelte 5 + Vite frontend served by [`app.py`](../app.py) (Flask). It connects
to the Flask backend via SSE (`/events`) and REST endpoints to chat with a
`pi --mode rpc` subprocess: send prompts (with image/text attachments),
switch models and thinking levels, browse and resume sessions, watch
token/cost/context stats, browse/preview/download/delete project files,
and inspect per-file git diffs as an in-file diff view with changed
lines highlighted, and edit text files in a syntax-highlighted edit
mode (transparent textarea over an hljs-rendered underlay) with
optimistic concurrency against agent-side edits.

## Develop

    npm install
    npm run dev      # Vite dev server with HMR (needs app.py on :5000)

## Build

    npm run build    # outputs to dist/, served by app.py at /
