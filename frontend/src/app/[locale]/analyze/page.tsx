"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HeartPulse, Loader2, Search, Shield, Sword, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { use, useEffect, useRef, useState } from "react";

import { AnalysisCard } from "@/components/AnalysisCard";
import { AnalysisShareControls } from "@/components/AnalysisShareControls";
import { AuthGuard } from "@/components/AuthGuard";
import { ClassBadge } from "@/components/ClassBadge";
import { EmptyState } from "@/components/EmptyState";
import { Button, Card, FieldError, Input } from "@/components/ui";
import { ApiClientError, apiFetch } from "@/lib/api";
import { formatDateTime, formatDuration, formatNumber } from "@/lib/format";
import { formatParsePercent, parseColorFor } from "@/lib/parsePercent";
import type { Locale } from "@/i18n/config";
import type {
  Analysis,
  AnalysisListItem,
  GameClass,
  PaginatedAnalyses,
  PaginatedReports,
  PublicConfig,
  Report,
  ReportFight,
  ReportPlayer,
  UserAiConfig,
} from "@/types/api";

export default function AnalyzePage({ params }: { params: Promise<{ locale: Locale }> }) {
  const { locale } = use(params);
  return <AuthGuard locale={locale}>{() => <AnalyzeView locale={locale} />}</AuthGuard>;
}

