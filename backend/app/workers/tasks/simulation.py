"""Background worker task: run a queued SimC simulation.

The HTTP layer creates a ``pending`` :class:`Simulation` row plus one
``SimulationRun`` per (loadout × fight profile) combination and enqueues
this task. Here we:

1. Flip the parent row to ``running``.
2. Probe the sidecar for its current simc build (so we can stamp it on
   the parent row).
3. Iterate the pre-created children one by one, calling the sidecar
   for each. We deliberately serialise rather than launching them in
   parallel — simc internally pins all logical CPUs already (one
   thread per core), and stacking two sims at once on the same box
   halves both throughputs without lowering wall-clock.
4. Persist the per-run results (DPS summary, top-100 abilities).
5. Roll up the parent's terminal status from the children's statuses.

On unhandled exceptions / cancellation we drop a best-effort failed
status on whatever rows are still ``running`` or ``pending``.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import async_session_factory
from app.models import (
    Simulation,
    SimulationRun,
    SimulationRunStatus,
    SimulationStatus,
)
from app.services import simc_service
from app.services.blizzard_talent_refresh import refresh_loadout_talents

logger = logging.getLogger(__name__)

# Cap the abilities list per run before insert — the long tail past ~100
# rows is sub-1% damage noise and just inflates the JSONB.
_MAX_ABILITIES_PER_RUN = 100

# Talent-finder sweep convergence cutoffs (simc target_error, in %).
# Phase 1 (screening many single-flip sims) runs looser/faster; phase 2
# (the final combine ranking) runs tighter for a trustworthy winner.
# Tighter than this makes the combine round run far too long for a
# UI-triggered job (0.1% ≈ 40 min); 0.15% keeps it ~15 min.
_SWEEP_TARGET_ERROR = 0.3
_COMBINE_TARGET_ERROR = 0.15

# Cap on combine variants per hero tree (2^5). Keeps the second sim
# phase bounded — only the 5 best-delta flips get Cartesian-expanded.
_COMBINE_MAX_PER_TREE = 32


async def _flip_parent(sim_id: uuid.UUID, **updates: Any) -> None:
    """Open a fresh transaction and patch the parent row. Used both
    for normal lifecycle transitions and the failure-cleanup path."""
    async with async_session_factory() as session:
        async with session.begin():
            row = (
                await session.execute(select(Simulation).where(Simulation.id == sim_id))
            ).scalar_one_or_none()
            if row is None:
                return
            for key, value in updates.items():
                setattr(row, key, value)


async def _mark_remaining_failed(sim_id: uuid.UUID, message: str) -> None:
    """Catch-all called from the cancellation / unhandled-exception
    branches: stamp any non-terminal rows under this simulation as
    failed so the UI doesn't show eternally-pending runs."""
    try:
        async with async_session_factory() as session:
            async with session.begin():
                runs = (
                    await session.execute(
                        select(SimulationRun).where(SimulationRun.simulation_id == sim_id)
                    )
                ).scalars().all()
                terminal_left_alone = True
                any_succeeded = False
                for run in runs:
                    if run.status in (
                        SimulationRunStatus.succeeded,
                        SimulationRunStatus.failed,
                    ):
                        any_succeeded = any_succeeded or run.status == SimulationRunStatus.succeeded
                        continue
                    terminal_left_alone = False
                    run.status = SimulationRunStatus.failed
                    run.error = (message or "")[:1000]
                    run.finished_at = datetime.now(UTC)
                parent = (
                    await session.execute(select(Simulation).where(Simulation.id == sim_id))
                ).scalar_one_or_none()
                if parent is not None and parent.status not in (
                    SimulationStatus.succeeded,
                    SimulationStatus.failed,
                ):
                    parent.status = (
                        SimulationStatus.succeeded if any_succeeded else SimulationStatus.failed
                    )
                    parent.error = (message or "")[:1000] if not any_succeeded else None
                    parent.finished_at = datetime.now(UTC)
                # Silence pyflakes: we only use this for side effects.
                _ = terminal_left_alone
    except Exception:  # noqa: BLE001
        logger.exception("Could not flip remaining runs to failed for simulation %s", sim_id)


