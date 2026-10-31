.DEFAULT_GOAL := help
.PHONY: help dev-prepare sync-cache

help:
	@echo 'make dev-prepare  Pull, prepare 600-frame data, build UI, and start server'
	@echo 'make sync-cache   Fetch upstream cache and merge records (remote wins)'

# Run Git/data steps sequentially, including when make is invoked with -j.
dev-prepare:
	uv run scripts/dev_prepare.py prepare
	npm --prefix ui ci --no-audit --no-fund
	npm --prefix ui run build
	uv run ui/server.py

sync-cache:
	uv run scripts/dev_prepare.py sync-cache
