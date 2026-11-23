from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from lab.loadgen.models import (
    BurstProfile,
    LoadProfile,
    ProfileType,
    RampProfile,
    SteadyProfile,
)

DEFAULT_PROFILES_PATH = Path(__file__).parent / "profiles.yml"


def parse_profile_dict(raw: dict[str, Any]) -> LoadProfile:
    """Parses a dictionary into a typed LoadProfile instance."""
    p_type = raw.get("type")
    if p_type == ProfileType.STEADY or p_type == "steady":
        return SteadyProfile.model_validate(raw)
    elif p_type == ProfileType.BURST or p_type == "burst":
        return BurstProfile.model_validate(raw)
    elif p_type == ProfileType.RAMP or p_type == "ramp":
        return RampProfile.model_validate(raw)
    else:
        raise ValueError(f"Unknown profile type: {p_type}")


def load_profiles(path: Path | str | None = None) -> dict[str, LoadProfile]:
    """Loads all named profiles from a YAML file."""
    target_path = Path(path) if path else DEFAULT_PROFILES_PATH
    if not target_path.exists():
        raise FileNotFoundError(f"Profiles file not found at: {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    profiles_data = data.get("profiles", data)
    result: dict[str, LoadProfile] = {}

    for key, val in profiles_data.items():
        if isinstance(val, dict):
            if "profile_id" not in val:
                val["profile_id"] = key
            result[key] = parse_profile_dict(val)

    return result


def get_profile(
    name_or_path: str,
    profiles_file: Path | str | None = None,
) -> LoadProfile:
    """Resolves a profile by name from profiles.yml or loads it directly from a YAML file."""
    cand_path = Path(name_or_path)
    if cand_path.exists() and cand_path.is_file():
        with open(cand_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            if "profiles" in data:
                # First profile in file
                first_key = next(iter(data["profiles"]))
                val = data["profiles"][first_key]
                val.setdefault("profile_id", first_key)
                return parse_profile_dict(val)
            val = data
            val.setdefault("profile_id", cand_path.stem)
            return parse_profile_dict(val)

    registry = load_profiles(profiles_file)
    if name_or_path in registry:
        return registry[name_or_path]

    raise KeyError(
        f"Profile '{name_or_path}' not found. Available profiles: {list(registry.keys())}"
    )
