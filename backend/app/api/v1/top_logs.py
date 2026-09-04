"""Endpoints for browsing top logs and (admin) refreshing them."""
from __future__ import annotations

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.core.errors import NotFoundError
from app.deps import AdminUser, CurrentUser, LocaleDep, SessionDep
from app.models import GameSpec
from app.schemas.analysis import TopLogOut
from app.services import top_logs_service
from app.services.wow_data_service import resolve_encounter_names_with_fallback

router = APIRouter()


@router.get("", response_model=list[TopLogOut])
async def list_top_logs(
    session: SessionDep,
    user: CurrentUser,
    locale: LocaleDep,
    spec_slug: str = Query(..., description="GameSpec slug, e.g. 'priest_holy'"),
    encounter_id: int | None = Query(default=None),
    metric: str | None = Query(default=None, pattern=r"^(dps|hps)$"),
    difficulty: int | None = Query(default=None, ge=1, le=5),
) -> list[TopLogOut]:
    rows = await top_logs_service.list_top_logs(
        session,
        spec_slug=spec_slug,
        encounter_id=encounter_id,
        metric=metric,
        difficulty=difficulty,
    )
    pairs = list({(r.encounter_id, r.encounter_name) for r in rows})
    name_map = await resolve_encounter_names_with_fallback(
        session, locale=user.locale or locale, encounters=pairs
    )
    out: list[TopLogOut] = []
    for r in rows:
        item = TopLogOut.model_validate(r)
        item.encounter_name_localized = name_map.get(r.encounter_id)
        out.append(item)
    return out


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_top_logs(
    spec_slug: str,
    encounter_id: int,
    session: SessionDep,
    _: AdminUser,
    metric: str | None = Query(default=None, pattern=r"^(dps|hps)$"),
) -> dict[str, int]:
    spec = (
        await session.execute(select(GameSpec).where(GameSpec.slug == spec_slug))
    ).scalar_one_or_none()
    if not spec:
        raise NotFoundError(f"Unknown spec_slug: {spec_slug}")
    rows = await top_logs_service.refresh_top_logs_for_spec_encounter(
        session, spec=spec, encounter_id=encounter_id, metric=metric
    )
    await session.commit()
    return {"refreshed": len(rows)}
