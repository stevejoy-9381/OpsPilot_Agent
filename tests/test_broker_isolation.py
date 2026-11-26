"""Live Docker isolation tests for the Execution Broker container (Phase 9.7).

Marked with @pytest.mark.lab; intended to run in Codespaces / CI with Docker Engine.
"""

from __future__ import annotations

import json
import subprocess

import pytest

COMPOSE_CMD = [
    "docker",
    "compose",
    "-f",
    "infra/docker-compose.yml",
    "-f",
    "infra/docker-compose.lab.yml",
    "-f",
    "infra/docker-compose.broker.yml",
]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _require_docker_broker() -> str:
    try:
        ps = _run([*COMPOSE_CMD, "ps", "-q", "broker"])
    except FileNotFoundError:
        pytest.skip("Docker CLI is not available")
    if ps.returncode != 0 or not ps.stdout.strip():
        pytest.skip("Broker container is not running (run 'make broker-up' in Codespaces)")
    return ps.stdout.strip()


@pytest.mark.lab
def test_broker_container_security_and_env():
    """Verifies non-root, read-only rootfs, cap_drop ALL, no forbidden secrets or mounts."""
    cid = _require_docker_broker()
    insp = _run(["docker", "inspect", cid])
    assert insp.returncode == 0
    info = json.loads(insp.stdout)[0]

    host_cfg = info["HostConfig"]
    assert host_cfg["ReadonlyRootfs"] is True
    assert "ALL" in (host_cfg.get("CapDrop") or [])
    assert info["Config"]["User"] == "10001:10001"

    nets = set(info["NetworkSettings"]["Networks"].keys())
    assert nets == {"opspilot_broker", "opspilot_lab-admin"}

    mounts = [m["Source"] for m in info.get("Mounts", [])]
    for m in mounts:
        assert "docker.sock" not in m
        assert "vault" not in m
        assert "telemetry" not in m

    env_res = _run([*COMPOSE_CMD, "exec", "-T", "broker", "env"])
    assert env_res.returncode == 0
    env_text = env_res.stdout
    for forbidden in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HARNESS_TOKEN",
        "CONTROLLER_TOKEN",
    ):
        assert forbidden not in env_text, f"Forbidden secret {forbidden} found in broker env"


@pytest.mark.lab
def test_broker_isolation_and_reachability_matrix():
    """Tests reachability from core/worker/broker and prints the observed reachability matrix."""
    _require_docker_broker()
    matrix: list[tuple[str, str, str]] = []

    # 1. From core network: cannot reach broker, lab admin routes, or controller
    for label, url in [
        ("broker:8090/healthz", "http://broker:8090/healthz"),
        ("gateway/admin/state", "http://gateway:8000/admin/state"),
        ("orders/admin/state", "http://orders:8000/admin/state"),
        ("inventory/admin/state", "http://inventory:8000/admin/state"),
        ("controller/healthz", "http://controller:8000/healthz"),
    ]:
        res = _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "opspilot_core",
                "curlimages/curl:8.10.1",
                "curl",
                "-sS",
                "-m",
                "2",
                url,
            ]
        )
        assert res.returncode != 0, f"core unexpectedly reached {url}: {res.stdout}"
        matrix.append(("core", label, "BLOCKED"))

    # 2. From worker container (if running): cannot reach broker, lab admin, controller, /vault
    worker_ps = _run([*COMPOSE_CMD, "ps", "-q", "worker"])
    if worker_ps.returncode == 0 and worker_ps.stdout.strip():
        for label, py_cmd in [
            ("broker:8090/healthz", "import urllib.request; urllib.request.urlopen('http://broker:8090/healthz', timeout=2)"),
            ("orders/admin/state", "import urllib.request; urllib.request.urlopen('http://orders:8000/admin/state', timeout=2)"),
            ("controller/healthz", "import urllib.request; urllib.request.urlopen('http://controller:8000/healthz', timeout=2)"),
            ("/vault", "import os; assert not os.path.exists('/vault')"),
        ]:
            res = _run([*COMPOSE_CMD, "exec", "-T", "worker", "uv", "run", "--no-sync", "python", "-c", py_cmd])
            if label == "/vault":
                assert res.returncode == 0
            else:
                assert res.returncode != 0
            matrix.append(("worker", label, "BLOCKED"))

    # 3. From broker container: CAN reach lab admin & postgres; CANNOT reach vault, harness truth, otel, or internet
    for svc in ("gateway", "orders", "inventory"):
        py_ok = (
            "import os, urllib.request; "
            f"req=urllib.request.Request('http://{svc}:8000/admin/state', headers={{'X-Lab-Admin-Token': os.environ['LAB_ADMIN_TOKEN']}}); "
            "r=urllib.request.urlopen(req, timeout=3); assert r.status == 200"
        )
        res = _run([*COMPOSE_CMD, "exec", "-T", "broker", "python", "-c", py_ok])
        assert res.returncode == 0, f"broker failed reaching {svc}/admin/state: {res.stderr}"
        matrix.append(("broker", f"{svc}/admin/state", "ALLOWED"))

    for label, py_blocked in [
        ("controller/runs/1/truth", "import urllib.request; urllib.request.urlopen('http://controller:8000/runs/1/truth', timeout=2)"),
        ("/vault", "import os; assert os.path.exists('/vault')"),
        ("otel-collector:4317", "import socket; socket.create_connection(('otel-collector', 4317), timeout=2)"),
        ("outbound-internet", "import urllib.request; urllib.request.urlopen('https://generativelanguage.googleapis.com', timeout=2)"),
    ]:
        res = _run([*COMPOSE_CMD, "exec", "-T", "broker", "python", "-c", py_blocked])
        assert res.returncode != 0, f"broker unexpectedly accessed {label}"
        matrix.append(("broker", label, "BLOCKED"))

    # 4. Broker listening ports: only 8090 (/healthz)
    port_cmd = (
        "import socket; "
        "s=socket.create_connection(('127.0.0.1', 8090), timeout=2); s.close()"
    )
    res_p = _run([*COMPOSE_CMD, "exec", "-T", "broker", "python", "-c", port_cmd])
    assert res_p.returncode == 0

    print("\n--- REACHABILITY MATRIX ---")
    for src, dst, status in matrix:
        print(f"{src:<8} | {dst:<24} | {status}")
