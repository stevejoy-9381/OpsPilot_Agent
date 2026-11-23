from typing import Any

from pydantic import BaseModel, Field, field_validator

FORBIDDEN_VERSION_SUBSTRINGS = (
    "bad",
    "buggy",
    "leak",
    "defect",
    "fault",
    "broken",
    "error",
    "slow",
    "fail",
)


def validate_neutral_version(version: str) -> str:
    """Enforces that version identifiers follow neutral naming conventions (v1.0.0, etc.)."""
    cleaned = version.strip().lower()
    for forbidden in FORBIDDEN_VERSION_SUBSTRINGS:
        if forbidden in cleaned:
            raise ValueError(
                f"Version string '{version}' contains forbidden non-neutral term '{forbidden}'"
            )
    return version.strip()


class VersionProfile(BaseModel):
    """Encapsulates a service version and its active behavior flags."""

    version: str
    features: dict[str, Any] = Field(default_factory=dict)
    commit_sha: str = "7a8b9c0"
    changelog: str = "Standard maintenance update"

    @field_validator("version")
    @classmethod
    def check_version_neutrality(cls, v: str) -> str:
        return validate_neutral_version(v)


class VersionProfileRegistry:
    """In-memory registry of version profiles allowing dynamic in-process deployment switching."""

    def __init__(
        self,
        profiles: dict[str, VersionProfile] | None = None,
        default_version: str = "v1.0.0",
    ) -> None:
        self._profiles: dict[str, VersionProfile] = profiles or {}
        if default_version not in self._profiles:
            # Seed a default baseline profile if not present
            self._profiles[default_version] = VersionProfile(
                version=default_version,
                features={"baseline": True},
                commit_sha="golden1",
                changelog="Initial release baseline",
            )
        self._active_version: str = default_version

    @property
    def active_version(self) -> str:
        return self._active_version

    def register(self, profile: VersionProfile) -> None:
        """Registers a new version profile."""
        self._profiles[profile.version] = profile

    def has_version(self, version: str) -> bool:
        return version in self._profiles

    def get_active_profile(self) -> VersionProfile:
        return self._profiles[self._active_version]

    def get_profile(self, version: str) -> VersionProfile:
        if version not in self._profiles:
            raise KeyError(f"Profile for version '{version}' is not registered.")
        return self._profiles[version]

    def set_active_version(self, version: str) -> VersionProfile:
        """Switches active running profile to the specified version."""
        if version not in self._profiles:
            available = list(self._profiles.keys())
            raise KeyError(f"Cannot deploy unknown version '{version}'. Available: {available}")
        self._active_version = version
        return self._profiles[version]

    def list_versions(self) -> list[str]:
        return list(self._profiles.keys())
