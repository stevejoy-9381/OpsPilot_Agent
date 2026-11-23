"""Scenario Catalog for OpsPilot incident simulation runs."""

from __future__ import annotations

from lab.controller.models import (
    FaultInjection,
    HealthThresholds,
    RootCause,
    ScenarioDefinition,
)

SCENARIOS: dict[str, ScenarioDefinition] = {
    # -------------------------------------------------------------------------
    # S1 / F1: Connection Pool Exhaustion on 409 Conflict
    # -------------------------------------------------------------------------
    "S1": ScenarioDefinition(
        scenario_id="S1",
        name="Orders Connection Pool Exhaustion on 409 Conflict",
        description=(
            "Orders service v1.3.0 fails to release database pool connections "
            "when a 409 insufficient stock conflict occurs. Gradual pool exhaustion "
            "eventually causes 503 Pool Timeout failures under sustained load."
        ),
        family="pool_leak",
        root_cause=RootCause(
            service="orders",
            component="db_pool",
            mechanism="connection_pool_leak_on_conflict",
        ),
        culprit_service="orders",
        culprit_version="v1.3.0",
        expected_action={"action": "rollback", "service": "orders"},
        decoys=[],
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=1000.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=10.0,
    ),
    "F1": ScenarioDefinition(
        scenario_id="F1",
        name="Orders Connection Pool Exhaustion on 409 Conflict",
        description=(
            "Orders service v1.3.0 fails to release database pool connections "
            "when a 409 insufficient stock conflict occurs. Gradual pool exhaustion "
            "eventually causes 503 Pool Timeout failures under sustained load."
        ),
        family="connection_leak",
        root_cause=RootCause(
            service="orders",
            component="db_pool",
            mechanism="connection_leak_on_conflict",
        ),
        culprit_service="orders",
        culprit_version="v1.3.0",
        expected_action={"action": "rollback", "service": "orders"},
        decoys=[
            {
                "service": "inventory",
                "action": "config_update",
                "summary": "Routine inventory tier cache verification ping",
            }
        ],
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=1000.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=5.0,
    ),
    # -------------------------------------------------------------------------
    # S2 / F2: Datastore Cache Bypass & Redundant Query
    # -------------------------------------------------------------------------
    "S2": ScenarioDefinition(
        scenario_id="S2",
        name="Inventory Hot-path Cache Bypass and Redundant Query",
        description=(
            "Inventory service v1.2.0 bypasses Redis caching for stock checks "
            "and issues an unneeded secondary query, elevating p95 latency."
        ),
        family="latency_regression",
        root_cause=RootCause(
            service="inventory",
            component="cache_bypass",
            mechanism="cache_bypass_and_redundant_query",
        ),
        culprit_service="inventory",
        culprit_version="v1.2.0",
        expected_action={"action": "rollback", "service": "inventory"},
        decoys=[],
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=250.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=10.0,
    ),
    "F2": ScenarioDefinition(
        scenario_id="F2",
        name="Inventory Hot-path Cache Bypass and Redundant Query",
        description=(
            "Inventory service v1.2.0 bypasses Redis caching for stock checks "
            "and issues an unneeded secondary query, elevating p95 latency."
        ),
        family="performance_regression",
        root_cause=RootCause(
            service="inventory",
            component="cache_bypass",
            mechanism="cache_bypass_and_extra_query",
        ),
        culprit_service="inventory",
        culprit_version="v1.2.0",
        expected_action={"action": "rollback", "service": "inventory"},
        decoys=[
            {
                "service": "orders",
                "action": "deploy",
                "summary": "Benign receipt formatting refactor v1.1.0",
            }
        ],
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=250.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=5.0,
    ),
    # -------------------------------------------------------------------------
    # S3 / F3_REDIS_LATENCY: Redis Dependency Latency Fault
    # -------------------------------------------------------------------------
    "S3": ScenarioDefinition(
        scenario_id="S3",
        name="Inventory Redis Dependency Latency Degradation",
        description="Injected 300ms latency on inventory Redis cache reads.",
        family="dependency_timeout",
        root_cause=RootCause(
            service="inventory",
            component="redis",
            mechanism="redis_dependency_latency_timeout",
        ),
        faults=[
            FaultInjection(
                service="inventory",
                dependency_latency_ms={"redis": 300.0},
            )
        ],
        expected_action={"action": "remediate_dependency", "target": "redis"},
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=350.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=10.0,
    ),
    # -------------------------------------------------------------------------
    # S4: Dependency Latency Fault with Orders Decoy Deploy
    # -------------------------------------------------------------------------
    "S4": ScenarioDefinition(
        scenario_id="S4",
        name="Inventory Redis Latency Timeout with Benign Orders Decoy Deploy",
        description="Redis 300ms latency degradation with benign orders v1.1.0 decoy deploy.",
        family="dependency_timeout_with_decoy",
        root_cause=RootCause(
            service="inventory",
            component="redis",
            mechanism="redis_dependency_latency_timeout",
        ),
        faults=[
            FaultInjection(
                service="inventory",
                dependency_latency_ms={"redis": 300.0},
            )
        ],
        decoys=[
            {
                "service": "orders",
                "action": "deploy",
                "summary": "Benign receipt formatting refactor v1.1.0",
            }
        ],
        expected_action={"action": "remediate_dependency", "target": "redis"},
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=350.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=10.0,
    ),
    # -------------------------------------------------------------------------
    # S5: False Alarm / Transient Burst
    # -------------------------------------------------------------------------
    "S5": ScenarioDefinition(
        scenario_id="S5",
        name="Transient Marketing Traffic Burst (False Alarm)",
        description="Transient traffic surge within healthy system capacity limits.",
        family="false_alarm",
        root_cause=RootCause(
            service=None,
            component=None,
            mechanism="transient_traffic_spike_within_capacity",
        ),
        expected_action={"action": "no_action"},
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=500.0),
        split_tag="eval",
        load_profile="burst_5x",
        warmup_seconds=10.0,
    ),
    # -------------------------------------------------------------------------
    # F3_REDIS_LATENCY: Redis Dependency Latency Fault
    # -------------------------------------------------------------------------
    "F3_REDIS_LATENCY": ScenarioDefinition(
        scenario_id="F3_REDIS_LATENCY",
        name="Inventory Redis Dependency Latency Degradation",
        description="Injected 300ms latency on inventory Redis cache reads.",
        family="dependency_latency",
        root_cause=RootCause(
            service="inventory",
            component="redis",
            mechanism="dependency_latency_fault",
        ),
        faults=[
            FaultInjection(
                service="inventory",
                dependency_latency_ms={"redis": 300.0},
            )
        ],
        expected_action={"action": "remediate_dependency", "target": "redis"},
        health_thresholds=HealthThresholds(error_rate=0.05, p95_latency_ms=350.0),
        split_tag="eval",
        load_profile="steady_20rps",
        warmup_seconds=5.0,
    ),
    # -------------------------------------------------------------------------
    # M1_MULTI_FAULT: Concurrent Multi-Fault Degradation
    # -------------------------------------------------------------------------
    "M1_MULTI_FAULT": ScenarioDefinition(
        scenario_id="M1_MULTI_FAULT",
        name="Concurrent Multi-Fault: Orders DB Error + Inventory Redis Latency",
        description=(
            "Simultaneous fault injection across multiple services: 15% error rate "
            "on Orders DB access combined with 250ms Redis latency on Inventory."
        ),
        family="multi_fault",
        root_cause=RootCause(
            service="orders,inventory",
            component="db,redis",
            mechanism="simultaneous_dependency_degradation",
        ),
        faults=[
            FaultInjection(
                service="orders",
                dependency_error_rate={"db": 0.15},
            ),
            FaultInjection(
                service="inventory",
                dependency_latency_ms={"redis": 250.0},
            ),
        ],
        expected_action={
            "action": "multi_remediation",
            "targets": ["orders:db", "inventory:redis"],
        },
        health_thresholds=HealthThresholds(error_rate=0.08, p95_latency_ms=300.0),
        split_tag="test",
        load_profile="steady_20rps",
        warmup_seconds=5.0,
    ),
}


def get_scenario(scenario_id: str) -> ScenarioDefinition:
    """Retrieves a scenario definition by ID or raises KeyError."""
    if scenario_id not in SCENARIOS:
        raise KeyError(f"Unknown scenario ID '{scenario_id}'. Available: {list(SCENARIOS.keys())}")
    return SCENARIOS[scenario_id]


def list_scenarios() -> list[ScenarioDefinition]:
    """Returns all configured scenarios."""
    return list(SCENARIOS.values())
