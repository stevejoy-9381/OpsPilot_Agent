"""End-to-End lifecycle, oracle recovery, and determinism tests for all Lab Scenarios.

Covers S1-S5:
  - S1: pool_leak (orders connection pool leak on 409 conflict)
  - S2: latency_regression (inventory cache bypass extra query)
  - S3: dependency_timeout (redis latency fault)
  - S4: dependency_timeout_with_decoy (redis latency with benign orders decoy deploy)
  - S5: false_alarm (transient marketing traffic burst)

Verifies:
  1. start -> warm-up -> inject
  2. Oracle expected status (unhealthy for S1-S4, healthy for S5)
  3. Lab primitive application (rollback/reset)
  4. Oracle post-action recovery verification for S1-S4
  5. Run determinism: two consecutive executions produce identical truth records
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from lab.controller.models import HealthThresholds, OracleResult, TruthRecord
from lab.controller.oracle import Oracle
from lab.controller.scenarios import get_scenario
from lab.controller.service import ControllerService
from lab.controller.settings import ControllerSettings
from lab.controller.vault import Vault
from lab.controller_lib.deploy import DeployRecord, rollback


@pytest.fixture
def e2e_vault_path(tmp_path: Path) -> str:
    """Fixture providing a clean SQLite vault for e2e execution."""
    return str(tmp_path / "e2e_vault.db")


@pytest.fixture
def e2e_vault(e2e_vault_path: str) -> Vault:
    return Vault(db_path=e2e_vault_path)


@pytest.fixture
def e2e_settings(e2e_vault_path: str) -> ControllerSettings:
    return ControllerSettings(
        controller_token=SecretStr("test-e2e-controller-token"),
        harness_token=SecretStr("test-e2e-harness-token"),
        lab_admin_token=SecretStr("test-e2e-admin-token"),
        vault_db_path=e2e_vault_path,
        gateway_url="http://mock-gateway:8000",
        orders_url="http://mock-orders:8000",
        inventory_url="http://mock-inventory:8000",
        prometheus_url="http://mock-prometheus:9090",
    )


def _mock_promql_for_scenario(scenario_id: str, phase: str) -> tuple[float, float]:
    """Returns synthetic (error_rate, p95_latency_ms) for scenario and phase."""
    if phase == "post_action" or scenario_id == "S5":
        # Healthy status
        return (0.01, 85.0)

    # Post-injection unhealthy metrics per scenario
    if scenario_id == "S1":
        # 503 errors from pool exhaustion
        return (0.28, 120.0)
    elif scenario_id == "S2":
        # Elevated p95 query latency from cache bypass (> 250ms threshold)
        return (0.01, 480.0)
    elif scenario_id in ("S3", "S4"):
        # Elevated Redis latency breaching 350ms threshold
        return (0.02, 520.0)
    return (0.01, 80.0)


async def execute_scenario_e2e_cycle(
    scenario_id: str,
    service: ControllerService,
    oracle: Oracle,
    seed: int = 42,
    warmup_s: float = 0.01,
) -> tuple[TruthRecord, OracleResult, OracleResult | None]:
    """Executes start -> warm-up -> inject -> oracle evaluation -> action -> recovery."""
    scenario = get_scenario(scenario_id)

    # 1. Start run (world reset, background load, warm-up, injection, vault record)
    run_id = await service.start_run(scenario_id, seed=seed, warmup_s=warmup_s)
    truth = service.get_truth(run_id)
    assert truth is not None, f"TruthRecord missing for run {run_id}"

    thresholds = HealthThresholds(
        error_rate=scenario.health_thresholds.error_rate,
        p95_latency_ms=scenario.health_thresholds.p95_latency_ms,
    )

    # 2. Evaluate Oracle status post-injection
    inj_err, inj_p95 = _mock_promql_for_scenario(scenario_id, phase="post_injection")
    with patch.object(Oracle, "query_promql", side_effect=[inj_err, inj_p95]):
        oracle_injected = await oracle.oracle_health(thresholds, window="1m")

    # Assert oracle expected status
    if scenario_id == "S5":
        assert oracle_injected.healthy is True, "S5 false_alarm must evaluate healthy"
    else:
        assert (
            oracle_injected.healthy is False
        ), f"{scenario_id} must evaluate unhealthy post-injection"

    # 3. Apply expected action via lab primitives
    oracle_recovered: OracleResult | None = None
    expected = scenario.expected_action
    action = expected.get("action")

    if action == "rollback":
        target_svc = expected["service"]
        await rollback(
            service=target_svc,
            service_url=service._get_service_url(target_svc),
            admin_token=service.settings.lab_admin_token.get_secret_value(),
            database_url=service.settings.database_url,
            http_client=service._external_client,
        )
    elif action == "remediate_dependency":
        # Remediate injected faults via world reset or fault clearance
        await service.reset_world()
    elif action == "no_action":
        # For false alarm, no operational write action is required
        pass

    # 4. Assert recovery for S1-S4
    if scenario_id != "S5":
        rec_err, rec_p95 = _mock_promql_for_scenario(scenario_id, phase="post_action")
        with patch.object(Oracle, "query_promql", side_effect=[rec_err, rec_p95]):
            oracle_recovered = await oracle.oracle_health(thresholds, window="1m")
        assert oracle_recovered.healthy is True, f"{scenario_id} must evaluate healthy post-action"

    # Clean up run
    await service.stop_run(run_id)
    return truth, oracle_injected, oracle_recovered


# -----------------------------------------------------------------------------
# Fast CI Subset Tests (S1 and S3 with short timings)
# -----------------------------------------------------------------------------
@pytest.mark.lab
@pytest.mark.lab_fast
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", ["S1", "S3"])
async def test_fast_ci_scenario_lifecycle(
    scenario_id: str,
    e2e_settings: ControllerSettings,
    e2e_vault: Vault,
) -> None:
    """Fast subset covering S1 (deploy pool_leak) and S3 (dependency_timeout).
    
    Runs with fast 10ms timings for continuous integration.
    """
    service = ControllerService(settings=e2e_settings, vault=e2e_vault)
    oracle = Oracle(prometheus_url=e2e_settings.prometheus_url)

    fake_record = DeployRecord(
        deploy_id=f"dep-fixed-{scenario_id.lower()}",
        service="orders",
        old_version="v1.0.0",
        new_version="v1.3.0",
        deployed_at=pytest.importorskip("datetime").datetime.now(
            pytest.importorskip("datetime").timezone.utc
        ),
        author="release-bot@internal",
        summary="Rollback deployment",
        changed_files=["app.py"],
        config_changes={},
        diff_text="--- diff",
    )

    with patch.object(ControllerService, "reset_world", new_callable=AsyncMock), \
         patch.object(ControllerService, "_run_background_load", new_callable=AsyncMock), \
         patch("lab.controller.service.deploy", new_callable=AsyncMock, return_value=fake_record), \
         patch(
             "lab.controller_lib.deploy.deploy", new_callable=AsyncMock, return_value=fake_record
         ), \
         patch("psycopg.connect"):

        # Run 1
        truth_1, oracle_inj_1, oracle_rec_1 = await execute_scenario_e2e_cycle(
            scenario_id=scenario_id,
            service=service,
            oracle=oracle,
            seed=77,
            warmup_s=0.01,
        )
        assert oracle_inj_1.healthy is False
        assert oracle_rec_1 is not None and oracle_rec_1.healthy is True

        # Run 2 (Determinism assertion)
        truth_2, oracle_inj_2, oracle_rec_2 = await execute_scenario_e2e_cycle(
            scenario_id=scenario_id,
            service=service,
            oracle=oracle,
            seed=77,
            warmup_s=0.01,
        )

        # Assert identical truth record across runs
        assert truth_1.scenario_id == truth_2.scenario_id == scenario_id
        assert truth_1.family == truth_2.family
        assert truth_1.root_cause == truth_2.root_cause
        assert truth_1.expected_action == truth_2.expected_action
        assert truth_1.health_thresholds == truth_2.health_thresholds
        assert truth_1.split_tag == truth_2.split_tag
        assert truth_1.decoys == truth_2.decoys


# -----------------------------------------------------------------------------
# Full Scenario Suite Tests (S1 through S5)
# -----------------------------------------------------------------------------
@pytest.mark.lab
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", ["S1", "S2", "S3", "S4", "S5"])
async def test_full_scenario_e2e_lifecycle_and_determinism(
    scenario_id: str,
    e2e_settings: ControllerSettings,
    e2e_vault: Vault,
) -> None:
    """Full test suite covering S1-S5 lifecycle, oracle recovery, and truth determinism."""
    service = ControllerService(settings=e2e_settings, vault=e2e_vault)
    oracle = Oracle(prometheus_url=e2e_settings.prometheus_url)

    fake_record = DeployRecord(
        deploy_id=f"dep-fixed-{scenario_id.lower()}",
        service="orders",
        old_version="v1.0.0",
        new_version="v1.3.0",
        deployed_at=pytest.importorskip("datetime").datetime.now(
            pytest.importorskip("datetime").timezone.utc
        ),
        author="release-bot@internal",
        summary="Rollback deployment",
        changed_files=["app.py"],
        config_changes={},
        diff_text="--- diff",
    )

    with patch.object(ControllerService, "reset_world", new_callable=AsyncMock), \
         patch.object(ControllerService, "_run_background_load", new_callable=AsyncMock), \
         patch("lab.controller.service.deploy", new_callable=AsyncMock, return_value=fake_record), \
         patch(
             "lab.controller_lib.deploy.deploy", new_callable=AsyncMock, return_value=fake_record
         ), \
         patch("psycopg.connect"):

        # Cycle 1
        truth_1, inj_oracle_1, rec_oracle_1 = await execute_scenario_e2e_cycle(
            scenario_id=scenario_id,
            service=service,
            oracle=oracle,
            seed=101,
            warmup_s=0.01,
        )

        if scenario_id == "S5":
            assert inj_oracle_1.healthy is True
            assert rec_oracle_1 is None
        else:
            assert inj_oracle_1.healthy is False
            assert rec_oracle_1 is not None and rec_oracle_1.healthy is True

        # Cycle 2: Execute identical scenario run to assert determinism
        truth_2, inj_oracle_2, rec_oracle_2 = await execute_scenario_e2e_cycle(
            scenario_id=scenario_id,
            service=service,
            oracle=oracle,
            seed=101,
            warmup_s=0.01,
        )

        # Assert truth records are strictly identical (except generated timestamps/run_id)
        assert truth_1.scenario_id == truth_2.scenario_id == scenario_id
        assert truth_1.family == truth_2.family
        assert truth_1.root_cause == truth_2.root_cause
        assert truth_1.expected_action == truth_2.expected_action
        assert truth_1.health_thresholds == truth_2.health_thresholds
        assert truth_1.split_tag == truth_2.split_tag
        assert truth_1.decoys == truth_2.decoys
