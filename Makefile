.PHONY: build run dev serve check audit

HOST_IP = $(word 1, $(shell hostname -I))
PORT = 5000

# Build the frontend into web/dist/ (required; Flask serves only dist/)
build:
	npm --prefix web run build

# Rebuild frontend and start the Flask server (kills any running pi session)
run: build
	.venv/bin/python app.py

serve: build
	.venv/bin/gunicorn -w 1 --threads 8 -b $(HOST_IP):$(PORT) app:app

# Vite dev server with live reload (run `make run`/app.py alongside it)
dev:
	npm --prefix web run dev

# Static verification: syntax, lint, frontend build
check:
	python3 -m py_compile app.py
	ruff check app.py
	npm --prefix web run build

# Dependency vulnerability audit (backend pins + frontend runtime deps).
# pip-audit needs network access; install it via pip if missing.
audit:
	python3 -m pip_audit -r requirements.txt
	npm --prefix web audit --omit=dev
