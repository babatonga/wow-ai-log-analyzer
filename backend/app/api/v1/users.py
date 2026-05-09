"""User-self endpoints (/users/me)."""
from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import delete

from app.core.errors import AuthError, NotFoundError, ValidationAppError
from app.core.security import hash_password, verify_password
from app.deps import CurrentUser, SessionDep
from app.models import Report
from app.schemas.user import UserOut, UserUpdateMe
from app.schemas.user_ai import (
    UserAiConfigIn,
    UserAiConfigOut,
    UserAiConfigTestIn,
    UserAiConfigTestResult,
)
from app.schemas.wcl import WclConnectionStatus
from app.services import user_ai_service, wcl_oauth_service

router = APIRouter()


@router.get("/me", response_model=UserOut)
async def read_me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
async def update_me(payload: UserUpdateMe, user: CurrentUser, session: SessionDep) -> UserOut:
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.locale is not None:
        user.locale = payload.locale
    if payload.new_password is not None:
        if not payload.current_password:
            raise ValidationAppError("Current password is required to change the password.")
        if not verify_password(payload.current_password, user.password_hash):
            raise AuthError("Current password is incorrect.")
        user.password_hash = hash_password(payload.new_password)
    await session.commit()
    return UserOut.model_validate(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(user: CurrentUser, session: SessionDep) -> None:
    """Hard-delete the caller's account and *all* of their data (DSGVO Art. 17).

    Owned reports are deleted explicitly so the ``ON DELETE CASCADE`` chain
    walks down through fights, players, gear, casts and analyses. The
    ``WclConnection`` row goes away with the user itself (its FK already
    cascades). Invites the user *created* keep their rows but lose the
    ``created_by`` link (FK is ``ON DELETE SET NULL``).
    """
    await session.execute(delete(Report).where(Report.owner_user_id == user.id))
    await session.flush()
    await session.delete(user)
    await session.commit()


# --- Warcraft Logs OAuth connection ------------------------------------------------


@router.get("/me/wcl-connection", response_model=WclConnectionStatus)
async def read_wcl_connection(user: CurrentUser, session: SessionDep) -> WclConnectionStatus:
    conn = await wcl_oauth_service.get_connection(session, user.id)
    if not conn:
        return WclConnectionStatus(connected=False)
    return WclConnectionStatus(
        connected=True,
        wcl_user_id=conn.wcl_user_id,
        wcl_user_name=conn.wcl_user_name,
        expires_at=conn.expires_at,
        scope=conn.scope,
    )


@router.delete("/me/wcl-connection", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wcl_connection(user: CurrentUser, session: SessionDep) -> None:
    await wcl_oauth_service.disconnect(session, user.id)
    await session.commit()


# --- BYOK AI configuration --------------------------------------------------------


@router.get("/me/ai-config", response_model=UserAiConfigOut | None)
async def read_ai_config(
    user: CurrentUser, session: SessionDep
) -> UserAiConfigOut | None:
    cfg = await user_ai_service.get_config(session, user.id)
    if cfg is None:
        return None
    return user_ai_service.to_out(cfg)


@router.put("/me/ai-config", response_model=UserAiConfigOut)
async def update_ai_config(
    payload: UserAiConfigIn, user: CurrentUser, session: SessionDep
) -> UserAiConfigOut:
    cfg = await user_ai_service.upsert_config(session, user.id, payload)
    await session.commit()
    return user_ai_service.to_out(cfg)


@router.delete("/me/ai-config", status_code=status.HTTP_204_NO_CONTENT)
async def remove_ai_config(user: CurrentUser, session: SessionDep) -> None:
    await user_ai_service.delete_config(session, user.id)
    await session.commit()


@router.post("/me/ai-config/test", response_model=UserAiConfigTestResult)
async def test_ai_config(
    payload: UserAiConfigTestIn,
    user: CurrentUser,
    session: SessionDep,
) -> UserAiConfigTestResult:
    """Probe the user-supplied credentials before they save them.

    No DB write — the frontend sends the form values plus (optionally) a
    plaintext key. When ``api_key`` is empty, fall back to the user's
    saved (encrypted) key so the user can re-test without typing the
    secret again. The provider's auth endpoint is hit and we report
    success/failure + latency.
    """
    return await user_ai_service.test_config_for_user(session, user.id, payload)
