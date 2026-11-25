.PHONY: build run dev serve test check audit

PORT = 5000
# Exported so app.py and gunicorn.conf.py see the port they are bound to:
# the registry file name is derived from it (projects-<port>.json).
# Command-line overrides (make serve PORT=5001) are exported too — without
# this they only reach recipe expansions, not the child environment.
export PORT

# Build the frontend into web/dist/ (required; Flask serves only dist/)
build:
	npm --prefix web run build

# Rebuild frontend and start the Flask server (kills any running pi session)
run: build
	.venv/bin/python app.py

# Production-style server. Configured via gunicorn.conf.py: exactly ONE
# worker (the pi subprocess and all bridge state are module globals),
# 64 threads (every open tab pins one for SSE), honoring HOST/.env and
# binding loopback alongside the LAN IP just like `python app.py`.
serve: build
	.venv/bin/gunicorn -c gunicorn.conf.py app:app

# Vite dev server with live reload (run `make run`/app.py alongside it)
dev:
	npm --prefix web run dev

# Backend unit & security tests (mocked pi subprocess, hermetic workspace)
test:
	.venv/bin/python -m unittest discover -s tests -v

# Static verification: syntax, lint, tests, frontend build
check:
	python3 -m py_compile app.py gunicorn.conf.py tests/test_app.py
	ruff check app.py gunicorn.conf.py tests
	.venv/bin/python -m unittest discover -s tests
	npm --prefix web run build

# Dependency vulnerability audit (backend pins + all frontend deps).
# Needs network access and pip-audit in the venv:
#   .venv/bin/python -m pip install -r requirements-dev.txt
audit:
	.venv/bin/python -m pip_audit -r requirements.txt
	npm --prefix web audit
