"use client";

import { KeyRound, Lock, Mail, User } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Suspense, use, useEffect, useState } from "react";

import { AuthShell } from "@/components/AuthShell";
import { TurnstileWidget, useTurnstileEnabled } from "@/components/TurnstileWidget";
import { Button, Card, FieldError, Input, Label } from "@/components/ui";
import { ApiClientError, apiFetch } from "@/lib/api";
import { setTokens } from "@/lib/auth";
import type { Locale } from "@/i18n/config";
import type { PublicConfig, TokenPair } from "@/types/api";

export default function RegisterPage({ params }: { params: Promise<{ locale: Locale }> }) {
  return (
    <Suspense fallback={null}>
      <RegisterPageInner params={params} />
    </Suspense>
  );
}

function RegisterPageInner({ params }: { params: Promise<{ locale: Locale }> }) {
  const { locale } = use(params);
  const t = useTranslations();
  const router = useRouter();
  const search = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [inviteToken, setInviteToken] = useState(search.get("token") ?? "");
  const [captchaToken, setCaptchaToken] = useState<string | null>(null);
  // Bump on submit-error → remounts TurnstileWidget for a fresh token.
  const [captchaResetKey, setCaptchaResetKey] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [allowOpenReg, setAllowOpenReg] = useState<boolean | null>(null);
  const captchaRequired = useTurnstileEnabled();

  useEffect(() => {
    apiFetch<PublicConfig>("/api/v1/config", { anonymous: true })
      .then((cfg) => setAllowOpenReg(cfg.allow_registration))
      .catch(() => setAllowOpenReg(false));
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (password.length < 8) {
      setErr(t("auth.passwordsTooShort"));
      return;
    }
    if (captchaRequired === true && !captchaToken) {
      setErr(t("auth.captchaMissing"));
      return;
    }
    setLoading(true);
    try {
      const tokens = await apiFetch<TokenPair>("/api/v1/auth/register", {
        method: "POST",
        anonymous: true,
        body: {
          email,
          password,
          display_name: displayName,
          invite_token: inviteToken || undefined,
          // Active UI locale becomes the account's preferred language —
          // a user registering on /de shouldn't have to flip their
          // profile to German afterwards.
          locale,
          captcha_token: captchaToken,
        },
      });
      setTokens(tokens);
      router.push(`/${locale}/analyze`);
    } catch (e) {
      setErr(e instanceof ApiClientError ? e.message : t("errors.generic"));
      setCaptchaToken(null);
      setCaptchaResetKey((k) => k + 1);
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthShell>
      <Card>
        <h1 className="mb-4 font-display text-2xl font-semibold">{t("auth.registerTitle")}</h1>
        {allowOpenReg === false && (
          <p className="mb-4 rounded bg-yellow-500/10 p-3 text-sm text-yellow-300">
            {t("auth.registrationDisabled")}
          </p>
        )}
        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <Label htmlFor="display_name">{t("auth.displayName")}</Label>
            <Input
              id="display_name"
              required
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              leftIcon={<User className="h-4 w-4" aria-hidden="true" />}
            />
          </div>
          <div>
            <Label htmlFor="email">{t("auth.email")}</Label>
            <Input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              leftIcon={<Mail className="h-4 w-4" aria-hidden="true" />}
            />
          </div>
          <div>
            <Label htmlFor="password">{t("auth.password")}</Label>
            <Input
              id="password"
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              leftIcon={<Lock className="h-4 w-4" aria-hidden="true" />}
            />
          </div>
          <div>
            <Label htmlFor="invite">{t("auth.inviteRequired")}</Label>
            <Input
              id="invite"
              value={inviteToken}
              onChange={(e) => setInviteToken(e.target.value)}
              placeholder="token..."
              leftIcon={<KeyRound className="h-4 w-4" aria-hidden="true" />}
            />
          </div>
          <TurnstileWidget key={captchaResetKey} onToken={setCaptchaToken} />
          <FieldError>{err}</FieldError>
          <Button type="submit" disabled={loading} className="w-full">
            {loading ? t("common.loading") : t("auth.registerTitle")}
          </Button>
        </form>
        <p className="mt-4 text-sm text-zinc-400">
          {t("auth.haveAccount")} <Link href={`/${locale}/login`}>{t("nav.login")}</Link>
        </p>
      </Card>
    </AuthShell>
  );
}
