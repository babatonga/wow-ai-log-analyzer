"""Aggregated v1 router."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    analysis,
    auth,
    meta,
    reports,
    shared_analyses,
    simulations,
    talent_finder,
    top_logs,
    users,
    wcl_oauth,
    wow_data,
)

api_router = APIRouter()
api_router.include_router(meta.router, tags=["meta"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(wcl_oauth.router, prefix="/auth/wcl", tags=["auth-wcl"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(wow_data.router, prefix="/admin", tags=["admin-wow-data"])
api_router.include_router(reports.router, prefix="/reports", tags=["reports"])
api_router.include_router(top_logs.router, prefix="/top-logs", tags=["top-logs"])
api_router.include_router(analysis.router, prefix="/analyses", tags=["analyses"])
# Anonymous read-only endpoint for share links. Mounted at its own prefix
# (instead of under /analyses) so the token-based path can't accidentally
# get UUID-validated by /analyses/{analysis_id}.
api_router.include_router(
    shared_analyses.router, prefix="/shared-analyses", tags=["shared-analyses"]
)
api_router.include_router(simulations.router, prefix="/simulations", tags=["simulations"])
api_router.include_router(
    talent_finder.router, prefix="/talent-finder", tags=["talent-finder"]
)
