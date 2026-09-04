"""OpenAI-compatible provider.

Works against three things, picked via the ``mode`` argument:

- OpenAI cloud (``mode="openai"``) — uses ``openai_api_key`` / ``openai_base_url``.
- A locally hosted OpenAI-compatible server (``mode="local"``) — vLLM, Ollama,
  LM Studio, and friends all expose the same chat-completions interface.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core.errors import UpstreamError
from app.services.ai._json import extract_json_object
from app.services.ai.base import AiResponse

logger = logging.getLogger(__name__)

Mode = Literal["openai", "local"]

# Canonical cloud-OpenAI URL. Hardcoded as a safety net because the SDK's
# own fallback (``os.environ["OPENAI_BASE_URL"]`` then the default) breaks
# when the env var is set to an empty string — see the constructor below.
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# 30 min HTTP read timeout. Cloud OpenAI typically replies in 1-3 min, but
# BYOK users on a self-hosted Ollama / llama.cpp endpoint with consumer
# hardware (no GPU or partial offload) can take 15-25 min to generate.
# Connect/write/pool stay tight so we still fail fast on a dead endpoint.
# Matches the arq ``run_analysis_task`` job_timeout so neither layer cuts
# the other off mid-generation.
_AI_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30 * 60, write=60.0, pool=15.0)


class OpenAiCompatibleProvider:
    def __init__(
        self,
        *,
        mode: Mode,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Build a provider.

        When ``api_key``/``base_url``/``model`` are passed (BYOK path: a user
        has stored their own key in their profile), those win. Otherwise we
        fall back to the app-wide settings — that's the legacy admin-managed
        path used when an analysis is triggered without ``use_own_ai=true``.

        ``reasoning_effort`` overrides ``settings.openai_reasoning_effort``
        for the per-user BYOK path (the user's profile lets them pick their
        own GPT-5 reasoning level). None / empty string falls through to
        the app-wide setting.
        """
        self._mode = mode
        self._reasoning_effort_override = (reasoning_effort or "").strip().lower() or None
        if api_key is not None:
            # BYOK / user-config path. Trust whatever the caller hands us
            # for self-hosted (``local`` mode), but for cloud OpenAI we
            # backstop an empty/missing URL with the canonical default —
            # see the admin-path comment below for why.
            cleaned_byok = (base_url or "").strip()
            client_base_url = (
                (cleaned_byok or _DEFAULT_OPENAI_BASE_URL)
                if mode == "openai"
                else (cleaned_byok or None)
            )
            self._client = AsyncOpenAI(
                api_key=api_key, base_url=client_base_url, timeout=_AI_HTTP_TIMEOUT
            )
            self._default_model = model or (
                settings.ai_model if mode == "openai" else settings.local_ai_model
            )
        elif mode == "openai":
            if not settings.openai_api_key:
                raise UpstreamError("OPENAI_API_KEY is not configured.")
            # IMPORTANT: never pass ``base_url=None`` to the OpenAI SDK
            # here. When ``base_url`` is None the SDK falls back to
            # ``os.environ["OPENAI_BASE_URL"]`` — and our .env ships
            # ``OPENAI_BASE_URL=`` (empty) by default. The SDK then uses
            # that empty string verbatim, so every request URL ends up
            # missing its scheme and httpx fails with
            # ``UnsupportedProtocol: Request URL is missing an
            # 'http://' or 'https://' protocol``. Pass the canonical
            # OpenAI URL explicitly when our setting is empty.
            cleaned = (settings.openai_base_url or "").strip()
            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                base_url=cleaned or _DEFAULT_OPENAI_BASE_URL,
                timeout=_AI_HTTP_TIMEOUT,
            )
            self._default_model = settings.ai_model
        else:
            self._client = AsyncOpenAI(
                api_key=settings.local_ai_api_key or "dummy",
                base_url=settings.local_ai_base_url,
                timeout=_AI_HTTP_TIMEOUT,
            )
            self._default_model = settings.local_ai_model

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> AiResponse:
        chosen = model or self._default_model
        max_t = max_tokens or settings.ai_max_tokens

        # Qwen 3.5/3.6 (and other recent reasoning-capable models) split
        # output into ``content`` (final answer) and ``reasoning_content``
        # (chain-of-thought). For analytical tasks like log coaching the
        # extra reasoning lifts quality, so default on for the local
        # provider. Set LOCAL_AI_ENABLE_THINKING=false in .env if you want
        # raw speed (~10s instead of ~30-60s) at a quality cost. The flag
        # is silently ignored by models that don't support it.
        # ``chat_template_kwargs.enable_thinking`` is a llama.cpp /
        # Qwen-template-specific knob — it toggles the chain-of-thought
        # block in the Jinja chat template. Real OpenAI's API rejects
        # the parameter outright (HTTP 400 "Unknown parameter:
        # 'chat_template_kwargs'"), so only send it for local mode.
        extra_body: dict[str, Any] = {}
        if self._mode == "local":
            extra_body["chat_template_kwargs"] = {
                "enable_thinking": settings.local_ai_enable_thinking,
            }
            if settings.local_ai_enable_thinking:
                # Qwen reasoning models degenerate into endless repetition
                # loops at near-greedy sampling — observed live: a German
                # analysis burned the full 48k output budget inside the
                # reasoning trace without ever finishing. Qwen's model card
                # prescribes temperature≈0.6 / top_p 0.95 for thinking
                # mode, so floor the caller's (analysis uses 0.2) to that.
                temperature = max(temperature, 0.6)
                extra_body["top_p"] = 0.95
        # OpenAI's GPT-5 / o-series reasoning. Without this parameter
        # the Chat Completions API silently runs the model in "no
        # reasoning" mode (reasoning_tokens=0 in usage) — verified by
        # smoke test against /v1/chat/completions. ``high`` engages
        # full reasoning at the cost of ~5–10× output tokens.
        # Valid values: minimal | low | medium | high. Empty / unset
        # = OpenAI default (de facto minimal for Chat Completions).
        # Per-user BYOK config overrides the app-wide setting.
        if self._mode == "openai":
            effort = (
                self._reasoning_effort_override
                or (settings.openai_reasoning_effort or "").strip().lower()
            )
            if effort in ("minimal", "low", "medium", "high"):
                extra_body["reasoning_effort"] = effort

        # OpenAI deprecated ``max_tokens`` in favour of
        # ``max_completion_tokens`` for GPT-5+ and o1+ models — those
        # API endpoints REJECT ``max_tokens`` outright with a 400.
        # Older OpenAI models (gpt-4o family) accept the new name as
        # well, so we use it unconditionally for cloud mode. Local
        # mode (llama.cpp's OpenAI shim) only knows ``max_tokens``,
        # so we keep that there.
        token_kwargs: dict[str, int] = (
            {"max_completion_tokens": max_t}
            if self._mode == "openai"
            else {"max_tokens": max_t}
        )

        # GPT-5 / o-series with reasoning engaged REJECT every
        # non-default ``temperature`` value (``Only the default (1) value
        # is supported``). For local llama.cpp and for OpenAI in
        # non-reasoning mode (gpt-4o-family or gpt-5* without
        # reasoning_effort), the parameter is honoured normally.
        # → omit ``temperature`` whenever we just told OpenAI to reason.
        omit_temperature = (
            self._mode == "openai" and "reasoning_effort" in extra_body
        )

        try:
            resp = await self._client.chat.completions.create(
                model=chosen,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **token_kwargs,
                **({} if omit_temperature else {"temperature": temperature}),
                extra_body=extra_body or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(f"{self._mode} chat completion failed: {exc}") from exc

        # Surface token usage for observability — the reasoning_tokens
        # number tells us whether GPT-5/o-series actually engaged the
        # reasoning trace or short-circuited (fast = often 0). For
        # llama.cpp / Qwen the field is absent and stays at 0.
        usage = getattr(resp, "usage", None)
        if usage is not None:
            details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", 0) if details else 0
            logger.info(
                "ai-call mode=%s model=%s prompt=%s completion=%s reasoning=%s",
                self._mode,
                chosen,
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
                reasoning_tokens,
            )

        choice = resp.choices[0] if resp.choices else None
        message = choice.message if choice else None
        # Some models split output into ``content`` (final answer) and
        # ``reasoning_content`` (CoT). If reasoning was actually emitted,
        # the JSON might live in there — extract_json_object walks balanced
        # braces and will find it regardless.
        primary = (getattr(message, "content", None) or "").strip() if message else ""
        reasoning = (getattr(message, "reasoning_content", None) or "").strip() if message else ""
        if primary:
            text = primary
        elif reasoning:
            text = reasoning
        else:
            text = ""
        usage: Any = getattr(resp, "usage", None)
        structured: dict[str, Any] = {}
        try:
            structured = extract_json_object(text)
        except ValueError:
            logger.warning(
                "%s response did not contain a parseable JSON object; returning text only.",
                self._mode,
            )

        # A response cut off at the output-token limit BEFORE any JSON was
        # produced is a hard failure, not a degraded success — with
        # thinking enabled the model can burn the entire budget on its
        # reasoning trace and the "answer" is then just truncated CoT.
        # Storing that as ``succeeded`` renders an empty report in the UI
        # with no hint at the cause, so fail loudly instead. (If the JSON
        # parsed despite finish_reason=length — e.g. only trailing prose
        # got cut — we keep the result.)
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        if not structured and finish_reason == "length":
            raise UpstreamError(
                f"{self._mode} response hit the {max_t}-token output limit before "
                "producing the structured JSON (the reasoning trace consumed the "
                "whole budget). Raise AI_MAX_TOKENS or disable "
                "LOCAL_AI_ENABLE_THINKING."
            )

        return AiResponse(
            text=text,
            structured=structured,
            model=chosen,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )
