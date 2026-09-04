"""Anthropic Claude provider for structured analysis output."""
from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic

from app.config import settings
from app.core.errors import UpstreamError
from app.services.ai._json import extract_json_object
from app.services.ai.base import AiResponse

logger = logging.getLogger(__name__)


class AnthropicProvider:
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key or settings.anthropic_api_key
        if not key:
            raise UpstreamError("ANTHROPIC_API_KEY is not configured.")
        self._client = AsyncAnthropic(api_key=key)
        self._default_model = model or settings.ai_model

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> AiResponse:
        chosen_model = model or self._default_model
        max_t = max_tokens or settings.ai_max_tokens
        try:
            message = await self._client.messages.create(
                model=chosen_model,
                max_tokens=max_t,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(f"Anthropic API call failed: {exc}") from exc

        text = "".join(part.text for part in message.content if getattr(part, "type", "") == "text")
        usage: Any = getattr(message, "usage", None)

        structured: dict[str, Any] = {}
        try:
            structured = extract_json_object(text)
        except ValueError:
            logger.warning("Claude response did not contain a JSON object; returning text only.")

        # Same guard as the OpenAI-compatible provider: a max_tokens-truncated
        # response without parseable JSON must fail loudly, not persist as an
        # empty "succeeded" report.
        if not structured and getattr(message, "stop_reason", None) == "max_tokens":
            raise UpstreamError(
                f"Claude response hit the {max_t}-token output limit before "
                "producing the structured JSON. Raise AI_MAX_TOKENS."
            )

        return AiResponse(
            text=text,
            structured=structured,
            model=chosen_model,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
