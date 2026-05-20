"""arq worker definition.

Run via ``arq app.workers.arq_app.WorkerSettings`` (Compose does this for the
``worker`` service).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from arq import func
from arq.connections import RedisSettings
from arq.cron import cron
from sqlalchemy import update

from app.config import settings
from app.db import async_session_factory
from app.models import (
    Analysis,
    AnalysisStatus,
    Report,
    TopLogsSeedJob,
    WowDataImport,
    WowImportStatus,
)
from app.workers.tasks.analysis import run_analysis_task
from app.workers.tasks.report_import import import_report_task
from app.workers.tasks.seed_encounter import seed_encounter_task
from app.workers.tasks.simulation import (
    cleanup_old_simulations,
    run_simulation_task,
    talent_finder_sweep_task,
)
from app.workers.tasks.top_logs import refresh_all_top_logs
from app.workers.tasks.wow_data import refresh_wow_data

logger = logging.getLogger(__name__)


async def _cleanup_zombies(_ctx: dict) -> None:
    """Mark stale in-progress rows as failed at worker boot.

    A worker container restart kills the running coroutine mid-flight; the
    DB row stays in ``in_progress`` / ``running`` indefinitely otherwise.
    Called by arq's ``on_startup`` so the next boot cleans up its predecessor.

    We use a 10-minute floor so jobs that are *legitimately* in flight when
    a brand-new worker starts (e.g. blue/green deploy with overlap) aren't
    falsely killed. In practice all our task types finish well under that.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    async with async_session_factory() as session:
        async with session.begin():
            wow_n = (
                await session.execute(
                    update(WowDataImport)
                    .where(
                        WowDataImport.status == WowImportStatus.in_progress.value,
                        WowDataImport.started_at < cutoff,
                    )
                    .values(
                        status=WowImportStatus.failed.value,
                        finished_at=datetime.now(UTC),
                        phase="",
                        notes="abandoned (worker restart killed the run)",
                    )
                )
            ).rowcount
            seed_n = (
                await session.execute(
                    update(TopLogsSeedJob)
                    .where(
                        TopLogsSeedJob.status.in_(("queued", "running")),
                        TopLogsSeedJob.started_at < cutoff,
                    )
                    .values(
                        status="failed",
                        finished_at=datetime.now(UTC),
                        current_spec_slug=None,
                        error="abandoned (worker restart killed the run)",
                    )
                )
            ).rowcount
            ana_n = (
                await session.execute(
                    update(Analysis)
                    .where(
                        Analysis.status.in_(
                            (AnalysisStatus.pending, AnalysisStatus.running)
                        ),
                        Analysis.created_at < cutoff,
                    )
                    .values(
                        status=AnalysisStatus.failed,
                        error="abandoned (worker restart killed the run)",
                    )
                )
            ).rowcount
            rep_n = (
                await session.execute(
                    update(Report)
                    .where(
                        Report.import_status == "importing",
                        Report.created_at < cutoff,
                    )
                    .values(
                        import_status="failed",
                        import_error="abandoned (worker restart killed the run)",
                    )
                )
            ).rowcount
    if wow_n or seed_n or ana_n or rep_n:
        logger.info(
            "worker startup zombie cleanup: wow_data=%s seed_jobs=%s analyses=%s reports=%s",
            wow_n,
            seed_n,
            ana_n,
            rep_n,
        )


def _parse_field(field: str) -> int | set[int] | None:
    """Convert one cron field into the int / set[int] / None form arq expects."""
    field = field.strip()
    if field in ("", "*"):
        return None
    if "," in field:
        return {int(x) for x in field.split(",") if x.strip()}
    if "-" in field:
        start, end = field.split("-", 1)
        return set(range(int(start), int(end) + 1))
    if "/" in field:
        # arq has no native step support; treat "*/n" as "any" and let the
        # min-fire-interval be the natural cadence of the schedule.
        return None
    return int(field)


