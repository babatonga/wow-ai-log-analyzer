"""Endpoints for the One-Button-Talent-Finder.

Lifecycle:

  GET   /talent-finder/encounter-map   public view of admin mapping
  POST  /talent-finder/run             create + enqueue, returns SimulationOut

The actual simulation runs through the existing
``run_simulation_task`` worker (one per variant loadout). The result
GET endpoint is the standard ``/simulations/{id}`` — the frontend
keys off the parent's ``mode`` field to pick the right result UI.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.errors import ValidationAppError
from app.deps import ArqDep, CurrentUser, SessionDep
from app.models import (
    Simulation,
    SimulationRun,
    SimulationRunStatus,
    SimulationStatus,
)
from app.schemas.simulation import PRECISION_ITERATIONS, SimulationOut
from app.schemas.talent_finder import EncounterMap, TalentFinderRunIn
from app.services import talent_finder_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/encounter-map", response_model=EncounterMap)
async def public_encounter_map(session: SessionDep, _: CurrentUser) -> EncounterMap:
    """Read-only view of the admin-configured fight_profile → encounter map.

    Auth-gated (any logged-in user) so unauthenticated callers can't
    probe the configured raid tier. The frontend uses this to grey-out
    fight profiles that don't have a mapping yet.
    """
    return await talent_finder_service.read_encounter_map(session)


@router.post(
    "/run", response_model=SimulationOut, status_code=status.HTTP_202_ACCEPTED
)
async def run_talent_finder(
    payload: TalentFinderRunIn,
    session: SessionDep,
    user: CurrentUser,
    arq: ArqDep,
) -> SimulationOut:
    """Generate variant builds from top-N WCL logs and enqueue a sim batch.

    Flow:

    1. Parse the spec from the pasted /simc profile.
    2. Look up the admin-configured encounter for the requested fight
       profile. Reject with 422 if the admin hasn't configured this
       profile yet.
    3. If the (spec, encounter) bucket has no cached TopLogs, refresh
       them now (one WCL hit, like the AI analyzer does on-demand).
    4. Run the cluster-analyzer → variant-generator → materializer
       chain. May auto-raise the threshold if the user's value
       produces a build explosion.
    5. Persist as a ``mode="talent_finder"`` :class:`Simulation` with
       one loadout per variant, then enqueue the existing worker.
    """
    # 1. Spec parsing
    try:
        class_db_slug, spec_simc = talent_finder_service.parse_class_and_spec(
            payload.simc_profile
        )
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from exc
    try:
        spec = await talent_finder_service.resolve_game_spec(
            session, class_db_slug, spec_simc
        )
    except ValueError as exc:
        raise ValidationAppError(str(exc)) from exc

    # 2. Encounter lookup
    encounter_map = await talent_finder_service.read_encounter_map(session)
    entry = encounter_map.for_profile(payload.fight_profile_key)
    if entry is None:
        raise ValidationAppError(
            f"No encounter is configured for fight profile "
            f"'{payload.fight_profile_key}'. Ask an admin to set one "
            f"under Admin → Talent-Finder."
        )

    # 3. Build the loadout set. Both strategies fetch WCL
    #    characterRankings via the service (cached snapshot); a
    #    WCL/encounter error surfaces as a 422.
    base_label = (
        payload.label
        or f"Talent-Finder: {entry.encounter_name or entry.encounter_id} ({spec.name_en})"
    )
    iterations = PRECISION_ITERATIONS[payload.precision]

    if payload.strategy == "sweep":
        try:
            pool = await talent_finder_service.build_sweep_screen_pool(
                session,
                spec=spec,
                encounter_id=entry.encounter_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("talent-finder: sweep screen-pool build failed")
            raise ValidationAppError(
                f"Couldn't build the sweep for "
                f"{entry.encounter_name or entry.encounter_id} "
                f"({exc.__class__.__name__}). The encounter ID may be wrong, "
                f"or WCL is unreachable. Try again in a few minutes."
            ) from exc
        if not pool.loadouts:
            raise ValidationAppError(
                "Couldn't build a sweep screening pool. Diagnostics: "
                + "; ".join(pool.diagnostics[:5])
            )
        loadout_entries = pool.loadouts
        sim_mode = "talent_finder_sweep"
        # The sweep converges on target_error; this is just the cap.
        iterations = max(iterations, 20000)
        worker_task = "talent_finder_sweep_task"
    else:
        try:
            run = await talent_finder_service.build_variants_for_spec_encounter(
                session,
                spec=spec,
                encounter_id=entry.encounter_id,
                top_n=payload.top_n,
                initial_threshold=payload.threshold,
                max_builds=payload.max_builds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("talent-finder: variant build failed")
            raise ValidationAppError(
                f"Couldn't build variants for "
                f"{entry.encounter_name or entry.encounter_id} "
                f"({exc.__class__.__name__}). The encounter ID may be wrong, "
                f"or WCL is unreachable. Try again in a few minutes."
            ) from exc
        if not run.builds:
            raise ValidationAppError(
                "Couldn't derive any variant from the cluster. Diagnostics: "
                + "; ".join(run.diagnostics[:5])
            )
        loadout_entries = [
            {
                "name": b.label,
                "talents": b.simc_block,
                "loadout_code": b.loadout_code,
            }
            for b in run.builds
        ]
        sim_mode = "talent_finder"
        worker_task = "run_simulation_task"

    # 4. Persist the Simulation + phase-1 (or all) child runs.
    parent = Simulation(
        requested_by_id=user.id,
        label=base_label[:255],
        simc_profile=payload.simc_profile,
        loadouts=loadout_entries,
        fight_profiles=[payload.fight_profile_key],
        # Talent-Finder always sims with rotation="blizzard" (=
        # one_button_mode=1 + 25% GCD penalty) — that's the whole point.
        rotations=["blizzard"],
        iterations=iterations,
        precision=payload.precision,
        mode=sim_mode,
        status=SimulationStatus.pending,
    )
    session.add(parent)
    await session.flush()  # need parent.id

    for li, entry_dict in enumerate(loadout_entries):
        session.add(
            SimulationRun(
                simulation_id=parent.id,
                loadout_index=li,
                loadout_name=entry_dict.get("name", f"build {li + 1}"),
                rotation="blizzard",
                fight_profile_key=payload.fight_profile_key,
                status=SimulationRunStatus.pending,
            )
        )

    await session.commit()
    await session.refresh(parent)

    await arq.enqueue_job(worker_task, str(parent.id))

    # Re-load with runs eagerly populated so the 202 body matches the
    # standard /simulations response shape.
    row = (
        await session.execute(
            select(Simulation)
            .options(selectinload(Simulation.runs))
            .where(Simulation.id == parent.id)
        )
    ).scalar_one()
    return SimulationOut.model_validate(row)
