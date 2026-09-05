"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { clearAuth, getAccessToken, setCachedUser } from "@/lib/auth";
import type { Locale } from "@/i18n/config";
import type { UserOut } from "@/types/api";

interface Props {
  locale: Locale;
  requireAdmin?: boolean;
  children: (user: UserOut) => React.ReactNode;
}

export function AuthGuard({ locale, requireAdmin, children }: Props) {
  const router = useRouter();
  const [user, setUser] = useState<UserOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // No token → straight to login without firing a request that would
    // predictably 401 (and get logged to the browser console).
    if (!getAccessToken()) {
      clearAuth();
      router.replace(`/${locale}/login`);
      return;
    }
    apiFetch<UserOut>("/api/v1/users/me")
      .then((u) => {
        if (requireAdmin && u.role !== "admin") {
          router.replace(`/${locale}`);
          return;
        }
        setUser(u);
        setCachedUser(u);
      })
      .catch(() => {
        clearAuth();
        router.replace(`/${locale}/login`);
      });
  }, [locale, requireAdmin, router]);

  if (error) return <p className="container-page text-red-400">{error}</p>;
  if (!user) return <p className="container-page text-zinc-400">…</p>;
  return <>{children(user)}</>;
}
