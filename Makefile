.DEFAULT_GOAL := help

.PHONY: help up down logs build test lint format typecheck migrate

help:
	@echo "make up         Start all services and infrastructure"
	@echo "make down       Stop containers"
	@echo "make logs       Follow container logs"
	@echo "make build      Build service images"
	@echo "make test       Run portal API and migration tests"
	@echo "make lint       Run Ruff"
	@echo "make format     Format Python code with Ruff"
	@echo "make typecheck  Run MyPy for the portal API service"
	@echo "make migrate    Apply database migrations"

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

test:
	uv run --package portal-api pytest services/portal-api/tests
	uv run --package database-migrator pytest migrations/tests

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run --package portal-api mypy services/portal-api/app

migrate:
	docker compose run --rm migrator
