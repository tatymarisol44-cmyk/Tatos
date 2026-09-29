.PHONY: install lint test eval run mcp up down k8s-dev

install:        ## Install locked deps + git hooks
	git submodule update --init --recursive
	uv sync --frozen
	uv run pre-commit install

lint:           ## Format check, lint, types
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

test:           ## Unit + integration tests with coverage gate
	uv run pytest

eval:           ## Offline routing eval (same gate as CI)
	LLM_BACKEND=fake ROUTER_USE_LLM=false uv run agency eval --min-top1 0.50 --min-recall 0.80

run:            ## API with hot reload on :8000
	uv run uvicorn --factory orchestrator.api.app:app_factory --reload

mcp:            ## MCP server over stdio
	uv run agency mcp

up:             ## Full stack: API + Qdrant + OTel + Jaeger + Prometheus
	docker compose up --build -d

down:
	docker compose down

k8s-dev:        ## Render the dev overlay
	kubectl kustomize deploy/k8s/overlays/dev
