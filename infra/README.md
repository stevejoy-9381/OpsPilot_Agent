# OpsPilot Infrastructure

Docker Compose definitions for the local, synthetic OpsPilot lab and application infrastructure.

## Services & Resource Limits

All services declare strict memory bounds (`mem_limit`) to ensure GitHub Codespaces and low-memory environments remain stable and responsive.

### Profile `core` (OpsPilot Application Plane)

| Service | Image / Build | Network(s) | Memory Limit | Purpose | Host bind |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `postgres` | `postgres:19.0-bookworm` | `core` | `512m` | OpsPilot application state and audit checkpoints | `127.0.0.1:5432` |
| `redis` | `redis:8.10.3-alpine` | `core` | `256m` | OpsPilot cache and ephemeral state | `127.0.0.1:6379` |

**Total Profile `core` Memory**: **768 MB**

---

### Profile `lab` (Synthetic Lab Microservices & Telemetry)

| Service | Image / Build | Network(s) | Memory Limit | Purpose | Host bind |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `lab-postgres` | `postgres:19.0-bookworm` | `lab` | `512m` | Dedicated lab database (products, inventory, orders) | None (Internal) |
| `lab-redis` | `redis:8.10.3-alpine` | `lab` | `256m` | Dedicated lab cache-aside store | None (Internal) |
| `inventory` | `infra/Dockerfile.lab` | `lab`, `lab-admin` | `384m` | Toy inventory stock validation service | None (Internal) |
| `orders` | `infra/Dockerfile.lab` | `lab`, `lab-admin` | `384m` | Toy order creation and persistence service | None (Internal) |
| `gateway` | `infra/Dockerfile.lab` | `lab`, `lab-admin` | `384m` | API gateway routing checkouts to orders | `127.0.0.1:8080` |
| `prometheus` | `prom/prometheus:v2.54.1` | `lab`, `lab-admin` | `256m` | Metrics scraper for lab services | None (Internal) |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.108.0` | `lab` | `256m` | Distributed trace receiver and pipeline | None (Internal) |
| `controller` | `infra/Dockerfile.lab` | `lab-admin` | `256m` | Scenario orchestration & ground truth vault | None (Internal) |

**Total Profile `lab` Memory**: **2,688 MB**

---

### Profile `extras` (Optional Visualizations & Generators — OFF by Default)

| Service | Image / Build | Network(s) | Memory Limit | Purpose | Host bind |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `grafana` | `grafana/grafana:11.2.0` | `lab` | `256m` | Optional visualization dashboard | `127.0.0.1:3000` |
| `load-generator` | `infra/Dockerfile.lab` | `lab` | `256m` | Open-loop synthetic traffic generator (`lab.loadgen`) | None (Internal) |

**Total Profile `extras` Memory**: **512 MB**

---

### Total Memory Footprint Summary

$$\text{Core} + \text{Lab} = 768\text{ MB} + 2,688\text{ MB} = 3,456\text{ MB } (\approx 3.375\text{ GB})$$

The baseline footprint for `core` + `lab` is strictly bounded under **3.5 GB**, meeting the **< 4 GB** requirement for low-memory hosts and GitHub Codespaces.

---

## Networks and Trust Boundaries

- **`core`**: Contains OpsPilot application services (`api`, `postgres`, `redis`).
- **`lab`**: Internal synthetic microservice network (`gateway`, `orders`, `inventory`, `lab-postgres`, `lab-redis`, `prometheus`, `otel-collector`). Isolated from `core` and external internet.
- **`lab-admin`**: Isolated administrative control plane (`gateway`, `orders`, `inventory`, and scenario controller). Used strictly for simulated in-process deployments and fault injections.
- **`broker`**: Isolated control-plane DB network connecting `broker` to `postgres`.

### Profile `broker` (Isolated Execution Broker)

| Service | Image / Build | Network(s) | Memory Limit | Security Posture | Host bind |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `broker` | `infra/Dockerfile.broker` | `broker`, `lab-admin` | `256m` | Non-root (`10001`), `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges` | None (`/healthz` internal `:8090` only) |

### Network Reachability Matrix (Source of Truth: `tests/test_broker_isolation.py`)

| Source \ Target | `broker:8090` | `lab /admin/*` | `controller` (`vault`) | `postgres` (core) | `otel-collector` (`evidence`) | `prometheus` | Outbound Internet |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`core` / `api`** | No | No | No | Yes (`opspilot_api`) | No | No | Host-dependent |
| **`worker` (LLM)** | No | No | No | Yes (`opspilot_worker`) | Yes | No | Yes (LLM API) |
| **`broker`** | Self (`/healthz`) | **Yes** (`X-Lab-Admin-Token`) | No (`HARNESS_TOKEN` absent) | Yes (`opspilot_broker`) | No | No write path | **No** (`internal: true`) |
| **`controller`** | No | Yes | Self | No | No | Yes (`:9090`) | **No** (`internal: true`) |
| **Lab services** | No | Self | No | No | Yes | Scraped (`:8000`) | **No** (`internal: true`) |

### Network Isolation Guarantees
1. **Unreachable from `core`**: Containers running on the `core` network have no network attachment, route, or DNS resolution to `lab` or `lab-admin`. Admin endpoints on all lab services are strictly unreachable from `core`.
2. **Zero Docker Socket Exposure**: No container in `docker-compose.yml` or `docker-compose.lab.yml` mounts `/var/run/docker.sock`. Deployments and rollbacks are executed in-process by swapping version profiles over `lab-admin` endpoints authenticated with `X-Lab-Admin-Token`. This is continuously enforced by `scripts/check_no_docker_socket.sh`.
3. **Optional Dashboards**: Grafana and auxiliary dashboards are optional everywhere. All automated tests interact directly with Prometheus and OpenTelemetry endpoints without depending on Grafana.

## Usage

```bash
# Codespaces Initial Setup
make codespace-setup  # Installs python virtual environment and web dependencies

# Core Application Stack
make up               # Start core postgres + redis (profile core), migrate, run API
make down             # Stop core stack

# Synthetic Lab Stack
make lab-up           # Start lab services & stores (profile lab), seed data, wait for gateway
make lab-lean         # Start lab services with existing images without rebuild
make lab-smoke        # Send 50 checkouts to verify lab health and trace propagation
make lab-down         # Stop lab stack
```
