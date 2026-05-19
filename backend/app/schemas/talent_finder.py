"""Pydantic schemas for the Talent-Finder feature.

This module owns:

* Admin-side: the (fight_profile → encounter) mapping the admin
  configures so users only have to pick a fight type.
* User-side: the talent-finder run request + result envelope.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.simulation import FightProfileKey, Precision


# ---------------------------------------------------------------------------
# Admin: encounter mapping per fight-profile
# ---------------------------------------------------------------------------


class EncounterMapEntry(BaseModel):
    """Which WCL encounter to mine top-15 logs from for a fight profile."""

    encounter_id: int = Field(gt=0, description="WCL encounter ID")
    encounter_name: str = Field(default="", max_length=128)
    difficulty: str | None = Field(
        default=None,
        description="WCL raid difficulty ('Mythic', 'Heroic', 'Normal'). "
                    "Null for M+ or non-raid sources.",
    )
    is_raid: bool = Field(
        default=True,
        description="False = Mythic+ / dungeon-style source. Affects which "
                    "difficulty filter the top-logs refresh applies.",
    )


class EncounterMap(BaseModel):
    """Map of every fight profile to its mining source encounter.

    Entries may be ``None`` when the admin hasn't configured that
    profile yet — the talent-finder endpoint must reject runs that
    request an unmapped profile with a clear error.
    """

    model_config = ConfigDict(populate_by_name=True)

    single_target: EncounterMapEntry | None = None
    council: EncounterMapEntry | None = None
    mythic_plus: EncounterMapEntry | None = None
    custom: EncounterMapEntry | None = None
    """``custom`` is included for API symmetry with FightProfileKey but
    is not normally configurable — a "custom" sim profile is the user's
    own knobs, which won't have a meaningful top-log source."""

    def for_profile(self, profile: FightProfileKey) -> EncounterMapEntry | None:
        return getattr(self, profile, None)


# ---------------------------------------------------------------------------
# User: talent-finder run request
# ---------------------------------------------------------------------------


class TalentFinderRunIn(BaseModel):
    """Payload for ``POST /talent-finder/run``.

    The user provides their *base* loadout (simc paste from in-game)
    and picks a fight profile. The backend looks up which encounter
    that profile maps to, pulls the top-15 logs from there, derives
    the variant set, and sims each variant.

    Fields kept tight — anything not explicitly set falls back to
    sensible defaults so the typical UI flow is two clicks (base
    loadout, fight profile).
    """

    label: str = Field(default="", max_length=255)
    simc_profile: str = Field(min_length=20, max_length=200_000)
    fight_profile_key: FightProfileKey = "single_target"
    precision: Precision = "fast"
    """1000 iter direct ("fast"). User explicitly chose single-stage
    1000-iter over a 25-iter screen + 1000-iter refine two-stage."""

    top_n: int = Field(default=15, ge=1, le=30)
    threshold: float = Field(default=0.30, ge=0.05, le=0.95)
    max_builds: int = Field(default=256, ge=1, le=1024)


class TalentFinderDiagnostic(BaseModel):
    """Per-build delta vs the cluster baseline — what's different about
    this variant. Useful in the result UI: a column per contested slot
    showing which of the two picks the variant chose."""

    node_id: int
    node_name: str
    bundle: list[tuple[int, int]]
    """[(entry_id, rank), ...] — empty list = "skip this slot"."""


class TalentFinderBuildOut(BaseModel):
    """One simulated variant in the result list."""

    label: str
    loadout_code: str
    """Blizzard base64 export string — copy-button source in the UI."""

    dps_mean: float = 0.0
    dps_min: float = 0.0
    dps_max: float = 0.0
    dps_stddev: float = 0.0
    contested_picks: list[TalentFinderDiagnostic] = Field(default_factory=list)


class TalentFinderRunOut(BaseModel):
    """Full result envelope returned by the GET endpoint."""

    model_config = ConfigDict(from_attributes=True)

    simulation_id: str
    status: str
    spec_slug: str
    encounter_id: int
    encounter_name: str
    fight_profile_key: FightProfileKey
    n_logs_considered: int
    n_logs_used: int
    threshold_used: float
    builds: list[TalentFinderBuildOut] = Field(default_factory=list)
    """All sim'd builds, DPS-sorted descending. Frontend shows top 5."""

    diagnostics: list[str] = Field(default_factory=list)
    """Human-readable notes: skip reasons, threshold raises, etc."""
