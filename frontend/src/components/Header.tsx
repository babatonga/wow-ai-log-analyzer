"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { clearAuth, getAccessToken, getCachedUser, setCachedUser } from "@/lib/auth";
import { Button, Select } from "@/components/ui";
import type { Locale } from "@/i18n/config";
import { LOCALES } from "@/i18n/config";
import type { UserOut } from "@/types/api";

export function Header({ locale }: { locale: Locale }) {
  const t = useTranslations();
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<UserOut | null>(null);

  useEffect(() => {
    let cancelled = false;
    const cached = getCachedUser();
    if (cached) setUser(cached);
    // Logged out (no access token)? Skip the /users/me probe entirely —
    // the request would just 401, and browsers log every failed request
    // to the console regardless of our catch handler. Still clear any
    // stale cached user so the UI can't show a ghost session.
    if (!getAccessToken()) {
      setUser(null);
      clearAuth();
      return () => {
        cancelled = true;
      };
    }
    apiFetch<UserOut>("/api/v1/users/me")
      .then((u) => {
        if (cancelled) return;
        setUser(u);
        setCachedUser(u);
      })
      .catch(() => {
        if (cancelled) return;
        setUser(null);
        clearAuth();
      });
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  const switchLocale = (next: string) => {
    if (!LOCALES.includes(next as Locale)) return;
    const segments = pathname.split("/");
    if (segments.length > 1) segments[1] = next;
    router.push(segments.join("/") || `/${next}`);
  };

  const logout = () => {
    clearAuth();
    setUser(null);
    router.push(`/${locale}/login`);
  };

  return (
    <header className="sticky top-0 z-30 border-b border-bg-3 bg-bg-0/80 backdrop-blur">
      <div className="container-page flex flex-wrap items-center justify-between gap-x-4 gap-y-2 !py-3 sm:!py-4">
        <Link
          href={`/${locale}`}
          className="group flex items-center gap-2.5 text-zinc-100 no-underline"
        >
          <img
            src="/brand/mark-128.png"
            alt=""
            aria-hidden="true"
            className="h-8 w-8 shrink-0 transition-[filter] duration-300 group-hover:drop-shadow-[0_0_8px_rgba(245,158,11,0.55)]"
          />
          <span className="hidden font-display text-lg font-semibold tracking-wide text-accent sm:inline">
            {t("app.name")}
          </span>
        </Link>
        <nav className="flex flex-wrap items-center justify-end gap-x-1 gap-y-1">
          {user ? (
            <>
              <Link href={`/${locale}/analyze`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                {t("nav.analyze")}
              </Link>
              <Link href={`/${locale}/simulate`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                {t("nav.simulate")}
              </Link>
              <Link href={`/${locale}/top-logs`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                {t("nav.topLogs")}
              </Link>
              {user.role === "admin" && (
                <Link href={`/${locale}/admin`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                  {t("nav.admin")}
                </Link>
              )}
              <Link href={`/${locale}/profile`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                {t("nav.profile")}
              </Link>
            </>
          ) : (
            <>
              <Link href={`/${locale}/login`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                {t("nav.login")}
              </Link>
              <Link href={`/${locale}/register`} className="px-2 py-1.5 text-sm text-zinc-200 no-underline hover:text-accent sm:px-3">
                {t("nav.register")}
              </Link>
            </>
          )}
          <Select
            value={locale}
            onChange={(e) => switchLocale(e.target.value)}
            aria-label={t("common.language")}
            className="!w-20 sm:!w-28"
          >
            <option value="en">{t("common.english")}</option>
            <option value="de">{t("common.german")}</option>
          </Select>
          {user && (
            <Button variant="ghost" size="sm" onClick={logout}>
              {t("nav.logout")}
            </Button>
          )}
        </nav>
      </div>
    </header>
  );
}
