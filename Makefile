.PHONY: build run dev

# Build the frontend into web/dist/ (required; Flask serves only dist/)
build:
	npm --prefix web run build

# Rebuild frontend and start the Flask server (kills any running pi session)
run: build
	.venv/bin/python app.py

# Vite dev server with live reload (run `make run`/app.py alongside it)
dev:
	npm --prefix web run dev