def _trim_abilities(abilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(abilities)[:_MAX_ABILITIES_PER_RUN]


async def _execute_run(
    *,
    sim_id: uuid.UUID,
    run_id: uuid.UUID,
    base_profile: str,
    loadout_talents: str,
    rotation: str,
    fight_profile_key: str,
    iterations: int,
    custom_overrides: dict[str, Any] | None = None,
    target_error: float | None = None,
) -> bool:
    """Run one (loadout × fight profile) combination. Returns True if
    the run completed successfully, False otherwise. Errors are
    persisted on the run row; the caller doesn't need to rescue
    them.

    ``target_error`` overrides the fight profile's convergence cutoff —
    the talent-finder sweep passes a looser value so its many screening
    sims converge faster."""
    profile_text = simc_service.apply_loadout_talents(base_profile, loadout_talents)

    async with async_session_factory() as session:
        async with session.begin():
            run = (
                await session.execute(select(SimulationRun).where(SimulationRun.id == run_id))
            ).scalar_one()
            run.status = SimulationRunStatus.running
            run.started_at = datetime.now(UTC)

    try:
        result = await simc_service.run_simulation(
            profile=profile_text,
            fight_profile_key=fight_profile_key,
            iterations=iterations,
            rotation=rotation,
            custom_overrides=custom_overrides,
            target_error=target_error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("simc run %s failed: %s", run_id, exc)
        async with async_session_factory() as session:
            async with session.begin():
                run = (
                    await session.execute(select(SimulationRun).where(SimulationRun.id == run_id))
                ).scalar_one()
                run.status = SimulationRunStatus.failed
                run.error = str(exc)[:1000]
                run.finished_at = datetime.now(UTC)
        return False

    abilities = _trim_abilities(result.get("abilities") or [])
    async with async_session_factory() as session:
        async with session.begin():
            run = (
                await session.execute(select(SimulationRun).where(SimulationRun.id == run_id))
            ).scalar_one()
            run.status = SimulationRunStatus.succeeded
            run.dps_mean = float(result.get("dps_mean") or 0.0)
            run.dps_min = float(result.get("dps_min") or 0.0)
            run.dps_max = float(result.get("dps_max") or 0.0)
            run.dps_stddev = float(result.get("dps_stddev") or 0.0)
            run.fight_length_mean = float(result.get("fight_length_mean") or 0.0)
            run.abilities = abilities
            run.finished_at = datetime.now(UTC)
    # The build_version stamp lives on the parent row; the dispatcher
    # picks it up from the first successful result.
    return True


async def run_simulation_task(_ctx: dict, simulation_id: str) -> None:
    sim_id = uuid.UUID(simulation_id)

    # ---- Load parent + plan the children ----
    async with async_session_factory() as session:
        async with session.begin():
            parent = (
                await session.execute(select(Simulation).where(Simulation.id == sim_id))
            ).scalar_one_or_none()
            if parent is None:
                logger.warning("run_simulation_task: row gone, simulation_id=%s", simulation_id)
                return
            if parent.status not in (SimulationStatus.pending, SimulationStatus.running):
                return  # already terminal
            parent.status = SimulationStatus.running
            parent.started_at = datetime.now(UTC)
            base_profile = parent.simc_profile
            iterations = parent.iterations or settings.simc_default_iterations
            loadouts: list[dict[str, Any]] = list(parent.loadouts or [])
            custom_overrides = parent.custom_overrides

            runs = (
                await session.execute(
                    select(SimulationRun).where(SimulationRun.simulation_id == sim_id)
                )
            ).scalars().all()
            # Snapshot id + axes — we need them after the session closes.
            planned: list[dict[str, Any]] = [
                {
                    "run_id": run.id,
                    "loadout_index": run.loadout_index,
                    "rotation": run.rotation,
                    "fight_profile_key": run.fight_profile_key,
                }
                for run in runs
            ]

    # ---- Stamp the simc build on the parent ----
    try:
        version = await simc_service.ping_version()
        build = (version or {}).get("banner") or None
        if build:
            await _flip_parent(sim_id, simc_build=str(build)[:128])
    except Exception:  # noqa: BLE001
        # Non-fatal: log and proceed.
        logger.warning("simc /version probe failed for %s", sim_id, exc_info=True)

    # ---- Refresh stale talent codes against Battle.net ----
    # The SimC Addon serialises saved loadouts from a stale snapshot
    # that's missing hero talents, so trusting the addon's strings
    # makes simc crash on "Invalid Hero Talent tree". We swap each
    # loadout's talent code for the live one from Blizzard's Profile
    # API (matched by longest common prefix). Best-effort: on any
    # failure we fall back to the user's input.
    loadouts = await refresh_loadout_talents(
        simc_profile=base_profile, loadouts=loadouts
    )

    # ---- Execute children one at a time ----
    try:
        succeeded_any = False
        for plan in planned:
            li = plan["loadout_index"]
            loadout = loadouts[li] if 0 <= li < len(loadouts) else {}
            talents = (loadout.get("talents") or "").strip() if isinstance(loadout, dict) else ""
            ok = await _execute_run(
                sim_id=sim_id,
                run_id=plan["run_id"],
                base_profile=base_profile,
                loadout_talents=talents,
                rotation=plan["rotation"],
                fight_profile_key=plan["fight_profile_key"],
                iterations=iterations,
                custom_overrides=custom_overrides,
            )
            succeeded_any = succeeded_any or ok

        await _flip_parent(
            sim_id,
            status=SimulationStatus.succeeded
            if succeeded_any
            else SimulationStatus.failed,
            error=None if succeeded_any else "all runs failed — see per-run errors",
            finished_at=datetime.now(UTC),
        )

    except asyncio.CancelledError:
        # Worker shutdown / job_timeout — flip whatever's still running
        # to failed under asyncio.shield so the cleanup commit survives
        # our own cancellation.
        logger.warning(
            "Simulation worker cancelled for id=%s — marking remaining runs failed",
            simulation_id,
        )
        await asyncio.shield(
            _mark_remaining_failed(
                sim_id,
                "Worker cancelled (timeout or restart) — partial results preserved.",
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Simulation worker failed for id=%s", simulation_id)
        await _mark_remaining_failed(sim_id, str(exc))


async def talent_finder_sweep_task(_ctx: dict, simulation_id: str) -> None:
    """Two-phase sim-driven talent sweep.

    The endpoint pre-creates the parent + phase-1 children (one
    baseline + one per choice-flip, per hero tree). Here we:

    1. Sim phase 1 (baseline + flips) — looser target_error, it's just
       screening which flips gain DPS.
    2. Per hero tree: rank the flips by measured delta vs the baseline,
       Cartesian-combine the positive ones, append those combine
       variants as new loadouts + child runs.
    3. Sim phase 2 (the combine variants) — tighter target_error.
    4. Roll up the parent status.
    """
    from collections import defaultdict

    from app.services.talent_finder_service import (
        DEFAULT_MAX_BUILDS,
        sweep_loadout_entry,
    )
    from app.services.talents import get_dataset
    from app.services.talents.finder import (
        TalentFlip,
        combine_flip_variants,
        materialize_variants,
    )

    sim_id = uuid.UUID(simulation_id)

    # ---- Load parent + plan phase 1 ----
    async with async_session_factory() as session:
        async with session.begin():
            parent = (
                await session.execute(select(Simulation).where(Simulation.id == sim_id))
            ).scalar_one_or_none()
            if parent is None:
                logger.warning("sweep_task: row gone, simulation_id=%s", simulation_id)
                return
            if parent.status not in (SimulationStatus.pending, SimulationStatus.running):
                return
            parent.status = SimulationStatus.running
            parent.started_at = datetime.now(UTC)
            base_profile = parent.simc_profile
            iterations = parent.iterations or settings.simc_default_iterations
            loadouts: list[dict[str, Any]] = list(parent.loadouts or [])
            fight_profile_key = (parent.fight_profiles or ["single_target"])[0]
            runs = (
                await session.execute(
                    select(SimulationRun).where(SimulationRun.simulation_id == sim_id)
                )
            ).scalars().all()
            planned_p1 = [
                {"run_id": r.id, "loadout_index": r.loadout_index} for r in runs
            ]

    try:
        version = await simc_service.ping_version()
        build = (version or {}).get("banner") or None
        if build:
            await _flip_parent(sim_id, simc_build=str(build)[:128])
    except Exception:  # noqa: BLE001
        logger.warning("simc /version probe failed for %s", sim_id, exc_info=True)

    try:
        # ---- Phase 1: baseline + single flips ----
        for plan in planned_p1:
            li = plan["loadout_index"]
            lo = loadouts[li] if 0 <= li < len(loadouts) else {}
            await _execute_run(
                sim_id=sim_id,
                run_id=plan["run_id"],
                base_profile=base_profile,
                loadout_talents=(lo.get("talents") or "").strip(),
                rotation="blizzard",
                fight_profile_key=fight_profile_key,
                iterations=iterations,
                target_error=_SWEEP_TARGET_ERROR,
            )

        # ---- Read phase-1 DPS ----
        async with async_session_factory() as session:
            p1_runs = (
                await session.execute(
                    select(SimulationRun).where(SimulationRun.simulation_id == sim_id)
                )
            ).scalars().all()
            dps_by_index = {
                r.loadout_index: r.dps_mean
                for r in p1_runs
                if r.status == SimulationRunStatus.succeeded
            }

        # ---- Phase 2: rank flips, build combine variants per hero tree ----
        dataset = get_dataset()
        by_tree: dict[int, dict] = defaultdict(lambda: {"baseline": None, "flips": []})
        for li, lo in enumerate(loadouts):
            role = lo.get("tf_role")
            tree = lo.get("tf_hero_tree")
            if tree is None:
                continue
            if role == "baseline":
                by_tree[tree]["baseline"] = (li, lo)
            elif role == "sweep":
                by_tree[tree]["flips"].append((li, lo))

        n_trees = max(1, len(by_tree))
        per_tree_budget = max(2, min(_COMBINE_MAX_PER_TREE, DEFAULT_MAX_BUILDS // n_trees))
        combine_loadouts: list[dict[str, Any]] = []
        for tree, grp in by_tree.items():
            if grp["baseline"] is None:
                continue
            bidx, blo = grp["baseline"]
            base_dps = dps_by_index.get(bidx)
            if base_dps is None:
                continue  # baseline sim failed — can't rank its tree
            base_variant = {
                int(k): v for k, v in (blo.get("tf_variant") or {}).items()
            }
            spec_id = int(blo.get("tf_spec_id") or 0)
            # Keep every flip that isn't *clearly* bad (worse than -0.5%
            # vs the baseline). Near-zero and slightly-negative flips are
            # kept on purpose — two individually-neutral flips can be a
            # positive synergy in combination, which the combine round
            # would miss if we dropped them here.
            drop_cutoff = -0.005 * base_dps
            ranked: list[tuple[float, TalentFlip]] = []
            for fidx, flo in grp["flips"]:
                fdps = dps_by_index.get(fidx)
                if fdps is None:
                    continue
                delta = fdps - base_dps
                if delta < drop_cutoff:
                    continue  # clearly bad — not worth combining
                fm = flo.get("tf_flip") or {}
                ranked.append((
                    delta,
                    TalentFlip(
                        node_id=fm["node_id"],
                        node_name=fm["node_name"],
                        remove=tuple(fm["remove"]),
                        add=tuple(fm["add"]),
                    ),
                ))
            ranked.sort(key=lambda x: -x[0])
            flips_sorted = [tf for _, tf in ranked]
            variants = combine_flip_variants(
                base_variant, flips_sorted, max_builds=per_tree_budget
            )
            builds = materialize_variants(
                variants, dataset, spec_id=spec_id, label_prefix=f"cmb{tree}_"
            )
            for b in builds:
                combine_loadouts.append(
                    sweep_loadout_entry(
                        b, role="combine", hero_tree=tree, spec_id=spec_id
                    )
                )

        # ---- Append combine loadouts + create phase-2 runs ----
        planned_p2: list[dict[str, Any]] = []
        all_loadouts = loadouts
        if combine_loadouts:
            async with async_session_factory() as session:
                async with session.begin():
                    parent = (
                        await session.execute(
                            select(Simulation).where(Simulation.id == sim_id)
                        )
                    ).scalar_one()
                    base_idx = len(parent.loadouts or [])
                    all_loadouts = list(parent.loadouts or []) + combine_loadouts
                    parent.loadouts = all_loadouts
                    new_runs = []
                    for i, lo in enumerate(combine_loadouts):
                        r = SimulationRun(
                            simulation_id=sim_id,
                            loadout_index=base_idx + i,
                            loadout_name=lo["name"],
                            rotation="blizzard",
                            fight_profile_key=fight_profile_key,
                            status=SimulationRunStatus.pending,
                        )
                        session.add(r)
                        new_runs.append(r)
                    await session.flush()
                    planned_p2 = [
                        {"run_id": r.id, "loadout_index": r.loadout_index}
                        for r in new_runs
                    ]

        # ---- Phase 2: sim the combine variants ----
        for plan in planned_p2:
            li = plan["loadout_index"]
            lo = all_loadouts[li] if 0 <= li < len(all_loadouts) else {}
            await _execute_run(
                sim_id=sim_id,
                run_id=plan["run_id"],
                base_profile=base_profile,
                loadout_talents=(lo.get("talents") or "").strip(),
                rotation="blizzard",
                fight_profile_key=fight_profile_key,
                iterations=iterations,
                target_error=_COMBINE_TARGET_ERROR,
            )

        # ---- Roll up ----
        async with async_session_factory() as session:
            final_runs = (
                await session.execute(
                    select(SimulationRun).where(SimulationRun.simulation_id == sim_id)
                )
            ).scalars().all()
        succeeded_any = any(
            r.status == SimulationRunStatus.succeeded for r in final_runs
        )
        await _flip_parent(
            sim_id,
            status=SimulationStatus.succeeded
            if succeeded_any
            else SimulationStatus.failed,
            error=None if succeeded_any else "all sweep runs failed",
            finished_at=datetime.now(UTC),
        )

    except asyncio.CancelledError:
        logger.warning("sweep worker cancelled for id=%s", simulation_id)
        await asyncio.shield(
            _mark_remaining_failed(
                sim_id, "Worker cancelled (timeout or restart) — partial results kept."
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("sweep worker failed for id=%s", simulation_id)
        await _mark_remaining_failed(sim_id, str(exc))


async def cleanup_old_simulations(_ctx: dict) -> None:
    """Cron job: delete simulations older than ``simc_retention_days``.

    The FK cascade on ``simulation_runs.simulation_id`` removes their
    children automatically.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=settings.simc_retention_days)
    async with async_session_factory() as session:
        async with session.begin():
            result = await session.execute(
                delete(Simulation).where(Simulation.created_at < cutoff)
            )
    deleted = result.rowcount or 0
    if deleted:
        logger.info(
            "Pruned %s simulation rows older than %s days (retention cron).",
            deleted,
            settings.simc_retention_days,
        )
