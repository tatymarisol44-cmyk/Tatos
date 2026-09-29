.PHONY: install lint test eval eval-answers eval-judge run mcp up down k8s-dev

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

eval-answers:   ## Answer quality graded by the LLM judge (real models: needs API keys)
	uv run agency eval-answers --min-pass-rate 0.80 --min-mean 4.0 --output answers-report.json

eval-judge:     ## Judge agreement with human labels (real models: needs API keys)
	uv run agency eval-judge --min-agreement 0.85 --max-false-pass 1 --output judge-report.json

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
