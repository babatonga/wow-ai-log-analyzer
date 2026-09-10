"""User / admin / invite schemas."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import UserRole


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: UserRole
    is_active: bool
    locale: str
    created_at: datetime
    last_login_at: datetime | None = None


class UserUpdateMe(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    locale: str | None = Field(default=None, pattern=r"^(en|de)$")
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class InviteIn(BaseModel):
    email: EmailStr
    locale: str = Field(default="en", pattern=r"^(en|de)$")


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    expires_at: datetime
    accepted_at: datetime | None
    revoked: bool
    created_at: datetime


class AdminSettingsOut(BaseModel):
    allow_registration: bool
    ai_provider: str
    ai_model: str
    # OpenAI GPT-5 / o-series reasoning depth. ``None`` (or empty) means
    # "use OpenAI's default" — effectively no reasoning for Chat Completions.
    # Only meaningful when ``ai_provider == "openai"``; the analyzer ignores
    # it for anthropic / local. Mirrors the per-user BYOK ``reasoning_effort``
    # so admins running the app-wide OpenAI key get the same lever.
    openai_reasoning_effort: str | None = None
    # Claude adaptive thinking + ``output_config.effort`` for the app-wide
    # Anthropic provider (Claude 4.6+/Fable). Unset/empty = no explicit
    # reasoning — the safe request shape for every Claude model id.
    anthropic_reasoning_effort: str | None = None
    # Runtime override of the env-level AI_MAX_TOKENS output budget.
    # None = env default. Lets admins react to truncated analyses live.
    ai_max_tokens: int | None = None


class AdminSettingsUpdate(BaseModel):
    allow_registration: bool | None = None
    ai_provider: str | None = None
    ai_model: str | None = None
    # Same valid range as ``UserAiConfig.reasoning_effort``:
    # ``"" | "minimal" | "low" | "medium" | "high"``. Empty string and None
    # both clear the override and fall back to OpenAI's default.
    openai_reasoning_effort: str | None = None
    # Same range; empty clears. Applied to the app-wide Anthropic provider.
    anthropic_reasoning_effort: str | None = None
    # 1000..128000, or 0/None to clear the override (env default applies).
    ai_max_tokens: int | None = None


class AdminUserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
