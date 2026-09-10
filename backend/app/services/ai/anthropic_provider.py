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

# BYOK "reasoning_effort" dropdown values → Anthropic ``output_config.effort``
# levels. Anthropic has no "minimal" — it maps to the lowest level.
_EFFORT_MAP = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        key = api_key or settings.anthropic_api_key
        if not key:
            raise UpstreamError("ANTHROPIC_API_KEY is not configured.")
        self._client = AsyncAnthropic(api_key=key)
        self._default_model = model or settings.ai_model
        self._effort = _EFFORT_MAP.get((reasoning_effort or "").strip().lower())
        self._max_tokens_override = max_tokens

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,  # noqa: ARG002 — see comment below
    ) -> AiResponse:
        chosen_model = model or self._default_model
        max_t = max_tokens or self._max_tokens_override or settings.ai_max_tokens

        # NOTE on the dropped parameters:
        # - ``temperature``: current Claude models (Fable 5, Opus 4.7/4.8)
        #   REMOVED the sampling parameters — sending temperature returns a
        #   hard 400. Older models simply fall back to their default. We
        #   therefore never send it; the structured-output prompt doesn't
        #   need low-temperature determinism anyway.
        # - ``thinking``/``effort``: only sent when the user opted in via
        #   the reasoning-effort dropdown. Adaptive thinking is supported on
        #   Claude 4.6+ / Fable 5 but would 400 on older models — opt-in
        #   keeps the default request shape valid for every model id.
        request: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": max_t,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if self._effort:
            request["thinking"] = {"type": "adaptive"}
            request["output_config"] = {"effort": self._effort}

        try:
            # Streaming is mandatory at this output size: the SDK refuses
            # non-streaming requests it estimates will outlive the HTTP
            # timeout (AI_MAX_TOKENS=48k is far past that bar).
            async with self._client.messages.stream(**request) as stream:
                message = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(f"Anthropic API call failed: {exc}") from exc

        # Claude Fable 5 runs safety classifiers that can decline a request
        # with HTTP 200 + stop_reason "refusal" (empty or partial content).
        # Fail loudly instead of persisting an empty report.
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            reason = getattr(details, "explanation", None) or getattr(details, "category", None)
            raise UpstreamError(
                "Claude declined this request (stop_reason=refusal"
                + (f", {reason}" if reason else "")
                + "). Retry with a different model."
            )

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
                "producing the structured JSON. Raise the max output tokens "
                "(admins: AI_MAX_TOKENS / admin settings; own AI config: the "
                "'max output tokens' field in your profile)."
            )

        return AiResponse(
            text=text,
            structured=structured,
            model=chosen_model,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
