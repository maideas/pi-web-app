# web — browser UI for the pi agent bridge

Svelte 5 + Vite frontend served by [`app.py`](../app.py) (Flask). It connects
to the Flask backend via SSE (`/events`) and REST endpoints to chat with a
`pi --mode rpc` subprocess: send prompts (with image/text attachments),
switch models and thinking levels, browse and resume sessions, watch
token/cost/context stats, browse/preview/download project files, and
view per-file git diffs.

## Develop

    npm install
    npm run dev      # Vite dev server with HMR (needs app.py on :5000)

## Build

    npm run build    # outputs to dist/, served by app.py at /
