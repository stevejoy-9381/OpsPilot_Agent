.PHONY: install lint typecheck test migrate run-api up down reset db-up db-migrate db-reset db-seed test-backbone web-install web-dev web-test web-lint lab-up lab-down lab-smoke lab-lean codespace-setup worker run-offline-demo test-agent test-hypothesis test-proposal test-approval test-durability demo-feature8

install:
	uv sync --extra dev

lint:
	uv run ruff check .

typecheck:
	uv run mypy apps agent lab tools baseline shared evals tests

test:
	uv run pytest -v

migrate:
	uv run alembic upgrade head

db-up:
	docker compose --env-file .env -f infra/docker-compose.yml --profile core up -d postgres
	@echo "Waiting for postgres to become healthy..."
	@until docker compose --env-file .env -f infra/docker-compose.yml exec -T postgres pg_isready -U opspilot -d opspilot > /dev/null 2>&1; do sleep 1; done
	@echo "Postgres is ready."

db-migrate:
	uv run alembic upgrade head

db-reset:
	uv run python -c "import os, psycopg; url = os.environ.get('DATABASE_URL', 'postgresql://opspilot:opspilot_dev_password@127.0.0.1:5432/opspilot').replace('+psycopg', ''); conn = psycopg.connect(url, autocommit=True); conn.execute('DROP SCHEMA IF EXISTS app CASCADE; DROP SCHEMA IF EXISTS checkpoints CASCADE; DROP TABLE IF EXISTS alembic_version;'); conn.close(); print('Database schema dropped.')"
	uv run alembic upgrade head
	@echo "Database reset and migrated to head."

db-seed:
	uv run python scripts/seed_dev.py

test-backbone:
	uv run pytest tests/backbone/test_lifecycle.py -v

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

worker:
	uv run python -m agent.worker

run-offline-demo:
	uv run python scripts/demo_offline_investigator.py

test-agent:
	uv run pytest tests/test_agent_context.py tests/test_graph_state_and_steps.py tests/test_plan_step.py tests/test_conclude_and_finalize.py tests/test_investigator_loop_deterministic.py tests/test_worker_and_graph.py -v -m "not live"

test-hypothesis:
	uv run pytest tests/test_hypothesis_ledger.py tests/test_step_schema_v2.py tests/test_gate_and_outcomes.py tests/test_investigator_workflow_v2.py tests/test_finalize_v2_and_api.py tests/test_v2_loop_deterministic.py -v -m "not live"

test-proposal:
	uv run pytest tests/test_shared_plans.py tests/test_target_registry_and_reader.py tests/test_policy_validator.py tests/test_propose_node.py tests/test_plan_persistence.py tests/test_plan_review.py tests/test_proposal_e2e_deterministic.py -v -m "not lab and not live"

test-approval:
	uv run pytest tests/test_approval_and_interrupt.py tests/test_approval_service_pg.py tests/approval_adversarial/ -v -m "not lab and not live and not durability"

test-durability:
	uv run pytest tests/durability/ -v -m durability

demo-feature8:
	uv run python scripts/demo_feature8.py

codespace-setup: install web-install

web-install:
	cd apps/web && npm install

web-dev:
	cd apps/web && npm run dev

web-test:
	cd apps/web && npm test

web-lint:
	cd apps/web && npm run lint
