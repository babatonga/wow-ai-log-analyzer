"""SimulationCraft DPS-simulation requests + per-loadout/profile runs.

Schema layout
-------------
``simulations`` is the user-facing request: one row per "I want to sim
this character". It stores the /simc profile text, which fight styles
were selected, which talent loadouts were submitted, and an aggregate
status.

``simulation_runs`` is the cartesian product the worker actually
executes: one row per (loadout × fight style). The frontend renders the
matrix as a comparison grid. We keep these as separate rows so partial
failures (e.g. one loadout's profile is malformed) don't tank the rest.

Foreign-key cascade deletes the children when the parent is removed by
the retention cron, so cleanup stays a single DELETE.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as PgEnum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._types import JSONType
from app.models.base import Base, TimestampMixin


class SimulationStatus(str, enum.Enum):
    """Status of a parent ``Simulation`` request.

    ``pending`` = queued, no run has started.
    ``running`` = at least one child is executing.
    ``succeeded`` = every child reached a terminal state and at least one
                    finished cleanly.
    ``failed`` = every child failed (or the sidecar itself errored
                  before any could run).
    """

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class SimulationRunStatus(str, enum.Enum):
    """Per-(loadout × fight-style) state. Independent of parent so one
    failing combination doesn't cancel the others."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Simulation(Base, TimestampMixin):
    __tablename__ = "simulations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Friendly label the user can set in the UI ("Demon Hunter — heroic
    # raid", "M+ farm prep", …). Empty string means "untitled".
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # Raw /simc paste from in-game. Talent strings inside the profile
    # are overridden per-loadout (the worker patches them in before
    # handing the profile to the sidecar). We keep the full text so
    # we can recompute deterministically if a sim has to be re-queued.
    simc_profile: Mapped[str] = mapped_column(Text, nullable=False)
    # Loadouts the user submitted. Each entry is ``{"name": str, "talents": str}``.
    # ``talents`` is the talent-string portion only (without the rest of
    # the /simc profile); the worker substitutes the matching
    # ``talents=``/``class_talents=``/``spec_talents=`` line(s) in.
    loadouts: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Selected fight profiles. Each entry is one of
    # ``"single_target" | "council" | "mythic_plus"`` — the worker maps
    # them to simc fight_style + desired_targets.
    fight_profiles: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    # Rotation modes the user wants to compare against. Each entry is one of
    # ``"simc_default" | "blizzard" | "custom"``. The cartesian product
    # ``loadouts × fight_profiles × rotations`` becomes the per-row
    # ``simulation_runs`` table — so checking "Blizzard one-button" alongside
    # "Community APL" automatically yields a side-by-side comparison.
    rotations: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    iterations: Mapped[int] = mapped_column(default=5000, nullable=False)
    # Symbolic precision band the iteration count came from. Kept on the
    # row so the UI can show "fast / medium / precise" rather than just
    # the raw number, and so a future migration can recompute iterations
    # if we tune the presets.
    precision: Mapped[str] = mapped_column(String(16), default="precise", nullable=False)
    # Sim mode. ``"standard"`` = the original per-loadout DPS compare,
    # ``"talent_finder"`` = the One-Button-Talent-Finder which fans a single
    # base profile into many variant runs derived from top WCL logs.
    # Free-form string so future modes don't need another migration.
    mode: Mapped[str] = mapped_column(
        String(32), default="standard", server_default="standard",
        nullable=False, index=True,
    )
    status: Mapped[SimulationStatus] = mapped_column(
        PgEnum(SimulationStatus, name="simulation_status"),
        default=SimulationStatus.pending,
        nullable=False,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SimC build the sidecar reported when the simulation kicked off.
    # Persisted so the UI can flag stale runs after an upstream update.
    simc_build: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Per-request overrides for the ``custom`` fight profile (fight
    # style / desired_targets / max_time / target_error). NULL for any
    # request that doesn't include the synthetic ``custom`` profile.
    custom_overrides: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    runs: Mapped[list["SimulationRun"]] = relationship(
        "SimulationRun",
        back_populates="simulation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SimulationRun(Base, TimestampMixin):
    __tablename__ = "simulation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Which slot in the parent's ``loadouts`` array this run belongs to.
    # Frontend uses (loadout_index, fight_profile_key) as the grid axes.
    loadout_index: Mapped[int] = mapped_column(default=0, nullable=False)
    loadout_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    rotation: Mapped[str] = mapped_column(String(32), default="simc_default", nullable=False)
    fight_profile_key: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[SimulationRunStatus] = mapped_column(
        PgEnum(SimulationRunStatus, name="simulation_run_status"),
        default=SimulationRunStatus.pending,
        nullable=False,
        index=True,
    )
    dps_mean: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    dps_min: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    dps_max: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    dps_stddev: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fight_length_mean: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Sorted-descending list of {name, school, dps, pct, executes, …}.
    # Capped to top 100 entries (anything past that is noise) before
    # we write to the DB to keep the row size sane.
    abilities: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    simulation: Mapped[Simulation] = relationship("Simulation", back_populates="runs")
