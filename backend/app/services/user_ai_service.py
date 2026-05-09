"""Service helpers for the per-user AI configuration.

The AI provider classes in ``app.services.ai`` already know how to talk to
Anthropic and OpenAI-compatible endpoints. This module wraps them with the
extra plumbing the BYOK feature needs:

- decrypt the user's key at request time;
- run a quick probe call so the user can verify their config before
  triggering a full analysis;
- pick the right ``AiProvider`` for the analyser based on a user config.
"""
from __future__ import annotations

import time
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_str, encrypt_str
from app.models import UserAiConfig
from app.schemas.user_ai import (
    UserAiConfigIn,
    UserAiConfigOut,
    UserAiConfigTestIn,
    UserAiConfigTestResult,
)
from app.services.ai.anthropic_provider import AnthropicProvider
from app.services.ai.base import AiProvider
from app.services.ai.openai_provider import OpenAiCompatibleProvider


def _mask(plaintext: str) -> str:
    """Return ``ab••••••••cd`` so the UI can display *which* key is on file."""
    if len(plaintext) <= 6:
        return "•" * len(plaintext)
    return f"{plaintext[:2]}{'•' * 8}{plaintext[-2:]}"


async def get_config(session: AsyncSession, user_id: uuid.UUID) -> UserAiConfig | None:
    return (
        await session.execute(
            select(UserAiConfig).where(UserAiConfig.user_id == user_id)
        )
    ).scalar_one_or_none()


async def upsert_config(
    session: AsyncSession, user_id: uuid.UUID, payload: UserAiConfigIn
) -> UserAiConfig:
    cfg = await get_config(session, user_id)
    encrypted = encrypt_str(payload.api_key)
    if cfg is None:
        cfg = UserAiConfig(
            user_id=user_id,
            provider_type=payload.provider_type,
            base_url=(payload.base_url or "").strip() or None,
            model=payload.model.strip(),
            api_key_encrypted=encrypted,
            label=payload.label.strip(),
            reasoning_effort=payload.reasoning_effort,
        )
        session.add(cfg)
    else:
        cfg.provider_type = payload.provider_type
        cfg.base_url = (payload.base_url or "").strip() or None
        cfg.model = payload.model.strip()
        cfg.api_key_encrypted = encrypted
        cfg.label = payload.label.strip()
        cfg.reasoning_effort = payload.reasoning_effort
    await session.flush()
    return cfg


async def delete_config(session: AsyncSession, user_id: uuid.UUID) -> None:
    cfg = await get_config(session, user_id)
    if cfg is not None:
        await session.delete(cfg)


def to_out(cfg: UserAiConfig) -> UserAiConfigOut:
    plain = decrypt_str(cfg.api_key_encrypted)
    return UserAiConfigOut(
        provider_type=cfg.provider_type,  # type: ignore[arg-type]
        base_url=cfg.base_url,
        model=cfg.model,
        label=cfg.label,
        api_key_masked=_mask(plain),
        reasoning_effort=cfg.reasoning_effort,  # type: ignore[arg-type]
    )


def provider_for_user_config(cfg: UserAiConfig) -> AiProvider:
    """Build an :class:`AiProvider` from a stored user config.

    Reuses the existing provider classes; the only twist is feeding them
    user-level credentials instead of the app-wide settings values.
    """
    api_key = decrypt_str(cfg.api_key_encrypted)
    if cfg.provider_type == "anthropic":
        return AnthropicProvider(api_key=api_key, model=cfg.model)
    # Both "openai" cloud and "openai_compatible" self-hosted speak the same
    # /v1/chat/completions API — only base_url + key vary.
    base_url = cfg.base_url or "https://api.openai.com/v1"
    return OpenAiCompatibleProvider(
        mode="openai" if cfg.provider_type == "openai" else "local",
        api_key=api_key,
        base_url=base_url,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
    )


async def test_config_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    payload: UserAiConfigTestIn,
) -> UserAiConfigTestResult:
    """Test endpoint wrapper that handles the empty-key fallback.

    The BYOK panel clears the api_key input after a successful save, so
    when the user clicks Test afterwards the request body has no key.
    Look up the persisted (encrypted) key in that case rather than 422.
    """
    api_key = (payload.api_key or "").strip()
    if not api_key:
        cfg = await get_config(session, user_id)
        if cfg is None:
            return UserAiConfigTestResult(
                ok=False,
                detail="No API key — enter one above and click Test, or save first.",
            )
        api_key = decrypt_str(cfg.api_key_encrypted)
    return await test_config(
        UserAiConfigIn(
            provider_type=payload.provider_type,
            base_url=payload.base_url,
            model=payload.model,
            api_key=api_key,
            label=payload.label,
        )
    )


async def test_config(payload: UserAiConfigIn) -> UserAiConfigTestResult:
    """Send a single tiny probe to verify the credentials + endpoint work.

    For Anthropic we call ``/v1/messages`` with one user turn and ``max_tokens=8``.
    For OpenAI / OpenAI-compatible we GET ``/models`` (cheap, no token cost).
    Either way: 4xx response → likely auth/key issue, 5xx/timeout → server.
    """
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if payload.provider_type == "anthropic":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": payload.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": payload.model,
                        "max_tokens": 8,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
            else:
                base = (payload.base_url or "https://api.openai.com/v1").rstrip("/")
                resp = await client.get(
                    f"{base}/models",
                    headers={"Authorization": f"Bearer {payload.api_key}"},
                )
        latency_ms = int((time.time() - t0) * 1000)
        if 200 <= resp.status_code < 300:
            return UserAiConfigTestResult(
                ok=True,
                detail=f"HTTP {resp.status_code} (success)",
                latency_ms=latency_ms,
            )
        body = resp.text[:200].replace("\n", " ")
        return UserAiConfigTestResult(
            ok=False,
            detail=f"HTTP {resp.status_code}: {body}",
            latency_ms=latency_ms,
        )
    except httpx.HTTPError as exc:
        return UserAiConfigTestResult(
            ok=False,
            detail=f"Network/timeout: {exc}",
            latency_ms=int((time.time() - t0) * 1000),
        )
