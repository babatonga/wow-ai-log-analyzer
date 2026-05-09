"""Schemas for the per-user AI provider configuration."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProviderType = Literal["anthropic", "openai", "openai_compatible"]
# OpenAI / o-series ``reasoning_effort`` parameter. None = OpenAI's
# default (de facto no reasoning for Chat Completions).
ReasoningEffort = Literal["minimal", "low", "medium", "high"]


class UserAiConfigOut(BaseModel):
    """Sanitised view of a user's AI config — never returns the key cleartext."""

    model_config = ConfigDict(from_attributes=True)

    provider_type: ProviderType
    base_url: str | None
    model: str
    label: str
    # Frontend renders this as "ab••••••••cd" so the user can confirm
    # which key is on file without us ever exposing the secret.
    api_key_masked: str
    reasoning_effort: ReasoningEffort | None = None


class UserAiConfigIn(BaseModel):
    provider_type: ProviderType
    base_url: str | None = None
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=64)
    # Optional. Only honoured when provider_type is openai/openai_compatible
    # and the model recognises it (GPT-5 / o-series). Anthropic & most
    # self-hosted servers ignore the field.
    reasoning_effort: ReasoningEffort | None = None


class UserAiConfigTestIn(BaseModel):
    """Same shape as :class:`UserAiConfigIn` but with ``api_key`` optional.

    The Test button sits BELOW the Save button on the BYOK panel — once a
    user has saved a config the panel clears the api_key input (we don't
    want it floating around in form state), so a subsequent Test would
    submit an empty key. We treat empty ``api_key`` as "use the saved
    one" rather than rejecting the request with 422.
    """

    provider_type: ProviderType
    base_url: str | None = None
    model: str = Field(min_length=1, max_length=128)
    # Empty/missing → fall back to the user's saved (Fernet-encrypted) key.
    api_key: str = Field(default="", max_length=512)
    label: str = Field(default="", max_length=64)
    reasoning_effort: ReasoningEffort | None = None


class UserAiConfigTestResult(BaseModel):
    ok: bool
    detail: str
    latency_ms: int | None = None
