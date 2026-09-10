"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { Button, Card, FieldError, Input, Label, Select } from "@/components/ui";
import { ApiClientError, apiFetch } from "@/lib/api";
import type {
  AiProviderType,
  ReasoningEffort,
  UserAiConfig,
  UserAiConfigInput,
  UserAiConfigTestResult,
} from "@/types/api";

// Default model name presets per provider — picked from the most common
// public choices so the user has something working out of the box. The
// input is free-text so users can override with whatever model id their
// provider supports (e.g. ``gpt-5.4-nano``, ``o3-mini``, …).
const DEFAULT_MODELS: Record<AiProviderType, string> = {
  anthropic: "claude-sonnet-4-6",
  openai: "gpt-5.4-mini",
  openai_compatible: "",
};

export function UserAiConfigPanel() {
  const t = useTranslations();
  const qc = useQueryClient();
  const [providerType, setProviderType] = useState<AiProviderType>("anthropic");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState(DEFAULT_MODELS.anthropic);
  const [apiKey, setApiKey] = useState("");
  const [label, setLabel] = useState("");
  // "" = use OpenAI default (no reasoning); rest map to GPT-5/o-series
  // ``reasoning_effort`` values. Only sent for openai* providers.
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | "">("");
  // "" = inherit the app-wide default; "on"/"off" force the thinking
  // toggle for the user's own OpenAI-compatible server.
  const [enableThinking, setEnableThinking] = useState<"" | "on" | "off">("");
  // "" = app-wide AI_MAX_TOKENS default.
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);
  const [testResult, setTestResult] = useState<UserAiConfigTestResult | null>(null);

  const cfgQ = useQuery({
    queryKey: ["my-ai-config"],
    queryFn: () => apiFetch<UserAiConfig | null>("/api/v1/users/me/ai-config"),
  });

  // Hydrate the form from the existing config exactly once after the query
  // first lands. Subsequent edits stay local until the user saves.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    if (hydrated || cfgQ.data == null) return;
    setProviderType(cfgQ.data.provider_type);
    setBaseUrl(cfgQ.data.base_url ?? "");
    setModel(cfgQ.data.model);
    setLabel(cfgQ.data.label);
    setReasoningEffort(cfgQ.data.reasoning_effort ?? "");
    setEnableThinking(
      cfgQ.data.enable_thinking == null ? "" : cfgQ.data.enable_thinking ? "on" : "off",
    );
    setMaxOutputTokens(
      cfgQ.data.max_output_tokens != null ? String(cfgQ.data.max_output_tokens) : "",
    );
    setHydrated(true);
  }, [cfgQ.data, hydrated]);

  const saveMut = useMutation({
    mutationFn: (body: UserAiConfigInput) =>
      apiFetch<UserAiConfig>("/api/v1/users/me/ai-config", {
        method: "PUT",
        body,
      }),
    onSuccess: () => {
      setErr(null);
      setSavedFlash(true);
      setApiKey("");
      qc.invalidateQueries({ queryKey: ["my-ai-config"] });
      window.setTimeout(() => setSavedFlash(false), 3000);
    },
    onError: (e) => {
      setErr(e instanceof ApiClientError ? e.message : t("errors.generic"));
    },
  });

  const testMut = useMutation({
    mutationFn: (body: UserAiConfigInput) =>
      apiFetch<UserAiConfigTestResult>("/api/v1/users/me/ai-config/test", {
        method: "POST",
        body,
      }),
    onSuccess: (result) => {
      setTestResult(result);
      setErr(null);
    },
    onError: (e) => {
      setTestResult(null);
      setErr(e instanceof ApiClientError ? e.message : t("errors.generic"));
    },
  });

  const deleteMut = useMutation({
    mutationFn: () =>
      apiFetch("/api/v1/users/me/ai-config", { method: "DELETE" }),
    onSuccess: () => {
      setHydrated(false);
      setProviderType("anthropic");
      setBaseUrl("");
      setModel(DEFAULT_MODELS.anthropic);
      setApiKey("");
      setLabel("");
      setReasoningEffort("");
      setEnableThinking("");
      setMaxOutputTokens("");
      setTestResult(null);
      setErr(null);
      qc.invalidateQueries({ queryKey: ["my-ai-config"] });
    },
  });

  const switchProvider = (next: AiProviderType) => {
    setProviderType(next);
    if (!hydrated || (cfgQ.data && cfgQ.data.provider_type !== next)) {
      setModel(DEFAULT_MODELS[next] || model);
    }
  };

  const buildBody = (): UserAiConfigInput | null => {
    if (!model.trim()) {
      setErr(t("profile.aiCfg.errModelRequired"));
      return null;
    }
    if (!apiKey.trim() && !cfgQ.data) {
      setErr(t("profile.aiCfg.errKeyRequired"));
      return null;
    }
    if (
      providerType === "openai_compatible" &&
      !(baseUrl.trim().startsWith("http://") || baseUrl.trim().startsWith("https://"))
    ) {
      setErr(t("profile.aiCfg.errBaseUrlRequired"));
      return null;
    }
    setErr(null);
    return {
      provider_type: providerType,
      base_url: baseUrl.trim() || undefined,
      model: model.trim(),
      // When editing an existing config without re-entering the key, send
      // a sentinel so the backend keeps the existing one. Pragmatic: we
      // require re-entering — simplest approach.
      api_key: apiKey.trim(),
      label: label.trim() || undefined,
      // openai: GPT-5/o-series ``reasoning_effort``. anthropic: maps to
      // Claude's adaptive thinking + effort in the backend. Empty → null
      // = provider default (no explicit reasoning).
      reasoning_effort: reasoningEffort || null,
      // openai_compatible only: overrides the app-wide thinking toggle
      // for the user's own server. null = inherit.
      enable_thinking:
        enableThinking === "" ? null : enableThinking === "on",
      // null = app-wide AI_MAX_TOKENS default.
      max_output_tokens: maxOutputTokens.trim()
        ? Number(maxOutputTokens.trim())
        : null,
    };
  };

  const isConfigured = !!cfgQ.data;
  const showBaseUrl = providerType !== "anthropic";

  return (
    <Card>
      <h2 className="text-lg font-semibold">{t("profile.aiCfg.title")}</h2>
      <p className="mt-1 text-sm text-zinc-400">{t("profile.aiCfg.intro")}</p>

      <div className="mt-4 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-200">
        <p className="font-semibold">{t("profile.aiCfg.disclaimerTitle")}</p>
        <p className="mt-1 whitespace-pre-line text-amber-100/90">
          {t("profile.aiCfg.disclaimerBody")}
        </p>
      </div>

      {isConfigured && (
        <p className="mt-4 text-xs text-emerald-300">
          {t("profile.aiCfg.currentlyConfigured", {
            provider: cfgQ.data!.provider_type,
            model: cfgQ.data!.model,
            key: cfgQ.data!.api_key_masked,
          })}
        </p>
      )}

      <form
        className="mt-4 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          const body = buildBody();
          if (body) saveMut.mutate(body);
        }}
      >
        <div>
          <Label>{t("profile.aiCfg.provider")}</Label>
          <Select
            value={providerType}
            onChange={(e) => switchProvider(e.target.value as AiProviderType)}
          >
            <option value="anthropic">{t("profile.aiCfg.providerAnthropic")}</option>
            <option value="openai">{t("profile.aiCfg.providerOpenai")}</option>
            <option value="openai_compatible">
              {t("profile.aiCfg.providerOpenaiCompatible")}
            </option>
          </Select>
          <p className="mt-1 text-xs text-zinc-500">
            {providerType === "openai_compatible"
              ? t("profile.aiCfg.providerCompatibleHint")
              : providerType === "openai"
                ? t("profile.aiCfg.providerOpenaiHint")
                : t("profile.aiCfg.providerAnthropicHint")}
          </p>
        </div>

        {showBaseUrl && (
          <div>
            <Label>{t("profile.aiCfg.baseUrl")}</Label>
            <Input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={
                providerType === "openai"
                  ? "https://api.openai.com/v1"
                  : "https://your-server/v1"
              }
            />
            <p className="mt-1 text-xs text-zinc-500">
              {providerType === "openai"
                ? t("profile.aiCfg.baseUrlOpenaiHint")
                : t("profile.aiCfg.baseUrlCompatibleHint")}
            </p>
          </div>
        )}

        <div>
          <Label>{t("profile.aiCfg.model")}</Label>
          <Input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={DEFAULT_MODELS[providerType] || "model name"}
            required
          />
        </div>

        <div>
          <Label>{t("profile.aiCfg.apiKey")}</Label>
          <Input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            autoComplete="off"
            placeholder={
              isConfigured
                ? t("profile.aiCfg.apiKeyEditPlaceholder")
                : t("profile.aiCfg.apiKeyPlaceholder")
            }
          />
          <p className="mt-1 text-xs text-zinc-500">
            {t("profile.aiCfg.apiKeyHint")}
          </p>
        </div>

        <div>
          <Label>{t("profile.aiCfg.label")}</Label>
          <Input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={t("profile.aiCfg.labelPlaceholder")}
          />
        </div>

        {providerType !== "openai_compatible" && (
          // OpenAI cloud: maps to the ``reasoning_effort`` parameter
          // (GPT-5 / o-series). Anthropic: maps to Claude's adaptive
          // thinking + ``output_config.effort`` (minimal→low) on Claude
          // 4.6+/Fable models. Self-hosted ``openai_compatible`` uses
          // the thinking toggle below instead — their servers have no
          // effort parameter.
          <div>
            <Label>{t("profile.aiCfg.reasoningEffort")}</Label>
            <Select
              value={reasoningEffort}
              onChange={(e) =>
                setReasoningEffort(e.target.value as ReasoningEffort | "")
              }
            >
              <option value="">{t("profile.aiCfg.reasoningEffortOff")}</option>
              <option value="minimal">minimal</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </Select>
            <p className="mt-1 text-xs text-zinc-500">
              {providerType === "anthropic"
                ? t("profile.aiCfg.reasoningEffortHintAnthropic")
                : t("profile.aiCfg.reasoningEffortHint")}
            </p>
          </div>
        )}

        {providerType === "openai_compatible" && (
          // Self-hosted servers control their own context window — the app
          // cannot raise it per request (verified: Ollama's OpenAI endpoint
          // ignores options.num_ctx). Without ≥128k ctx every analysis
          // fails, so surface this loudly before the user hits the error.
          <div className="rounded-md border border-sky-500/30 bg-sky-500/5 p-3 text-xs text-sky-200">
            <p className="font-semibold">{t("profile.aiCfg.contextHintTitle")}</p>
            <p className="mt-1 whitespace-pre-line text-sky-100/90">
              {t("profile.aiCfg.contextHintBody")}
            </p>
          </div>
        )}

        {providerType === "openai_compatible" && (
          // Per-user override of the server-wide thinking toggle
          // (llama.cpp/vLLM ``chat_template_kwargs.enable_thinking``).
          <div>
            <Label>{t("profile.aiCfg.enableThinking")}</Label>
            <Select
              value={enableThinking}
              onChange={(e) =>
                setEnableThinking(e.target.value as "" | "on" | "off")
              }
            >
              <option value="">{t("profile.aiCfg.enableThinkingInherit")}</option>
              <option value="on">{t("profile.aiCfg.enableThinkingOn")}</option>
              <option value="off">{t("profile.aiCfg.enableThinkingOff")}</option>
            </Select>
            <p className="mt-1 text-xs text-zinc-500">
              {t("profile.aiCfg.enableThinkingHint")}
            </p>
          </div>
        )}

        <div>
          <Label>{t("profile.aiCfg.maxTokens")}</Label>
          <Input
            type="number"
            min={1000}
            max={128000}
            step={1000}
            value={maxOutputTokens}
            onChange={(e) => setMaxOutputTokens(e.target.value)}
            placeholder={t("profile.aiCfg.maxTokensPlaceholder")}
          />
          <p className="mt-1 text-xs text-zinc-500">
            {t("profile.aiCfg.maxTokensHint")}
          </p>
        </div>

        <FieldError>{err}</FieldError>

        {testResult && (
          <div
            className={`flex items-start gap-2 rounded border p-2 text-xs ${
              testResult.ok
                ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-200"
                : "border-red-500/30 bg-red-500/5 text-red-200"
            }`}
          >
            {testResult.ok ? (
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            ) : (
              <XCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            )}
            <div>
              <p className="font-semibold">
                {testResult.ok
                  ? t("profile.aiCfg.testOk", { ms: testResult.latency_ms ?? "?" })
                  : t("profile.aiCfg.testFail")}
              </p>
              <p className="mt-0.5 break-words">{testResult.detail}</p>
            </div>
          </div>
        )}

        {savedFlash && (
          <p className="text-xs text-emerald-300">{t("profile.aiCfg.saved")}</p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={saveMut.isPending}>
            {saveMut.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            {t("profile.aiCfg.save")}
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => {
              const body = buildBody();
              if (body) testMut.mutate(body);
            }}
            disabled={testMut.isPending}
            className="inline-flex items-center gap-2"
          >
            {testMut.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            {t("profile.aiCfg.test")}
          </Button>
          {isConfigured && (
            <Button
              type="button"
              variant="danger"
              onClick={() => {
                if (window.confirm(t("profile.aiCfg.deleteConfirm"))) {
                  deleteMut.mutate();
                }
              }}
              disabled={deleteMut.isPending}
            >
              {t("profile.aiCfg.delete")}
            </Button>
          )}
        </div>
      </form>
    </Card>
  );
}
