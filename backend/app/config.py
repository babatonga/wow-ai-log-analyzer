"""Centralised settings, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AnyUrl, EmailStr, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General ---
    app_name: str = "WoW AI Log Analyzer"
    app_env: Literal["production", "development", "test"] = "production"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:3000"

    # --- Backend ---
    backend_host: str = "0.0.0.0"  # noqa: S104  (intentional in container)
    backend_port: int = 8000
    secret_key: str = Field(default="insecure-dev-key", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 60
    jwt_refresh_ttl_days: int = 30
    cors_allow_origins: str = "http://localhost:3000"

    # --- Database ---
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "wowanalyzer"
    postgres_user: str = "wowanalyzer"
    postgres_password: str = "change-me"

    # --- Redis ---
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    # --- WCL ---
    wcl_client_id: str = ""
    wcl_client_secret: str = ""
    wcl_api_url: str = "https://www.warcraftlogs.com/api/v2/client"
    wcl_user_api_url: str = "https://www.warcraftlogs.com/api/v2/user"
    wcl_oauth_token_url: str = "https://www.warcraftlogs.com/oauth/token"
    wcl_oauth_authorize_url: str = "https://www.warcraftlogs.com/oauth/authorize"
    # Must EXACTLY match a redirect URL registered with the WCL API client.
    # Production: https://wow.swsp.eu/api/v1/auth/wcl/callback
    # Local dev:  http://localhost:8000/api/v1/auth/wcl/callback
    wcl_redirect_uri: str = "http://localhost:8000/api/v1/auth/wcl/callback"
    wcl_oauth_scope: str = "view-user-profile"

    # --- AI ---
    # "anthropic" → cloud Claude. "openai" → cloud OpenAI / Azure-OpenAI.
    # "local" → an OpenAI-compatible server you run yourself (vLLM/Ollama/...).
    # "disabled" → app-wide AI is off; users can still analyse with their
    # own API keys via the BYOK flow on the profile page.
    ai_provider: Literal["anthropic", "openai", "local", "disabled"] = "anthropic"
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-4-6"
    ai_max_tokens: int = 8000

    # --- AI: OpenAI cloud (only used when ai_provider == "openai") ---
    openai_api_key: str = ""
    # Leave empty to hit OpenAI directly; set to a custom URL for Azure OpenAI
    # or any OpenAI-compatible cloud proxy.
    openai_base_url: str = ""
    # Reasoning effort for GPT-5 / o-series models (others ignore it).
    # Empty or unrecognised value → OpenAI default (de facto "minimal" for
    # Chat Completions, i.e. reasoning_tokens ≈ 0). Set to ``high`` for
    # full reasoning at the cost of ~5–10× output tokens. Valid values:
    # minimal | low | medium | high.
    openai_reasoning_effort: str = ""

    # --- AI: local model via the bundled llama.cpp container ---
    # Used when ai_provider == "local". The compose service ``local-ai``
    # exposes the OpenAI-compatible inference endpoint here.
    local_ai_base_url: str = "http://local-ai:8080/v1"
    # The local-ai sidecar's management API (port 8081 inside the same
    # container). Used to switch models, list cached GGUFs, and stop or
    # start inference without going through the Docker socket.
    local_ai_supervisor_url: str = "http://local-ai:8081"
    # Initial defaults for the supervisor — only consulted on first boot
    # of the local-ai container. After an admin saves config in the UI
    # the supervisor's persisted state in /cache/supervisor-state.json
    # takes precedence on subsequent boots.
    local_ai_hf_repo: str = ""
    local_ai_hf_file: str = ""
    # Friendly model alias returned by /v1/models. Must match the OpenAI
    # SDK ``model=`` parameter on the backend side.
    local_ai_model: str = "local-llm"
    # llama-server doesn't authenticate; the OpenAI SDK still demands a
    # non-empty value.
    local_ai_api_key: str = "dummy"
    # KV cache context window in tokens.
    local_ai_ctx_size: int = 16384
    # Enable chain-of-thought ("reasoning_content") for local models that
    # support it (Qwen 3.5/3.6, DeepSeek-R1-distilled, etc.). Reasoning gives
    # noticeably better analysis quality at the cost of inference time
    # (~30-60s vs ~10s) and ~5-8k extra completion tokens. The flag is sent
    # as ``chat_template_kwargs.enable_thinking`` and silently ignored by
    # models that don't recognise it.
    local_ai_enable_thinking: bool = True

    # --- SMTP ---
    smtp_host: str = "smtp.local"
    smtp_port: int = 25
    smtp_use_tls: bool = False
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: EmailStr = "wowanalyzer@example.com"
    smtp_from_name: str = "WoW AI Log Analyzer"

    # --- Top-Logs daily fetcher ---
    # Default: Wednesday 08:00 UTC = ~1-3h after the EU weekly reset (Wed 07:00
    # CET/CEST). Daily would burn the WCL rate budget for little benefit since
    # the leaderboard barely moves intra-week.
    top_logs_cron: str = "0 8 * * 3"
    top_logs_limit: int = 25
    # Region filter for the WCL world rankings ("EU", "US", "KR", or "" for all).
    top_logs_region: str = "EU"
    # Mythic raid difficulty (5 = Mythic, 4 = Heroic, 3 = Normal).
    top_logs_raid_difficulty: int = 5
    # For HPS rankings, drop logs with fewer than this many healers (filters out
    # log-runs / low-healer comps that inflate single-healer HPS unrealistically).
    top_logs_min_healers: int = 4
    # How many top performers per (spec, encounter, metric) we fetch *detail*
    # data for (casts, gear, buffs, talents, damage taken). Less than top_logs_limit
    # because detailed fetch is expensive.
    top_logs_detail_count: int = 5

    # --- Initial admin ---
    initial_admin_email: EmailStr = "admin@example.com"
    initial_admin_password: str = "changeme-at-first-login"

    # --- Feature flags ---
    allow_registration: bool = True

    # --- Captcha (Cloudflare Turnstile, optional) ---
    # Enable Turnstile checks on login, register, forgot-password and accept-
    # invite endpoints. When False the backend skips verification entirely; the
    # frontend reads the flag from /api/v1/config and skips rendering the
    # widget. When True, set both ``TURNSTILE_SECRET_KEY`` (here, kept on
    # the server) and ``TURNSTILE_SITE_KEY`` (delivered to the browser via
    # the same /api/v1/config endpoint).
    turnstile_enabled: bool = False
    turnstile_secret_key: str = ""

    # --- Admin Docker control (optional, opt-in) ---
    # When True the admin UI exposes a "System" card that lists the compose
    # stack containers and lets admins start/stop/restart them — and the
    # local-ai container is auto-started/stopped when ai_provider toggles.
    # Requires ``/var/run/docker.sock`` mounted into the backend container.
    # Security: equivalent to root on the host. Only enable on single-tenant
    # self-hosted instances where the admin is also the host operator.
    admin_docker_control: bool = False
    # Compose project name; defaults to "wow-ai-log-analyzer" (matches the
    # ``name:`` directive in docker-compose.yml). Used to filter the list of
    # visible containers so we never expose containers from unrelated projects.
    docker_compose_project: str = "wow-ai-log-analyzer"

    # ---- derived ----
    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[misc]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @computed_field  # type: ignore[misc]
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# Well-known weak defaults shipped with .env.example. Boot-time guard refuses
# to start the production app if any of these slipped through into the
# active settings — preserving them as defaults keeps dev/test ergonomics
# (uv run / pytest without .env) but catches a "forgot to fill .env" boot
# in production where the consequences are JWT-forging + Fernet-decryption.
_INSECURE_DEFAULTS = {
    "secret_key": {"insecure-dev-key"},
    "postgres_password": {"change-me"},
    "initial_admin_password": {"changeme-at-first-login"},
}


def assert_production_secrets() -> None:
    """Raise RuntimeError when the active settings still hold any of the
    well-known weak defaults from ``.env.example`` and ``app_env`` is
    ``production``. Called from the FastAPI lifespan so misconfigured
    deployments fail loudly at boot rather than silently accepting JWTs
    signed with the public-on-GitHub default key.
    """
    if settings.app_env != "production":
        return
    bad: list[str] = []
    for field, weak in _INSECURE_DEFAULTS.items():
        if getattr(settings, field) in weak:
            bad.append(field.upper())
    # Also catch the case where someone copies the example value but only
    # the prefix matched a long random string — accept anything ≥32 chars
    # for SECRET_KEY (HS256 + Fernet derivation both want >=128 bit entropy).
    if len(settings.secret_key) < 32:
        bad.append("SECRET_KEY (must be >=32 chars; generate with `openssl rand -hex 64`)")
    if bad:
        raise RuntimeError(
            "Refusing to boot in production with insecure default values for: "
            + ", ".join(bad)
            + ". Set strong values in .env (or set APP_ENV=development for "
            "local testing)."
        )
