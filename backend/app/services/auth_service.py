"""Auth + invite + password-reset business logic."""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.email import send_email
from app.core.errors import AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from app.core.security import (
    create_token,
    decode_token,
    generate_secure_token,
    hash_password,
    verify_password,
)
from app.models import AppSetting, Invite, User, UserRole

logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _registration_allowed(session: AsyncSession) -> bool:
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == "allow_registration"))
    ).scalar_one_or_none()
    if not row:
        return settings.allow_registration
    value = row.value or {}
    return bool(value.get("enabled", settings.allow_registration))


async def register_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    invite_token: str | None,
    locale: str | None = None,
) -> User:
    """Register a new user. Either open registration must be enabled OR a valid invite is required."""
    invite: Invite | None = None
    if invite_token:
        invite = await _consume_invite(session, invite_token, expected_email=email)
    elif not await _registration_allowed(session):
        raise ForbiddenError("Public registration is disabled. Ask an admin for an invitation.")

    existing = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("An account with this email already exists.")

    user = User(
        email=email.lower(),
        display_name=display_name,
        password_hash=hash_password(password),
        role=UserRole.user,
        is_active=True,
        locale=locale if locale in {"en", "de"} else "en",
    )
    session.add(user)

    if invite is not None:
        invite.accepted_at = datetime.now(UTC)

    await session.flush()
    return user


async def _consume_invite(session: AsyncSession, token: str, *, expected_email: str | None) -> Invite:
    digest = _hash_token(token)
    invite = (
        await session.execute(select(Invite).where(Invite.token_hash == digest))
    ).scalar_one_or_none()
    if not invite:
        raise NotFoundError("Invite not found.")
    if invite.revoked:
        raise ForbiddenError("This invite has been revoked.")
    if invite.accepted_at is not None:
        raise ConflictError("This invite was already used.")
    if invite.expires_at < datetime.now(UTC):
        raise ForbiddenError("This invite has expired.")
    if expected_email and invite.email.lower() != expected_email.lower():
        raise ValidationAppError("The email does not match the invitation.")
    return invite


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise AuthError("Invalid email or password.")
    if not user.is_active:
        raise ForbiddenError("Account is inactive.")
    user.last_login_at = datetime.now(UTC)
    return user


def issue_token_pair(user: User) -> dict[str, str]:
    return {
        "access_token": create_token(str(user.id), kind="access", extra={"role": user.role.value}),
        "refresh_token": create_token(str(user.id), kind="refresh"),
        "token_type": "bearer",
    }


async def refresh_tokens(session: AsyncSession, refresh_token: str) -> dict[str, str]:
    payload = decode_token(refresh_token, expected_kind="refresh")
    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Invalid refresh token.")
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.is_active:
        raise AuthError("Account no longer active.")
    return issue_token_pair(user)


async def create_invite(
    session: AsyncSession,
    *,
    email: str,
    inviter: User,
    locale: str,
) -> Invite:
    if locale not in {"en", "de"}:
        locale = "en"
    existing_user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if existing_user:
        raise ConflictError("A user with this email already exists.")

    raw = generate_secure_token()
    invite = Invite(
        email=email.lower(),
        token_hash=_hash_token(raw),
        invited_by_id=inviter.id,
        expires_at=datetime.now(UTC) + timedelta(days=14),
    )
    session.add(invite)
    await session.flush()

    try:
        await send_email(
            to=email,
            template="invite",
            locale=locale,
            context={
                "app_name": settings.app_name,
                "inviter_name": inviter.display_name,
                "token": raw,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to deliver invite email — token still stored in DB.")
    return invite


async def revoke_invite(session: AsyncSession, invite_id: Any) -> None:
    invite = (
        await session.execute(select(Invite).where(Invite.id == invite_id))
    ).scalar_one_or_none()
    if not invite:
        raise NotFoundError("Invite not found.")
    invite.revoked = True


async def initiate_password_reset(session: AsyncSession, email: str, locale: str) -> None:
    """Always succeeds (does not leak which emails exist)."""
    user = (
        await session.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()
    if not user:
        return
    token = create_token(str(user.id), kind="reset")
    try:
        await send_email(
            to=user.email,
            template="password_reset",
            locale=locale,
            context={
                "app_name": settings.app_name,
                "user_name": user.display_name,
                "token": token,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send password reset email")


async def confirm_password_reset(session: AsyncSession, token: str, new_password: str) -> User:
    payload = decode_token(token, expected_kind="reset")
    user = (
        await session.execute(select(User).where(User.id == payload["sub"]))
    ).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found.")
    user.password_hash = hash_password(new_password)
    return user


async def accept_invite(
    session: AsyncSession,
    *,
    token: str,
    password: str,
    display_name: str,
    locale: str | None = None,
) -> User:
    invite = await _consume_invite(session, token, expected_email=None)
    existing = (
        await session.execute(select(User).where(User.email == invite.email))
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("An account with this email already exists.")
    user = User(
        email=invite.email,
        display_name=display_name,
        password_hash=hash_password(password),
        role=UserRole.user,
        is_active=True,
        locale=locale if locale in {"en", "de"} else "en",
    )
    session.add(user)
    invite.accepted_at = datetime.now(UTC)
    await session.flush()
    return user
