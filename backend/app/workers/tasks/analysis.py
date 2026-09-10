"""Background task: run a queued AI analysis on the worker.

The HTTP layer creates a ``pending`` Analysis row and enqueues this task.
We hand the row over to ``analyzer.request_analysis`` which mutates it in
place to ``running`` → ``succeeded`` / ``failed``. On unhandled exceptions
(or arq's ``job_timeout`` cancelling us mid-generation) we open a fresh
transaction and stamp the row as ``failed`` so the frontend's poll lands
on a definitive terminal state instead of pending forever.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select

from app.db import async_session_factory
from app.models import Analysis, AnalysisStatus
from app.services.ai import analyzer

logger = logging.getLogger(__name__)


async def _mark_failed(aid: uuid.UUID, error_message: str) -> None:
    """Open a fresh session and flip the analysis row to ``failed``.

    Kept separate from the main coroutine so we can wrap it in
    :func:`asyncio.shield` and let it survive the very cancellation that
    triggered the cleanup (otherwise the cancel propagates straight
    through ``await session.commit()`` and the row stays pending).
    """
    try:
        async with async_session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(select(Analysis).where(Analysis.id == aid))
                ).scalar_one_or_none()
                if row is not None and row.status not in (
                    AnalysisStatus.succeeded,
                    AnalysisStatus.failed,
                ):
                    row.status = AnalysisStatus.failed
                    row.error = error_message[:1000]
    except Exception:  # noqa: BLE001
        logger.exception("Could not flip analysis %s to failed", aid)


async def run_analysis_task(_ctx: dict, analysis_id: str) -> None:
    aid = uuid.UUID(analysis_id)
    try:
        async with async_session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(select(Analysis).where(Analysis.id == aid))
                ).scalar_one_or_none()
                if row is None:
                    logger.warning("run_analysis_task: row gone, analysis_id=%s", analysis_id)
                    return
                if row.status not in (AnalysisStatus.pending, AnalysisStatus.running):
                    return  # already terminal — the task may have been re-enqueued
                await analyzer.request_analysis(
                    session,
                    report_id=row.report_id,
                    fight_id=row.fight_id,
                    player_id=row.player_id,
                    requested_by_id=row.requested_by_id,
                    locale=row.locale,
                    analysis_id=aid,
                )
    except asyncio.CancelledError:
        # arq's ``job_timeout`` fired (or the worker is shutting down). The
        # ``except Exception`` below would NOT catch this — ``CancelledError``
        # inherits from ``BaseException``. Without explicit handling the
        # placeholder row stays in ``pending`` forever; the frontend keeps
        # polling and never lands on a terminal status. Mark the row as
        # ``failed`` *under shield* so the cleanup commit survives our own
        # cancellation, then re-raise so arq's bookkeeping is correct.
        logger.warning(
            "Analysis worker cancelled for id=%s (likely job_timeout) — marking failed",
            analysis_id,
        )
        await asyncio.shield(
            _mark_failed(
                aid,
                "Worker timeout — the analysis didn't finish within the per-job "
                "limit (45 min). For BYOK on slow self-hosted models, try a "
                "smaller model, lower context, or run on a GPU.",
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Analysis worker failed for id=%s", analysis_id)
        await _mark_failed(aid, str(exc))
