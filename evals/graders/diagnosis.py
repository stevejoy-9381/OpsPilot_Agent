"""Deterministic diagnosis grader evaluating agent findings against scenario ground truth."""

from __future__ import annotations

from typing import Any

from evals.graders.models import Diagnosis, DiagnosisGrade


def normalize_category(cat: str | None) -> str:
    """Normalizes scenario ground truth category to standard diagnosis category."""
    if not cat:
        return "none"
    c = cat.lower().strip()
    if c in (
        "software_defect",
        "bad_deploy",
        "performance_regression",
        "pool_leak",
        "latency_regression",
    ):
        return "bad_deploy"
    if c in ("dependency_failure", "dependency_degradation", "dependency_timeout"):
        return "dependency_failure"
    if c in ("false_alarm", "none"):
        return "false_alarm"
    if c == "unknown":
        return "unknown"
    return c


def normalize_mechanism(mech: str | None) -> str:
    """Normalizes scenario ground truth mechanism to standard diagnosis mechanism."""
    if not mech:
        return "none"
    m = mech.lower().strip()
    if "pool" in m or "leak" in m or "exhaust" in m:
        return "connection_pool_exhaustion"
    if "cache" in m or "bypass" in m or "redundant" in m or "audit" in m:
        return "cache_bypass_latency"
    if "redis" in m or "timeout" in m:
        return "dependency_timeout"
    if "transient" in m or "burst" in m or "spike" in m or m == "none":
        return "none"
    if m == "unknown":
        return "unknown"
    return m


def extract_truth_fields(scenario_truth: Any) -> tuple[str | None, str, str, str | None]:
    """Extracts (root_cause_service, category, mechanism, culprit_deploy)
    from various truth representations.
    """
    # Check if Scenario object or has ground_truth attribute
    if hasattr(scenario_truth, "ground_truth"):
        gt = scenario_truth.ground_truth
        return (
            getattr(gt, "root_cause_service", None),
            getattr(gt, "root_cause_category", ""),
            getattr(gt, "mechanism", ""),
            getattr(gt, "culprit_deploy", None),
        )
    # Check if TruthRecord from vault
    if hasattr(scenario_truth, "root_cause"):
        rc = scenario_truth.root_cause
        if hasattr(rc, "service"):
            svc = rc.service
            mech = rc.mechanism
        elif isinstance(rc, dict):
            svc = rc.get("service")
            mech = rc.get("mechanism", "")
        else:
            svc = None
            mech = ""
        fam = getattr(scenario_truth, "family", "")
        culprit = getattr(scenario_truth, "culprit_deploy_id", None)
        return (svc, fam, mech, culprit)
    # Check if direct GroundTruth model or dict
    if isinstance(scenario_truth, dict):
        gt_dict = scenario_truth.get("ground_truth", scenario_truth)
        svc = gt_dict.get("root_cause_service") or gt_dict.get("service")
        cat = (
            gt_dict.get("root_cause_category")
            or gt_dict.get("category")
            or gt_dict.get("family", "")
        )
        mech = gt_dict.get("mechanism", "")
        culprit = gt_dict.get("culprit_deploy") or gt_dict.get("culprit_deploy_id")
        return (svc, cat, mech, culprit)

    svc = getattr(scenario_truth, "root_cause_service", getattr(scenario_truth, "service", None))
    cat = getattr(scenario_truth, "root_cause_category", getattr(scenario_truth, "category", ""))
    mech = getattr(scenario_truth, "mechanism", "")
    culprit = getattr(
        scenario_truth, "culprit_deploy", getattr(scenario_truth, "culprit_deploy_id", None)
    )
    return (svc, cat, mech, culprit)