def _parse_cron(expr: str) -> dict[str, int | set[int] | None]:
    parts = expr.split()
    if len(parts) != 5:
        logger.warning("Bad TOP_LOGS_CRON %r — falling back to '0 4 * * *'", expr)
        parts = ["0", "4", "*", "*", "*"]
    minute, hour, day, month, weekday = parts
    try:
        return {
            "minute": _parse_field(minute),
            "hour": _parse_field(hour),
            "day": _parse_field(day),
            "month": _parse_field(month),
            "weekday": _parse_field(weekday),
        }
    except ValueError:
        logger.exception("Could not parse TOP_LOGS_CRON %r — falling back to 04:00 daily.", expr)
        return {"minute": 0, "hour": 4}


_cron_kwargs = {k: v for k, v in _parse_cron(settings.top_logs_cron).items() if v is not None}


class WorkerSettings:
    redis_settings = RedisSettings(
        host=settings.redis_host, port=settings.redis_port, database=settings.redis_db
    )
    functions = [
        refresh_all_top_logs,
        refresh_wow_data,
        import_report_task,
        # 30 min — cloud Anthropic/OpenAI typically return in 1-3 min, but
        # BYOK users with a self-hosted Ollama / llama.cpp on consumer
        # hardware (no GPU or partial offload) can legitimately take
        # 15-25 min for a single analysis. The 10 min default would cut
        # those runs off mid-generation. The HTTP client in
        # ``OpenAiCompatibleProvider`` is bumped to the same ceiling so
        # both layers cap simultaneously instead of one fighting the
        # other.
        func(run_analysis_task, timeout=30 * 60),
        # 35 min — 39 specs × ~17 s WCL latency easily blows past the
        # 10 min default for fresh-cache seeds.
        func(seed_encounter_task, timeout=35 * 60),
        # 35 min — worst case is 3 loadouts × 3 fight profiles × 5000
        # iter where DungeonSlice can sit at ~3-5 min per run on a
        # mid-range box. The sidecar enforces its own per-call limit
        # (1800 s) so even a runaway profile can't pin a worker for
        # 35 min total — this ceiling is just the wall-clock envelope.
        func(run_simulation_task, timeout=35 * 60),
        # 50 min — the talent-finder sweep runs two sim phases back to
        # back (baseline + ~20 single-flip screens, then up to ~256
        # combine variants). Each sim converges on target_error so the
        # count, not raw iterations, drives wall-clock.
        func(talent_finder_sweep_task, timeout=50 * 60),
    ]
    cron_jobs = [
        cron(refresh_all_top_logs, name="refresh_all_top_logs", **_cron_kwargs),
        # WoW DBC dumps drop ~daily right after a patch and only every couple
        # of weeks otherwise, so checking once a week (Tuesday 03:00 UTC, well
        # before the EU reset top-logs job) is plenty.
        cron(refresh_wow_data, name="refresh_wow_data", weekday=2, hour=3, minute=0),
        # Daily 04:30 UTC simulation retention sweep. Runs after the
        # top-logs job so we never compete with it for DB locks.
        cron(
            cleanup_old_simulations,
            name="cleanup_old_simulations",
            hour=4,
            minute=30,
        ),
    ]
    on_startup = _cleanup_zombies
    keep_result = 86400
    # Number of jobs that may run in parallel inside this worker process.
    # Top-logs refresh, report import and analysis can all coexist; bump
    # this if you see jobs queueing up.
    max_jobs = 4
    # Default per-job timeout. Long enough for a slow Anthropic call or a
    # large Mythic raid import. Per-task overrides are wired via
    # ``arq.func(..., timeout=...)`` in the ``functions`` list above for
    # tasks that legitimately take longer (e.g. seed_encounter_task walks
    # 39 specs and bumps to 35 min).
    job_timeout = 600
    # Don't retry analysis or import jobs by default — failures are stored
    # on the row itself and the user can re-trigger from the UI.
    max_tries = 1
