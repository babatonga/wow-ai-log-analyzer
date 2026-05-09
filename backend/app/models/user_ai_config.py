"""Per-user AI provider configuration.

Lets each user plug in their own API key for an AI provider (Anthropic,
OpenAI, or any OpenAI-compatible self-hosted server like Ollama, LM Studio,
AnythingLLM, open-webui, vLLM, llama.cpp). The user can then choose at
analyze time whether to use the app-wide AI (admin-managed) or their own.

The API key is encrypted at rest with the same ``SECRET_KEY``-derived Fernet
key that protects WCL OAuth tokens (see ``app/core/crypto.py``). Rotating
``SECRET_KEY`` invalidates every stored config; users will simply have to
re-enter their key.
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserAiConfig(Base, TimestampMixin):
    """One row per user that has BYOK (bring-your-own-key) configured.

    ``provider_type`` is one of:
      - ``anthropic``           — cloud Claude (uses Anthropic SDK)
      - ``openai``              — cloud OpenAI (Azure if base_url set)
      - ``openai_compatible``   — any /v1-compatible endpoint (Ollama,
                                  LM Studio, AnythingLLM, open-webui, vLLM,
                                  llama.cpp, etc.)
    """

    __tablename__ = "user_ai_configs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # Fernet ciphertext of the raw API key. Never returned to the frontend
    # in cleartext — only a "configured" / last-test-result indicator.
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form display name the user picked (e.g. "My Ollama @ home").
    label: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # OpenAI / o-series ``reasoning_effort`` parameter
    # (minimal | low | medium | high). NULL = OpenAI's default
    # (effectively no reasoning for Chat Completions). Ignored for
    # ``provider_type=anthropic`` since Claude has its own ``thinking``
    # control that we don't surface here yet.
    reasoning_effort: Mapped[str | None] = mapped_column(String(8), nullable=True)
