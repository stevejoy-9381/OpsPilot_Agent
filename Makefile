.PHONY: install lint typecheck test migrate run-api up down reset web-install web-dev web-test web-lint lab-up lab-down lab-smoke lab-lean codespace-setup

install:
	uv sync --extra dev

lint:
	uv run ruff check .

typecheck:
	uv run mypy apps agent lab tools tests

test:
	uv run pytest -v

migrate:
	uv run alembic upgrade head

run-api:
	uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000

up:
	docker compose --env-file .env -f infra/docker-compose.yml --profile core up -d
	@echo "Waiting for postgres to become healthy..."
	@until docker compose --env-file .env -f infra/docker-compose.yml exec -T postgres pg_isready -U opspilot -d opspilot > /dev/null 2>&1; do sleep 1; done
	uv run alembic upgrade head
	uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000

down:
	docker compose --env-file .env -f infra/docker-compose.yml down

reset:
	@printf "WARNING: This will permanently wipe all Docker volumes (database data). Proceed? [y/N] " && read ans && [ "$$ans" = "y" -o "$$ans" = "Y" ] && docker compose --env-file .env -f infra/docker-compose.yml down -v && echo "Volumes wiped successfully." || echo "Reset cancelled."

lab-up:
	docker compose --env-file .env -f infra/docker-compose.lab.yml --profile lab up -d --build
	@echo "Waiting for lab-postgres to become healthy..."
	@until docker compose --env-file .env -f infra/docker-compose.lab.yml exec -T lab-postgres pg_isready -U lab_admin -d lab > /dev/null 2>&1; do sleep 1; done
	@echo "Seeding lab database..."
	@docker compose --env-file .env -f infra/docker-compose.lab.yml exec -T inventory uv run python lab/seed/seed.py --database-url "postgresql://lab_admin:lab-postgres-local-pw@lab-postgres:5432/lab" --seed-history
	@echo "Waiting for gateway..."
	@until curl -sf http://127.0.0.1:8080/healthz > /dev/null 2>&1; do sleep 1; done
	@echo "Lab stack is ready at http://127.0.0.1:8080."

lab-lean:
	docker compose --env-file .env -f infra/docker-compose.lab.yml --profile lab up -d
	@echo "Waiting for lab-postgres to become healthy..."
	@until docker compose --env-file .env -f infra/docker-compose.lab.yml exec -T lab-postgres pg_isready -U lab_admin -d lab > /dev/null 2>&1; do sleep 1; done
	@echo "Seeding lab database..."
	@docker compose --env-file .env -f infra/docker-compose.lab.yml exec -T inventory uv run python lab/seed/seed.py --database-url "postgresql://lab_admin:lab-postgres-local-pw@lab-postgres:5432/lab" --seed-history
	@echo "Waiting for gateway..."
	@until curl -sf http://127.0.0.1:8080/healthz > /dev/null 2>&1; do sleep 1; done
	@echo "Lean lab stack is ready at http://127.0.0.1:8080."

lab-down:
	docker compose --env-file .env -f infra/docker-compose.lab.yml down

lab-smoke:
	uv run python scripts/lab_smoke.py

codespace-setup: install web-install

web-install:
	cd apps/web && npm install

web-dev:
	cd apps/web && npm run dev

web-test:
	cd apps/web && npm test

web-lint:
	cd apps/web && npm run lint
