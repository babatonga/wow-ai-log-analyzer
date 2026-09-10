"""Auth-related endpoints: register, login, refresh, password reset, accept invite.

All endpoints here are unauthenticated — captcha alone (when enabled)
mitigates frontend abuse, but a determined attacker calling the API
directly without solving the widget could still brute-force credentials,
enumerate emails or hammer ``/refresh``. Each route therefore also runs
through Redis-backed rate limiting (``app.services.rate_limit``).
Limits are deliberately generous for legitimate retries and only kick
in on sustained abuse.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.deps import ArqDep, LocaleDep, SessionDep
from app.schemas.auth import (
    AcceptInviteIn,
    LoginIn,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshIn,
    RegisterIn,
    TokenPair,
)
from app.schemas.user import UserOut
from app.services import auth_service, captcha, rate_limit

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    # Prefer X-Forwarded-For when behind a reverse proxy (uvicorn started with
    # --proxy-headers). Falls back to the direct socket peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else None


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterIn, session: SessionDep, request: Request, arq: ArqDep
) -> TokenPair:
    # Strict per-IP throttle — registration is rare and bot-prone.
    await rate_limit.enforce_ip(request, arq, bucket="register", limit=10, window_s=3600)
    await captcha.verify_or_raise(payload.captcha_token, remote_ip=_client_ip(request))
    user = await auth_service.register_user(
        session,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        invite_token=payload.invite_token,
        locale=payload.locale,
    )
    await session.commit()
    return TokenPair(**auth_service.issue_token_pair(user))


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginIn, session: SessionDep, request: Request, arq: ArqDep
) -> TokenPair:
    # Two-tier throttle: per-IP catches a single attacker, per-email catches
    # distributed brute-force at one account.
    await rate_limit.enforce_ip(request, arq, bucket="login", limit=20, window_s=60)
    await rate_limit.enforce_identifier(
        arq, bucket="login", identifier=payload.email, limit=10, window_s=300
    )
    await captcha.verify_or_raise(payload.captcha_token, remote_ip=_client_ip(request))
    user = await auth_service.authenticate(session, email=payload.email, password=payload.password)
    await session.commit()
    return TokenPair(**auth_service.issue_token_pair(user))


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshIn, session: SessionDep, request: Request, arq: ArqDep
) -> TokenPair:
    # Refresh has no captcha; rate-limit covers brute-force on stolen
    # refresh tokens. Generous limit so multi-tab / multi-device users
    # don't trip it.
    await rate_limit.enforce_ip(request, arq, bucket="refresh", limit=120, window_s=60)
    tokens = await auth_service.refresh_tokens(session, payload.refresh_token)
    return TokenPair(**tokens)


@router.post("/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_request(
    payload: PasswordResetRequest,
    session: SessionDep,
    locale: LocaleDep,
    request: Request,
    arq: ArqDep,
) -> None:
    # Per-email cap stops "email-bombing" a single user; per-IP cap stops
    # one host iterating addresses.
    await rate_limit.enforce_ip(request, arq, bucket="pwreset_req", limit=10, window_s=3600)
    await rate_limit.enforce_identifier(
        arq, bucket="pwreset_req", identifier=payload.email, limit=3, window_s=3600
    )
    await captcha.verify_or_raise(payload.captcha_token, remote_ip=_client_ip(request))
    await auth_service.initiate_password_reset(session, payload.email, locale)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(
    payload: PasswordResetConfirm, session: SessionDep, request: Request, arq: ArqDep
) -> None:
    # Token brute-force defence — tokens are 256-bit so this is mostly
    # belt-and-braces, but it caps log noise from an attack regardless.
    await rate_limit.enforce_ip(
        request, arq, bucket="pwreset_confirm", limit=20, window_s=60
    )
    await auth_service.confirm_password_reset(session, payload.token, payload.new_password)
    await session.commit()


@router.post("/accept-invite", response_model=UserOut)
async def accept_invite(
    payload: AcceptInviteIn, session: SessionDep, request: Request, arq: ArqDep
) -> UserOut:
    await rate_limit.enforce_ip(request, arq, bucket="invite", limit=20, window_s=60)
    await captcha.verify_or_raise(payload.captcha_token, remote_ip=_client_ip(request))
    user = await auth_service.accept_invite(
        session,
        token=payload.token,
        password=payload.password,
        display_name=payload.display_name,
        locale=payload.locale,
    )
    await session.commit()
    return UserOut.model_validate(user)
