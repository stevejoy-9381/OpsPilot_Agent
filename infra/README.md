# OpsPilot infrastructure

Docker Compose definitions for the local, synthetic OpsPilot lab. Only PostgreSQL
and Redis exist today; the API, agent, broker, and lab services arrive in later
phases.

## Services

| Service  | Image                   | Purpose                          | Host bind         |
| -------- | ----------------------- | -------------------------------- | ----------------- |
| postgres | `postgres:19.0-bookworm` | Application state and approvals  | `127.0.0.1:5432`  |
| redis    | `redis:8.10.3-alpine`    | Short-lived run/approval state   | `127.0.0.1:6379`  |

Both ports bind to loopback only. PostgreSQL data lives in the named volume
`opspilot_postgres_data` mounted at `/var/lib/postgresql`, which is the volume
root used by PostgreSQL 18+ images.

Version pins were checked against the official release sources on 2026-11-22:
PostgreSQL 19.0 (GA 2026-11-19) and Redis 8.10.3 (2026-10-26).

## Networks and trust boundaries

- `core` — API, PostgreSQL, Redis. Normal application traffic.
- `lab` — toy services and the fault controller (Feature 1). Internal network.
- `broker` — broker only. Internal network.

All three are declared in `docker-compose.yml`. Compose only materialises a
network once a service joins it, so `lab` and `broker` stay as documented intent
until their services exist; that is expected at this stage.

The broker is the only component allowed to touch lab control. The LLM worker
will never join the `broker` network, and neither the worker nor the API will
ever receive the Docker socket or deployment credentials. The worker talks to
the API; the API records an exact-plan approval; the broker validates and
executes that plan. Keeping the broker on its own network means a compromised
or prompt-injected worker has no route to the control plane even if it escapes
its own process.

No Compose file mounts `/var/run/docker.sock`. That is deliberate, and
`scripts/check_no_docker_socket.sh` enforces it. The only future exception is
the allowlisted `broker` service.

## Usage

From the repository root:

```bash
cp .env.example .env   # first time only; .env is git-ignored
docker compose --env-file .env -f infra/docker-compose.yml config
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
```

The root `.env` is passed explicitly with `--env-file` because Compose otherwise
looks for `.env` next to the Compose file (in `infra/`). The Compose file also
contains local defaults matching `.env.example`, so a plain
`docker compose -f infra/docker-compose.yml config` still validates. Use
`--env-file .env` whenever you change credentials or other values.