def grade_diagnosis(scenario_truth: Any, diagnosis: Diagnosis) -> DiagnosisGrade:
    """Evaluates agent diagnosis against scenario ground truth deterministically.

    Grading Rules:
    - unknown is never correct; marked as abstained.
    - correct = service, category, mechanism match, and culprit deploy matches if bad_deploy.
    - partial = category matches and (service matches OR mechanism matches).
    """
    truth_svc, truth_cat, truth_mech, truth_culprit = extract_truth_fields(scenario_truth)

    norm_truth_cat = normalize_category(truth_cat)
    norm_truth_mech = normalize_mechanism(truth_mech)

    norm_diag_cat = normalize_category(diagnosis.root_cause_category)
    norm_diag_mech = normalize_mechanism(diagnosis.mechanism)

    reasons: list[str] = []

    # 1. Check for abstention
    if diagnosis.root_cause_category == "unknown" or diagnosis.mechanism == "unknown":
        return DiagnosisGrade(
            correct=False,
            partial=False,
            abstained=True,
            reasons=["Agent explicitly abstained from diagnosis (unknown category or mechanism)"],
            matched_service=False,
            matched_category=False,
            matched_mechanism=False,
            matched_culprit_deploy=False,
        )

    # 2. Check service match
    if truth_svc is None:
        matched_service = diagnosis.root_cause_service is None
    elif diagnosis.root_cause_service is None:
        matched_service = False
    else:
        matched_service = diagnosis.root_cause_service.lower().strip() == truth_svc.lower().strip()

    if not matched_service:
        reasons.append(
            f"Service mismatch: expected '{truth_svc}', got '{diagnosis.root_cause_service}'"
        )

    # 3. Check category match
    matched_category = norm_diag_cat == norm_truth_cat
    if not matched_category:
        reasons.append(f"Category mismatch: expected '{norm_truth_cat}', got '{norm_diag_cat}'")

    # 4. Check mechanism match
    matched_mechanism = norm_diag_mech == norm_truth_mech
    if not matched_mechanism:
        reasons.append(f"Mechanism mismatch: expected '{norm_truth_mech}', got '{norm_diag_mech}'")

    # 5. Check culprit deploy match for bad_deploy
    if norm_truth_cat == "bad_deploy":
        if truth_culprit is not None:
            if diagnosis.culprit_deploy_id is None:
                matched_culprit = False
                reasons.append(
                    f"Missing culprit deploy ID for bad_deploy: "
                    f"expected '{truth_culprit}', got null"
                )
            else:
                # Match either exact ID or common identifier
                diag_c = diagnosis.culprit_deploy_id.strip()
                matched_culprit = (
                    (diag_c == truth_culprit)
                    or (truth_culprit in diag_c)
                    or (diag_c in truth_culprit)
                )
                if not matched_culprit:
                    reasons.append(
                        f"Culprit deploy ID mismatch: expected '{truth_culprit}', "
                        f"got '{diagnosis.culprit_deploy_id}'"
                    )
        else:
            matched_culprit = True
    else:
        # Non-deploy incidents should not designate a culprit deploy
        matched_culprit = diagnosis.culprit_deploy_id is None
        if not matched_culprit:
            reasons.append(
                f"Unexpected culprit deploy ID '{diagnosis.culprit_deploy_id}' "
                f"for non-deploy category '{norm_diag_cat}'"
            )

    # 6. Determine final outcome: Correct, Partial, or Incorrect
    is_correct = matched_service and matched_category and matched_mechanism and matched_culprit
    is_partial = not is_correct and matched_category and (matched_service or matched_mechanism)

    if is_correct:
        reasons.insert(
            0,
            "Diagnosis fully matches ground truth across service, category, mechanism, and culprit",
        )
    elif is_partial:
        reasons.insert(
            0, "Diagnosis partially matches: correct category with partial component attribution"
        )
    else:
        reasons.insert(0, "Diagnosis is incorrect: failed core root cause attribution")

    return DiagnosisGrade(
        correct=is_correct,
        partial=is_partial,
        abstained=False,
        reasons=reasons,
        matched_service=matched_service,
        matched_category=matched_category,
        matched_mechanism=matched_mechanism,
        matched_culprit_deploy=matched_culprit,
    )
