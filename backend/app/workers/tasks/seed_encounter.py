"""Background task: seed top logs for one encounter across the relevant specs.

The HTTP layer (or the weekly cron) creates a ``TopLogsSeedJob`` row in
``queued`` state and enqueues this task with the job-id. We pick up the row,
flip it to ``running``, walk the specs, increment the progress counter
after each spec, and finally flip ``status`` to ``succeeded`` or
``failed``.

The admin UI polls ``GET /admin/top-logs/seed-jobs`` while any non-terminal
job exists so the user gets a live "12/39 specs · gerade priest_holy"
display.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from arq import Retry
from sqlalchemy import select

from app.db import async_session_factory
from app.models import GameSpec, TopLogsSeedJob
from app.services.top_logs_service import refresh_top_logs_for_spec_encounter
from app.services.wcl.client import WclClient

logger = logging.getLogger(__name__)

# Seeds are serialized via a Redis lock: every concurrent seed shares the
# same WCL rate budget, so N parallel jobs multiply each job's wall-clock
# by ~N and blow the 35-min arq timeout (three jobs died exactly at
# 2100 s during the MID2 initial seed). One-at-a-time, a full 39-spec
# encounter finishes in ~11 min. Jobs that don't get the lock re-queue
# themselves via ``Retry(defer=…)`` — deferral doesn't consume any of the
# job's own timeout, it just parks it back in the queue.
SEED_LOCK_KEY = "seed_encounter:lock"
# Must outlive the arq job timeout (35 min) so a hard-killed worker can't
# leave the queue deadlocked; expiry frees the lock on its own.
SEED_LOCK_TTL_S = 40 * 60
SEED_RETRY_DEFER_S = 120


async def seed_encounter_task(ctx: dict, job_id: str) -> None:
    jid = uuid.UUID(job_id)
    redis = ctx["redis"]
    got_lock = await redis.set(SEED_LOCK_KEY, job_id, nx=True, ex=SEED_LOCK_TTL_S)
    if not got_lock:
        raise Retry(defer=SEED_RETRY_DEFER_S)
    succeeded = False
    job_total = 0
    encounter_id: int | None = None
    try:
        async with async_session_factory() as session:
            # 1) Load + flip to running
            async with session.begin():
                job = (
                    await session.execute(
                        select(TopLogsSeedJob).where(TopLogsSeedJob.id == jid)
                    )
                ).scalar_one_or_none()
                if job is None:
                    logger.warning("seed_encounter_task: job gone, id=%s", job_id)
                    return
                if job.status not in ("queued", "running"):
                    return  # already terminal — duplicate enqueue
                job.status = "running"
                job.started_at = datetime.now(UTC)
                encounter_id = job.encounter_id
                is_raid = job.is_raid
                metric_filter = job.metric_filter

            # 2) Pre-compute spec list inside its own short transaction so the
            # per-spec begin() blocks below don't fight the session autobegin.
            async with session.begin():
                spec_q = select(GameSpec)
                if metric_filter == "hps":
                    spec_q = spec_q.where(GameSpec.role == "healer")
                elif metric_filter == "dps":
                    spec_q = spec_q.where(GameSpec.role != "healer")
                specs = list((await session.execute(spec_q)).scalars().all())
                job_total = len(specs)
                tracked = (
                    await session.execute(
                        select(TopLogsSeedJob).where(TopLogsSeedJob.id == jid)
                    )
                ).scalar_one()
                tracked.total_specs = job_total
                tracked.completed_specs = 0

            # 3) Walk specs. Each spec gets its own transaction so partial
            # progress survives crashes; before each spec we update the
            # ``current_spec_slug`` field so the UI shows where we are.
            async with WclClient() as wcl:
                for spec in specs:
                    async with session.begin():
                        tracked = (
                            await session.execute(
                                select(TopLogsSeedJob).where(TopLogsSeedJob.id == jid)
                            )
                        ).scalar_one()
                        tracked.current_spec_slug = spec.slug

                    try:
                        async with session.begin():
                            await refresh_top_logs_for_spec_encounter(
                                session,
                                spec=spec,
                                encounter_id=encounter_id,
                                is_raid=is_raid,
                                wcl_client=wcl,
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "seed_encounter_task: spec=%s encounter=%s failed",
                            spec.slug,
                            encounter_id,
                        )

                    async with session.begin():
                        tracked = (
                            await session.execute(
                                select(TopLogsSeedJob).where(TopLogsSeedJob.id == jid)
                            )
                        ).scalar_one()
                        tracked.completed_specs += 1

            # 4) Mark terminal succeeded.
            async with session.begin():
                tracked = (
                    await session.execute(
                        select(TopLogsSeedJob).where(TopLogsSeedJob.id == jid)
                    )
                ).scalar_one()
                tracked.status = "succeeded"
                tracked.current_spec_slug = None
                tracked.finished_at = datetime.now(UTC)
        succeeded = True
    finally:
        # Release the serialization lock — but only if it's still ours
        # (after a TTL expiry another job may legitimately hold it now).
        try:
            holder = await redis.get(SEED_LOCK_KEY)
            holder_s = holder.decode() if isinstance(holder, bytes) else holder
            if holder_s == job_id:
                await redis.delete(SEED_LOCK_KEY)
        except Exception:  # noqa: BLE001
            logger.exception("seed_encounter_task: lock release failed")

        # If we exit any other way (arq job_timeout, container restart,
        # bare BaseException), make sure the row doesn't get stuck on
        # ``running``. Open a fresh session because the outer one may have
        # been killed mid-transaction.
        if not succeeded:
            try:
                async with async_session_factory() as cleanup_session:
                    async with cleanup_session.begin():
                        tracked = (
                            await cleanup_session.execute(
                                select(TopLogsSeedJob).where(TopLogsSeedJob.id == jid)
                            )
                        ).scalar_one_or_none()
                        if tracked is not None and tracked.status == "running":
                            tracked.status = "failed"
                            tracked.current_spec_slug = None
                            tracked.finished_at = datetime.now(UTC)
                            tracked.error = (
                                f"task interrupted after {tracked.completed_specs}"
                                f"/{tracked.total_specs} specs"
                            )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "seed_encounter_task: cleanup-on-failure DB write failed"
                )

    if succeeded:
        logger.info(
            "seed_encounter_task done job=%s encounter=%s specs=%s",
            job_id,
            encounter_id,
            job_total,
        )
