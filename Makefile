.PHONY: build run dev serve check audit

HOST_IP = $(word 1, $(shell hostname -I))
PORT = 5000

# Build the frontend into web/dist/ (required; Flask serves only dist/)
build:
	npm --prefix web run build

# Rebuild frontend and start the Flask server (kills any running pi session)
run: build
	.venv/bin/python app.py

# Production-style server. Exactly ONE worker (the pi subprocess and all
# bridge state are module globals). Every open browser tab pins one
# gthread thread for its SSE stream, and once the pool is exhausted ALL
# requests stall — so keep the thread count well above the number of
# tabs you'll ever have open (threads are cheap here).
serve: build
	.venv/bin/gunicorn -w 1 --threads 64 -b $(HOST_IP):$(PORT) app:app

# Vite dev server with live reload (run `make run`/app.py alongside it)
dev:
	npm --prefix web run dev

# Static verification: syntax, lint, frontend build
check:
	python3 -m py_compile app.py
	ruff check app.py
	npm --prefix web run build

# Dependency vulnerability audit (backend pins + frontend runtime deps).
# Needs network access and pip-audit in the venv:
#   .venv/bin/python -m pip install -r requirements-dev.txt
audit:
	.venv/bin/python -m pip_audit -r requirements.txt
	npm --prefix web audit --omit=dev