function AnalyzeView({ locale }: { locale: Locale }) {
  const t = useTranslations();
  const qc = useQueryClient();
  const [input, setInput] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [activeReportId, setActiveReportId] = useState<string | null>(null);
  // When the user clicks an entry from "Recent analyses", we render the full
  // AnalysisCard for it without disturbing the report-importer flow.
  const [historicalAnalysisId, setHistoricalAnalysisId] = useState<string | null>(null);

  const REPORTS_PAGE_SIZE = 5;
  const [reportsPage, setReportsPage] = useState(1);

  const myReportsQ = useQuery({
    queryKey: ["my-reports", reportsPage],
    queryFn: () =>
      apiFetch<PaginatedReports>(
        `/api/v1/reports?page=${reportsPage}&page_size=${REPORTS_PAGE_SIZE}`,
      ),
    // Keep refreshing the list while at least one report is still being
    // imported, so the inline pulse-dot disappears the moment it lands.
    refetchInterval: (q) =>
      q.state.data?.items.some((r) => r.import_status === "importing")
        ? 2500
        : false,
  });

  const deleteReportMut = useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/reports/${id}`, { method: "DELETE" }),
    onSuccess: (_d, id) => {
      if (activeReportId === id) setActiveReportId(null);
      qc.invalidateQueries({ queryKey: ["my-reports"] });
      // Reports cascade-delete their analyses; refresh the analyses list too.
      qc.invalidateQueries({ queryKey: ["my-analyses"] });
    },
  });

  const [analysesPage, setAnalysesPage] = useState(1);
  const [analysesQuery, setAnalysesQuery] = useState("");
  // Debounce the search input so we don't fire a request on every keystroke.
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const ANALYSES_PAGE_SIZE = 5;
  const myAnalysesQ = useQuery({
    queryKey: ["my-analyses", analysesPage, debouncedQuery],
    queryFn: () => {
      const params = new URLSearchParams({
        page: String(analysesPage),
        page_size: String(ANALYSES_PAGE_SIZE),
      });
      if (debouncedQuery) params.set("q", debouncedQuery);
      return apiFetch<PaginatedAnalyses>(`/api/v1/analyses?${params.toString()}`);
    },
    // Keep refreshing while at least one item is still being worked on by
    // the worker, so a queued/running entry flips to its final headline +
    // score on its own.
    refetchInterval: (q) =>
      q.state.data?.items.some(
        (a) => a.status === "pending" || a.status === "running",
      )
        ? 2500
        : false,
  });

  const deleteAnalysisMut = useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/api/v1/analyses/${id}`, { method: "DELETE" }),
    onSuccess: (_data, id) => {
      // If the deleted item was the one currently shown, clear the view.
      if (historicalAnalysisId === id) setHistoricalAnalysisId(null);
      qc.invalidateQueries({ queryKey: ["my-analyses"] });
    },
  });

  const historicalAnalysisQ = useQuery({
    queryKey: ["analysis", historicalAnalysisId],
    queryFn: () => apiFetch<Analysis>(`/api/v1/analyses/${historicalAnalysisId}`),
    enabled: !!historicalAnalysisId,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "pending" || s === "running" ? 2000 : false;
    },
  });

  // Debounce search → only refetch ~300ms after user stops typing.
  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedQuery(analysesQuery.trim());
      setAnalysesPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [analysesQuery]);

  const classesQ = useQuery({
    queryKey: ["classes"],
    queryFn: () => apiFetch<GameClass[]>("/api/v1/classes"),
  });

  const reportQ = useQuery({
    queryKey: ["report", activeReportId],
    queryFn: () => apiFetch<Report>(`/api/v1/reports/${activeReportId}`),
    enabled: !!activeReportId,
    // Poll every 2 s while the worker is still importing the report.
    // Stops automatically once import_status flips to ``ready`` / ``failed``.
    refetchInterval: (q) =>
      q.state.data?.import_status === "importing" ? 2000 : false,
  });

  // Scroll the report section into view so the user immediately sees the
  // import-progress card / fights table without scrolling past the recent-
  // reports list. The anchor sits just above the three status cards. We
  // call it from two places: the useEffect below (handles reports opened
  // via the recent-reports list) and the import mutation success (handles
  // re-imports of the same WCL code where activeReportId doesn't change).
  const reportAnchorRef = useRef<HTMLDivElement>(null);
  function scrollToReportSection() {
    // Small delay so the conditional Cards (importing/failed/ready) have a
    // chance to mount before the browser computes the scroll target.
    window.setTimeout(() => {
      reportAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  }

  const importMut = useMutation({
    mutationFn: (raw: string) =>
      apiFetch<Report>("/api/v1/reports/import", {
        method: "POST",
        body: { wcl_url_or_code: raw },
      }),
    onSuccess: (report) => {
      setErr(null);
      setActiveReportId(report.id);
      qc.invalidateQueries({ queryKey: ["my-reports"] });
      scrollToReportSection();
    },
    onError: (e) => setErr(e instanceof ApiClientError ? e.message : t("errors.generic")),
  });

  useEffect(() => {
    if (activeReportId) scrollToReportSection();
  }, [activeReportId]);

  // Disable the import button as long as either the HTTP request is in
  // flight OR the worker is still chewing on the active report.
  const importInFlight =
    importMut.isPending || reportQ.data?.import_status === "importing";

  return (
    <div className="container-page space-y-6">
      <header>
        <h1 className="font-display text-3xl font-semibold">{t("analyze.title")}</h1>
        <p className="mt-1 max-w-2xl text-sm text-zinc-400">{t("analyze.subtitle")}</p>
      </header>

      <Card>
        <form
          className="flex flex-col gap-3 md:flex-row"
          onSubmit={(e) => {
            e.preventDefault();
            if (!input.trim()) return;
            importMut.mutate(input.trim());
          }}
        >
          <Input
            placeholder={t("analyze.input")}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            className="md:flex-1"
          />
          <Button
            type="submit"
            disabled={importInFlight}
            className="inline-flex items-center gap-2"
          >
            {importInFlight && (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            )}
            {importInFlight
              ? t("analyze.importRunning")
              : t("analyze.import")}
          </Button>
        </form>
        <FieldError>{err}</FieldError>
      </Card>

      <RecentAnalysesPanel
        page={myAnalysesQ.data}
        classes={classesQ.data ?? []}
        locale={locale}
        loading={myAnalysesQ.isLoading}
        activeId={historicalAnalysisId}
        onSelect={setHistoricalAnalysisId}
        query={analysesQuery}
        onQueryChange={setAnalysesQuery}
        currentPage={analysesPage}
        pageSize={ANALYSES_PAGE_SIZE}
        onPageChange={setAnalysesPage}
        onDelete={(id) => {
          if (window.confirm(t("analyze.confirmDeleteAnalysis"))) {
            deleteAnalysisMut.mutate(id);
          }
        }}
        deleting={deleteAnalysisMut.isPending ? deleteAnalysisMut.variables ?? null : null}
      />

      {historicalAnalysisId && historicalAnalysisQ.data && (
        <>
          {historicalAnalysisQ.data.status === "succeeded" && (
            <AnalysisShareControls
              analysis={historicalAnalysisQ.data}
              locale={locale}
            />
          )}
          <AnalysisCard analysis={historicalAnalysisQ.data} locale={locale} />
        </>
      )}

      {myReportsQ.data && myReportsQ.data.items.length > 0 && (
        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-400">
            {t("analyze.yourRecentReports")}
          </h2>
          <ul className="divide-y divide-bg-3">
            {myReportsQ.data.items.map((r) => {
              const isDeleting = deleteReportMut.isPending && deleteReportMut.variables === r.id;
              return (
                <li
                  key={r.id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-md py-2 transition-colors hover:bg-bg-2/60"
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-zinc-100">
                      {r.title || r.zone_name || r.wcl_code}
                      {r.import_status === "importing" && (
                        <span className="ml-2 inline-block h-2 w-2 animate-pulse rounded-full bg-accent" />
                      )}
                      {r.import_status === "failed" && (
                        <span className="ml-2 text-xs font-medium text-red-400">
                          {t("analyze.importFailed")}
                        </span>
                      )}
                    </p>
                    <p className="text-xs text-zinc-500">
                      {r.zone_name}
                      {r.zone_name && r.start_time && " · "}
                      {r.start_time ? formatDateTime(r.start_time, locale) : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setActiveReportId(r.id)}
                    >
                      {t("analyze.openReport")}
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => {
                        if (window.confirm(t("analyze.confirmDeleteReport"))) {
                          deleteReportMut.mutate(r.id);
                        }
                      }}
                      disabled={isDeleting}
                      aria-label={t("analyze.deleteReport")}
                      title={t("analyze.deleteReport")}
                    >
                      {isDeleting ? "…" : <Trash2 className="h-4 w-4" aria-hidden="true" />}
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
          {(() => {
            const total = myReportsQ.data?.total ?? 0;
            const totalPages = Math.max(1, Math.ceil(total / REPORTS_PAGE_SIZE));
            if (total <= REPORTS_PAGE_SIZE) return null;
            return (
              <div className="mt-4 flex items-center justify-between text-xs text-zinc-400">
                <span>
                  {t("analyze.pageOf", { page: reportsPage, total: totalPages })}
                </span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={reportsPage <= 1}
                    onClick={() => setReportsPage(Math.max(1, reportsPage - 1))}
                  >
                    ← {t("analyze.previousPage")}
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={reportsPage >= totalPages}
                    onClick={() =>
                      setReportsPage(Math.min(totalPages, reportsPage + 1))
                    }
                  >
                    {t("analyze.nextPage")} →
                  </Button>
                </div>
              </div>
            );
          })()}
        </Card>
      )}

      {/* scroll-anchor — ``activeReportId`` change or import-trigger calls
          scrollToReportSection() which targets this div. ``scroll-mt-24``
          leaves ~6rem of breathing room under the sticky header. */}
      <div ref={reportAnchorRef} className="scroll-mt-24" />

      {reportQ.data?.import_status === "importing" && (
        <Card>
          <div className="flex items-center gap-3">
            <Loader2 className="h-5 w-5 shrink-0 animate-spin text-accent" aria-hidden="true" />
            <div>
              <p className="font-semibold">{t("analyze.importRunning")}</p>
              <p className="text-xs text-zinc-500">{t("analyze.importRunningHint")}</p>
            </div>
          </div>
        </Card>
      )}
      {reportQ.data?.import_status === "failed" && (
        <Card className="border-red-500/30">
          <p className="font-semibold text-red-300">{t("analyze.importFailed")}</p>
          {reportQ.data.import_error && (
            <pre className="mt-2 whitespace-pre-wrap text-xs text-zinc-400">
              {reportQ.data.import_error}
            </pre>
          )}
        </Card>
      )}
      {reportQ.data?.import_status === "ready" && (
        <ReportView
          report={reportQ.data}
          classes={classesQ.data ?? []}
          locale={locale}
        />
      )}
    </div>
  );
}

function ReportView({
  report,
  classes,
  locale,
}: {
  report: Report;
  classes: GameClass[];
  locale: Locale;
}) {
  const [fightId, setFightId] = useState<string | null>(report.fights[0]?.id ?? null);
  const fight = report.fights.find((f) => f.id === fightId) ?? report.fights[0];
  return (
    <div className="space-y-4">
      <Card>
        <header className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">{report.title || report.zone_name}</h2>
            <p className="text-xs text-zinc-500">
              <a
                href={`https://www.warcraftlogs.com/reports/${report.wcl_code}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                warcraftlogs.com/reports/{report.wcl_code}
              </a>
            </p>
          </div>
          <span className="text-xs text-zinc-500">{report.region.toUpperCase() || "—"}</span>
        </header>
        <div className="mt-4 flex flex-wrap gap-2">
          {report.fights
            // Skip "no progress" pulls (boss reset before engagement
            // started). They have no metric data attached and clutter
            // the picker. M+ runs always pass through (keystone_level
            // set even on depleted runs).
            .filter(
              (f) =>
                f.is_kill ||
                f.boss_percentage !== null ||
                f.keystone_level !== null,
            )
            .map((f) => (
              <button
                key={f.id}
                onClick={() => setFightId(f.id)}
                className={`rounded-md border px-3 py-1.5 text-xs ${
                  f.id === fight?.id
                    ? "border-accent bg-accent/10 text-accent"
                    : "border-bg-3 bg-bg-2 text-zinc-200 hover:border-zinc-500"
                }`}
              >
                {f.name_localized || f.name}{" "}
                {f.is_kill ? (
                  <span className="ml-0.5 font-bold text-emerald-400">✓</span>
                ) : (
                  <span className="ml-0.5 text-zinc-500">
                    {f.boss_percentage !== null
                      ? `${f.boss_percentage?.toFixed(1)}%`
                      : ""}
                  </span>
                )}
                {f.keystone_level ? (
                  <span className="ml-0.5 text-zinc-500"> · +{f.keystone_level}</span>
                ) : null}
              </button>
            ))}
        </div>
      </Card>

      {fight && <PlayersTable fight={fight} classes={classes} locale={locale} reportId={report.id} />}
    </div>
  );
}

// Role-icon shown next to the class badge in the player table. Tank gets a
// shield (purely cosmetic — analyses still treat tanks as DPS via their
// damage metric). Healer gets a heart-pulse, DPS a sword.
function RoleIcon({ role }: { role: string }) {
  const cls = "h-3.5 w-3.5 shrink-0 text-zinc-400";
  if (role === "healer") return <HeartPulse className={cls} aria-hidden="true" />;
  if (role === "tank") return <Shield className={cls} aria-hidden="true" />;
  return <Sword className={cls} aria-hidden="true" />;
}

function ParseChip({ percent }: { percent: number | null | undefined }) {
  if (percent === null || percent === undefined) {
    return <span className="text-zinc-500">—</span>;
  }
  const colors = parseColorFor(percent);
  return (
    <span
      className="inline-block min-w-[2.25rem] rounded px-2 py-0.5 text-center text-xs font-semibold"
      style={{ backgroundColor: colors.background, color: colors.foreground }}
    >
      {formatParsePercent(percent)}
    </span>
  );
}

function PlayersTable({
  fight,
  classes,
  locale,
  reportId,
}: {
  fight: ReportFight;
  classes: GameClass[];
  locale: Locale;
  reportId: string;
}) {
  const t = useTranslations();
  const qc = useQueryClient();
  const [activePlayerId, setActivePlayerId] = useState<string | null>(null);
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

  // Decide whether the App-AI / Own-AI buttons are enabled.
  // Short staleTime so an admin who just toggled ``ai_provider=disabled``
  // sees the buttons disable on every other tab within ~10 s without
  // needing a hard refresh. Plus refetch-on-focus to cover Alt-Tab back.
  const publicCfgQ = useQuery({
    queryKey: ["public-config"],
    queryFn: () => apiFetch<PublicConfig>("/api/v1/config", { anonymous: true }),
    staleTime: 10 * 1000,
    refetchOnWindowFocus: true,
  });
  const ownAiQ = useQuery({
    queryKey: ["my-ai-config"],
    queryFn: () => apiFetch<UserAiConfig | null>("/api/v1/users/me/ai-config"),
    staleTime: 30 * 1000,
    refetchOnWindowFocus: true,
  });
  const appAiEnabled = publicCfgQ.data?.ai_enabled ?? true;
  const ownAiConfigured = !!ownAiQ.data;

  const analyzeMut = useMutation({
    mutationFn: ({ player, useOwnAi }: { player: ReportPlayer; useOwnAi: boolean }) =>
      apiFetch<Analysis>("/api/v1/analyses", {
        method: "POST",
        locale,
        body: {
          report_id: reportId,
          fight_id: fight.id,
          player_id: player.id,
          use_own_ai: useOwnAi,
        },
      }),
    onSuccess: (a) => {
      setAnalysisId(a.id);
      setAnalyzeError(null);
      qc.invalidateQueries({ queryKey: ["my-analyses"] });
      scrollToAnalysisSection();
    },
    onError: (e) => {
      setAnalysisId(null);
      if (e instanceof ApiClientError && e.code === "no_top_logs") {
        setAnalyzeError(t("analyze.noTopLogsErr"));
      } else {
        setAnalyzeError(
          e instanceof ApiClientError ? e.message : t("errors.generic"),
        );
      }
    },
  });

  const analysisQ = useQuery({
    queryKey: ["analysis", analysisId],
    queryFn: () => apiFetch<Analysis>(`/api/v1/analyses/${analysisId}`),
    enabled: !!analysisId,
    // Poll while the worker is still working on the analysis. Once a terminal
    // status (succeeded / failed) lands the interval returns false and the
    // query becomes static.
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "pending" || s === "running" ? 2000 : false;
    },
  });

  // The moment the active analysis flips to a terminal state, refresh the
  // "Recent Analyses" panel so it shows the final headline / score
  // immediately instead of waiting for the panel's own poll interval.
  useEffect(() => {
    const s = analysisQ.data?.status;
    if (s === "succeeded" || s === "failed") {
      qc.invalidateQueries({ queryKey: ["my-analyses"] });
    }
  }, [analysisQ.data?.status, qc]);

  // True from the moment the user clicks "Analyze" until the worker reports
  // a terminal status. Disables every analyze-button on the table so the
  // worker doesn't get spammed with parallel requests.
  const analysisInFlight =
    analyzeMut.isPending ||
    analysisQ.data?.status === "pending" ||
    analysisQ.data?.status === "running";

  // Auto-scroll to the analysis section the moment the user clicks
  // "Analyze". Same UX trick as the import button on the parent page.
  const analysisAnchorRef = useRef<HTMLDivElement>(null);
  function scrollToAnalysisSection() {
    window.setTimeout(() => {
      analysisAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  }

  function triggerAnalysis(player: ReportPlayer, useOwnAi: boolean) {
    setActivePlayerId(player.id);
    setAnalysisId(null);
    setAnalyzeError(null);
    analyzeMut.mutate({ player, useOwnAi });
  }

  function AnalyzeButtons({ player }: { player: ReportPlayer }) {
    const isMe = activePlayerId === player.id;
    const appBtnDisabled = analysisInFlight || !appAiEnabled;
    const ownBtnDisabled = analysisInFlight || !ownAiConfigured;
    return (
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        <Button
          size="sm"
          onClick={() => triggerAnalysis(player, false)}
          disabled={appBtnDisabled}
          className="inline-flex items-center gap-1.5"
          title={
            !appAiEnabled
              ? t("analyze.appAiDisabledTooltip")
              : t("analyze.appAiTooltip")
          }
        >
          {analysisInFlight && isMe ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Search className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {t("analyze.analyseAppAi")}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => triggerAnalysis(player, true)}
          disabled={ownBtnDisabled}
          className="inline-flex items-center gap-1.5"
          title={
            !ownAiConfigured
              ? t("analyze.ownAiNotConfiguredTooltip")
              : t("analyze.ownAiTooltip")
          }
        >
          {analysisInFlight && isMe ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Search className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {t("analyze.analyseOwnAi")}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Card className="!p-0 overflow-hidden">
        {/* Desktop: classic table */}
        <table className="hidden w-full text-sm md:table">
          <thead className="bg-bg-2 text-xs uppercase tracking-wide text-zinc-400">
            <tr>
              <th className="px-3 py-2 text-left">{t("analyze.players")}</th>
              <th className="px-3 py-2 text-right">DPS</th>
              <th className="px-3 py-2 text-right">HPS</th>
              <th className="px-3 py-2 text-right">{t("topLogs.ilvl")}</th>
              <th className="px-3 py-2 text-right">{t("analyze.parsePercent")}</th>
              <th className="px-3 py-2 text-right">{t("analyze.ilvlPercent")}</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {fight.players.map((p) => {
              const cls = classes.find((c) => c.slug === p.class_slug);
              const spec = cls?.specs.find((s) => s.slug === p.spec_slug);
              const isActive = p.id === activePlayerId;
              return (
                <tr key={p.id} className={`border-t border-bg-3 ${isActive ? "bg-bg-2" : ""}`}>
                  <td className="px-3 py-2">
                    <div className="flex flex-col">
                      <div className="flex items-center gap-1.5">
                        <RoleIcon role={p.role} />
                        <ClassBadge cls={cls} spec={spec} locale={locale} />
                      </div>
                      <span className="text-xs text-zinc-400">
                        {p.name}-{p.server || "?"}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {p.dps ? formatNumber(p.dps, locale) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {p.hps ? formatNumber(p.hps, locale) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {p.item_level ? p.item_level.toFixed(0) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    <ParseChip percent={p.parse_percent} />
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    <ParseChip percent={p.ilvl_percent} />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <AnalyzeButtons player={p} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {/* Mobile: stacked card layout per player */}
        <ul className="divide-y divide-bg-3 md:hidden">
          {fight.players.map((p) => {
            const cls = classes.find((c) => c.slug === p.class_slug);
            const spec = cls?.specs.find((s) => s.slug === p.spec_slug);
            const isActive = p.id === activePlayerId;
            const primary = p.role === "healer" ? p.hps : p.dps;
            const primaryLabel = p.role === "healer" ? "HPS" : "DPS";
            return (
              <li
                key={p.id}
                className={`space-y-2 px-4 py-3 ${isActive ? "bg-bg-2" : ""}`}
              >
                <div className="flex items-center gap-2">
                  <RoleIcon role={p.role} />
                  <ClassBadge cls={cls} spec={spec} locale={locale} />
                  <span className="ml-auto text-sm font-semibold tabular-nums text-accent">
                    {primary ? formatNumber(primary, locale) : "—"}
                    <span className="ml-1 text-xs font-normal text-zinc-400">
                      {primaryLabel}
                    </span>
                  </span>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-zinc-500">
                  <span className="truncate">
                    {p.name}-{p.server || "?"}
                  </span>
                  <span className="tabular-nums">
                    {t("topLogs.ilvl")} {p.item_level ? p.item_level.toFixed(0) : "—"}
                  </span>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5 text-xs text-zinc-400">
                    <span>{t("analyze.parsePercent")}</span>
                    <ParseChip percent={p.parse_percent} />
                    <span className="ml-2">{t("analyze.ilvlPercent")}</span>
                    <ParseChip percent={p.ilvl_percent} />
                  </div>
                  <AnalyzeButtons player={p} />
                </div>
              </li>
            );
          })}
        </ul>
      </Card>

      <p className="text-xs text-zinc-500">
        {t("topLogs.duration")}: {formatDuration(fight.duration_ms)}
      </p>

      {/* scroll-anchor — clicking "Analyze" smooth-scrolls here so the
          user immediately sees the running-state card and, later, the
          AnalysisCard without scrolling past the player table. */}
      <div ref={analysisAnchorRef} className="scroll-mt-24" />

      {analyzeMut.isPending && (
        <Card>
          <div className="flex items-center gap-3">
            <Loader2 className="h-5 w-5 shrink-0 animate-spin text-accent" aria-hidden="true" />
            <p className="text-sm text-zinc-300">{t("analyze.analysisRunning")}</p>
          </div>
        </Card>
      )}
      {analyzeError && !analyzeMut.isPending && (
        <Card className="border-yellow-500/40 bg-yellow-500/5">
          <p className="text-sm text-yellow-200">{analyzeError}</p>
        </Card>
      )}
      {analysisQ.data && (
        <>
          {analysisQ.data.status === "succeeded" && (
            <AnalysisShareControls
              analysis={analysisQ.data}
              locale={locale}
            />
          )}
          <AnalysisCard analysis={analysisQ.data} locale={locale} />
        </>
      )}
    </div>
  );
}


function RecentAnalysesPanel({
  page,
  classes,
  locale,
  loading,
  activeId,
  onSelect,
  query,
  onQueryChange,
  currentPage,
  pageSize,
  onPageChange,
  onDelete,
  deleting,
}: {
  page: PaginatedAnalyses | undefined;
  classes: GameClass[];
  locale: Locale;
  loading: boolean;
  activeId: string | null;
  onSelect: (id: string | null) => void;
  query: string;
  onQueryChange: (q: string) => void;
  currentPage: number;
  pageSize: number;
  onPageChange: (p: number) => void;
  onDelete: (id: string) => void;
  deleting: string | null;
}) {
  const t = useTranslations();
  const items = page?.items ?? [];
  const total = page?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const hasSearch = query.trim().length > 0;

  const severityColor = (status: string, score: number | null) => {
    if (status !== "succeeded") return "text-zinc-400";
    if (score === null) return "text-zinc-300";
    if (score >= 85) return "text-emerald-300";
    if (score >= 65) return "text-amber-300";
    return "text-red-300";
  };

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          {t("analyze.recentAnalyses")}
        </h2>
        <Input
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder={t("analyze.searchAnalyses")}
          className="md:!w-72"
        />
      </div>

      {loading && <p className="text-sm text-zinc-500">{t("common.loading")}</p>}
      {!loading && items.length === 0 && (
        <EmptyState
          message={hasSearch ? t("analyze.noSearchResults") : t("analyze.recentAnalysesEmpty")}
        />
      )}
      {!loading && items.length > 0 && (
        <ul className="divide-y divide-bg-3">
          {items.map((a) => {
            const cls = classes.find((c) => c.slug === a.player_class);
            const spec = cls?.specs.find((s) => s.slug === a.player_spec);
            const fightLabel = a.fight_name_localized || a.fight_name || "—";
            const isActive = a.id === activeId;
            const isDeleting = deleting === a.id;
            return (
              <li
                key={a.id}
                className={`flex flex-wrap items-center justify-between gap-3 rounded-md py-2 transition-colors ${
                  isActive ? "-mx-3 bg-bg-2/40 px-3" : "hover:bg-bg-2/60"
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-2 text-sm">
                    <span className="font-medium text-zinc-100">{a.player_name}</span>
                    <ClassBadge cls={cls} spec={spec} locale={locale} />
                    <span className="text-zinc-500">·</span>
                    <span className="text-zinc-300">{fightLabel}</span>
                    <span
                      className={`ml-2 text-xs font-semibold ${severityColor(
                        a.status,
                        a.overall_score,
                      )}`}
                    >
                      {a.status === "succeeded"
                        ? a.overall_score !== null
                          ? `${a.overall_score}/100`
                          : "—"
                        : a.status}
                    </span>
                  </div>
                  {a.headline && (
                    <p className="mt-0.5 truncate text-xs text-zinc-400">{a.headline}</p>
                  )}
                  <p className="mt-0.5 text-xs text-zinc-500">
                    {formatDateTime(a.created_at, locale)}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    size="sm"
                    variant={isActive ? "ghost" : "secondary"}
                    onClick={() => onSelect(isActive ? null : a.id)}
                  >
                    {isActive ? t("common.cancel") : t("analyze.loadAnalysis")}
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => onDelete(a.id)}
                    disabled={isDeleting}
                    aria-label={t("analyze.deleteAnalysis")}
                    title={t("analyze.deleteAnalysis")}
                  >
                    {isDeleting ? "…" : "🗑"}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {!loading && total > pageSize && (
        <div className="mt-4 flex items-center justify-between text-xs text-zinc-400">
          <span>{t("analyze.pageOf", { page: currentPage, total: totalPages })}</span>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={currentPage <= 1}
              onClick={() => onPageChange(Math.max(1, currentPage - 1))}
            >
              ← {t("analyze.previousPage")}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={currentPage >= totalPages}
              onClick={() => onPageChange(Math.min(totalPages, currentPage + 1))}
            >
              {t("analyze.nextPage")} →
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}
