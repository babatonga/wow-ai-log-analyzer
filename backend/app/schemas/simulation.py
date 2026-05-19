"""Pydantic schemas for the SimulationCraft endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.simulation import SimulationRunStatus, SimulationStatus

Rotation = Literal["simc_default", "blizzard", "custom"]
# ``custom`` lets the user pick any combination of fight style /
# desired_targets / max_time / target_error — paired with
# :class:`CustomProfileOverrides` on the request body.
FightProfileKey = Literal["single_target", "council", "mythic_plus", "custom"]
Precision = Literal["fast", "medium", "precise"]


class CustomProfileOverrides(BaseModel):
    """Per-request overrides for the ``custom`` fight profile.

    Mirrors the four knobs raidbots surfaces in its "Advanced" panel.
    All fields are optional; missing values fall back to the backend
    ``CUSTOM_PROFILE_DEFAULTS`` so the user can tweak just one knob
    without having to spell out the rest.
    """

    fight_style: str = Field(
        default="Patchwerk",
        max_length=64,
        description="simc fight_style (Patchwerk, DungeonSlice, …).",
    )
    desired_targets: int = Field(default=1, ge=1, le=20)
    max_time: int = Field(default=300, ge=10, le=3600)
    target_error: float = Field(default=0.05, ge=0.0, le=5.0)

# Iteration count for each precision preset. The /info endpoint exposes
# these so the frontend can show an indicative count next to the button.
# Tuned so "fast" returns in ~10 s on a fast box, "medium" in ~25 s, and
# "precise" in ~45-60 s for a single-target Patchwerk run. M+ DungeonSlice
# scales roughly 3x because the fight is longer + multi-target work.
PRECISION_ITERATIONS: dict[str, int] = {
    "fast": 1000,
    "medium": 2500,
    "precise": 5000,
}


class LoadoutIn(BaseModel):
    """One talent build the user wants to compare.

    ``talents`` holds the talent-string portion the user pasted (just
    the ``talents=…``/``class_talents=…``/``spec_talents=…`` lines, or
    a single B64 talent string the in-game UI exports). Empty means
    "use whatever the base /simc profile already has".

    ``loadout_code`` is the original Blizzard base64 export string (the
    single token that round-trips with the in-game UI). The standard
    /simulations flow leaves it empty; the talent-finder flow fills it
    so the result UI can show a copy-to-clipboard token per variant.
    """

    name: str = Field(default="", max_length=120)
    talents: str = Field(default="", max_length=20000)
    loadout_code: str = Field(default="", max_length=4096)


class SimulationCreate(BaseModel):
    """Payload for ``POST /simulations``.

    The cartesian product ``loadouts × fight_profiles × rotations``
    becomes the per-row ``SimulationRun`` table. The frontend renders
    the grid along whichever axes have > 1 entry, so a 1-loadout +
    1-fight + 2-rotation request collapses to a simple two-row
    "with / without Blizzard" comparison.
    """

    label: str = Field(default="", max_length=255)
    simc_profile: str = Field(min_length=20, max_length=200_000)
    fight_profiles: list[FightProfileKey] = Field(min_length=1, max_length=3)
    loadouts: list[LoadoutIn] = Field(min_length=1, max_length=3)
    # 1-3 rotation modes to simulate. Defaults to community APL only.
    # ``custom`` lets advanced users keep whatever ``actions=`` block
    # they pasted in the profile.
    rotations: list[Rotation] = Field(
        default_factory=lambda: ["simc_default"], min_length=1, max_length=3
    )
    precision: Precision = "precise"
    # Only consulted when ``fight_profiles`` contains ``"custom"``. The
    # same override block applies to every custom run in the request —
    # we don't currently support per-run knobs because the cartesian
    # combinator wouldn't have anywhere sensible to put them.
    custom_overrides: CustomProfileOverrides | None = None


class SimulationAbility(BaseModel):
    name: str = ""
    spell_id: int = 0
    spell_name: str = ""
    school: str = ""
    dps: float = 0.0
    pct: float = 0.0
    damage_per_iter: float = 0.0
    executes: float = 0.0
    hits: float = 0.0
    crit_pct: float = 0.0


class SimulationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    simulation_id: uuid.UUID
    loadout_index: int
    loadout_name: str
    rotation: Rotation
    fight_profile_key: FightProfileKey
    status: SimulationRunStatus
    dps_mean: float
    dps_min: float
    dps_max: float
    dps_stddev: float
    fight_length_mean: float
    abilities: list[SimulationAbility]
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SimulationOut(BaseModel):
    """Full detail view — includes every run for the comparison grid."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requested_by_id: uuid.UUID | None
    label: str
    simc_profile: str
    loadouts: list[LoadoutIn]
    fight_profiles: list[FightProfileKey]
    rotations: list[Rotation]
    iterations: int
    precision: Precision
    custom_overrides: CustomProfileOverrides | None = None
    mode: str = "standard"
    """Sim mode: ``"standard"`` or ``"talent_finder"``. Drives which UI
    template the frontend renders."""
    status: SimulationStatus
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    simc_build: str | None
    created_at: datetime
    updated_at: datetime
    runs: list[SimulationRunOut] = Field(default_factory=list)


class SimulationListItem(BaseModel):
    """Lightweight view used by ``GET /simulations`` (no per-run detail)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    status: SimulationStatus
    iterations: int
    precision: Precision
    fight_profiles: list[FightProfileKey]
    rotations: list[Rotation]
    loadout_count: int
    created_at: datetime
    finished_at: datetime | None


class PaginatedSimulations(BaseModel):
    items: list[SimulationListItem]
    total: int
    page: int
    page_size: int
