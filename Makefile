.PHONY: help install install-dev lint test run docker-build docker-up docker-down compose-obs clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## Install runtime deps
	pip install -r requirements.txt

install-dev: ## Install runtime + dev deps
	pip install -r requirements.txt -r requirements-dev.txt

lint: ## Run ruff
	ruff check .

test: ## Run tests
	pytest -q

run: ## Run bot locally (needs .env)
	python main.py

docker-build: ## Build container image
	docker compose build

docker-up: ## Start bot via Compose
	docker compose up -d

docker-down: ## Stop Compose stack
	docker compose down

compose-obs: ## Start bot + Prometheus
	docker compose --profile observability up -d

clean: ## Remove caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
