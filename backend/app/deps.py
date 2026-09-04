"""FastAPI dependency helpers."""
from __future__ import annotations

import uuid
from typing import Annotated

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import AuthError, ForbiddenError
from app.core.i18n import locale_dependency
from app.core.security import decode_token
from app.db import get_session
from app.models import User, UserRole

SessionDep = Annotated[AsyncSession, Depends(get_session)]
LocaleDep = Annotated[str, Depends(locale_dependency)]


# ----- arq job-queue pool ----------------------------------------------------

_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    """Lazily-built singleton arq client used by API endpoints to enqueue jobs."""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(
            RedisSettings(
                host=settings.redis_host,
                port=settings.redis_port,
                database=settings.redis_db,
            )
        )
    return _arq_pool


ArqDep = Annotated[ArqRedis, Depends(get_arq_pool)]


async def _resolve_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("Missing or malformed Authorization header.")
    return authorization.split(" ", 1)[1].strip()


async def get_current_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = await _resolve_bearer(authorization)
    payload = decode_token(token, expected_kind="access")
    sub = payload.get("sub")
    if not sub:
        raise AuthError("Invalid token: missing subject.")
    # JWT subjects are strings; User.id is a Uuid column. Postgres/asyncpg
    # coerces str→uuid implicitly, SQLite (tests) does not — compare with a
    # real UUID so both dialects behave the same.
    try:
        user_id = uuid.UUID(str(sub))
    except ValueError as exc:
        raise AuthError("Invalid token: malformed subject.") from exc
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthError("Account no longer active.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.admin:
        raise ForbiddenError("Admin role required.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
