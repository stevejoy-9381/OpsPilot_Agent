"""Policy table loader and policy_version computation."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from evals.graders.models import DiagnosisCategory, DiagnosisMechanism
from shared.actions import ActionType
from shared.plans import sha256_canonical_json
from shared.targets import DEFAULT_LAB_TARGETS_PATH, TargetRegistry, load_target_registry

DEFAULT_POLICY_TABLE_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent / "config" / "policy_table.yaml"
)
EXPECTED_POLICY_VERSION: str = "df6da5f8fd9b82d7"

ALL_DIAGNOSIS_CATEGORIES: frozenset[str] = frozenset(get_args(DiagnosisCategory))
ALL_DIAGNOSIS_MECHANISMS: frozenset[str] = frozenset(get_args(DiagnosisMechanism))

POLICY_RULE_CODES: tuple[str, ...] = (
    "diagnosis_required",
    "action_consistent_with_diagnosis",
    "service_matches_diagnosis",
    "target_allowlisted",
    "precondition_current",
    "rollback_possible",
    "evidence_valid",
    "one_active_plan",
    "no_recent_action",
    "single_effect",
    "culprit_deploy_mismatch",
    "action_unsupported_by_target",
    "policy_unavailable",
)


class PolicyTableEntry(BaseModel):
    """Single (category, mechanism) -> allowed_actions mapping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: DiagnosisCategory
    mechanism: DiagnosisMechanism
    allowed_actions: tuple[ActionType, ...] = Field(default_factory=tuple)
    description: str = ""


class PolicyTable(BaseModel):
    """Validated action-to-diagnosis policy table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    rules: tuple[PolicyTableEntry, ...]
    by_pair: dict[tuple[str, str], PolicyTableEntry] = Field(default_factory=dict)

    @classmethod
    def from_entries(cls, version: str, entries: list[PolicyTableEntry]) -> PolicyTable:
        by_pair: dict[tuple[str, str], PolicyTableEntry] = {}
        seen_mechanisms: set[str] = set()
        seen_categories: set[str] = set()

        for entry in entries:
            pair = (entry.category, entry.mechanism)
            if pair in by_pair:
                raise ValueError(f"Duplicate (category, mechanism) in policy table: {pair}")
            if entry.mechanism in seen_mechanisms:
                raise ValueError(
                    f"Mechanism '{entry.mechanism}' appears more than once in policy table"
                )
            by_pair[pair] = entry
            seen_mechanisms.add(entry.mechanism)
            seen_categories.add(entry.category)

        missing_cats = ALL_DIAGNOSIS_CATEGORIES - seen_categories
        if missing_cats:
            raise ValueError(f"Policy table missing diagnosis categories: {sorted(missing_cats)}")

        missing_mechs = ALL_DIAGNOSIS_MECHANISMS - seen_mechanisms
        if missing_mechs:
            raise ValueError(f"Policy table missing diagnosis mechanisms: {sorted(missing_mechs)}")

        return cls(version=version, rules=tuple(entries), by_pair=by_pair)

    def allowed_actions_for(self, category: str, mechanism: str) -> tuple[ActionType, ...] | None:
        entry = self.by_pair.get((category, mechanism))
        return entry.allowed_actions if entry is not None else None


def load_policy_table(path: Path | str | None = None) -> PolicyTable:
    """Loads and validates config/policy_table.yaml."""
    table_path = Path(path) if path is not None else DEFAULT_POLICY_TABLE_PATH
    raw = yaml.safe_load(table_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "rules" not in raw or not isinstance(raw["rules"], list):
        raise ValueError(f"Invalid policy table YAML structure in {table_path}")
    version = str(raw.get("version", "1.0"))
    entries = [PolicyTableEntry(**entry) for entry in raw["rules"]]
    return PolicyTable.from_entries(version=version, entries=entries)


def compute_policy_version(
    table: PolicyTable | None = None,
    registry: TargetRegistry | None = None,
) -> str:
    """Computes a deterministic version stamp over policy rules, policy table, and target registry."""
    tbl = table or load_policy_table(DEFAULT_POLICY_TABLE_PATH)
    reg = registry or load_target_registry(DEFAULT_LAB_TARGETS_PATH)
    payload = {
        "table_version": tbl.version,
        "rules": [
            {
                "category": r.category,
                "mechanism": r.mechanism,
                "allowed_actions": [a.value for a in r.allowed_actions],
            }
            for r in tbl.rules
        ],
        "targets": [
            {
                "target_id": t.target_id,
                "service": t.service,
                "kind": t.kind,
                "allowed_actions": [a.value for a in t.allowed_actions],
            }
            for t in reg.targets
        ],
        "rule_codes": list(POLICY_RULE_CODES),
    }
    return sha256_canonical_json(payload)[:16]
