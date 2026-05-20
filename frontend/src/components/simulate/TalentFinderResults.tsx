"use client";

import { useState } from "react";
import { Check, Copy, Crown, Sparkles, TrendingDown, TrendingUp } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button, Card } from "@/components/ui";
import type { Locale } from "@/i18n/config";
import { formatNumber } from "@/lib/format";
import type { Simulation, SimulationRunOut } from "@/types/api";

interface Props {
  simulation: Simulation;
  locale: Locale;
}

interface RankedBuild {
  rank: number;
  label: string;
  loadout_code: string;
  dps_mean: number;
}

const TOP_N_RENDERED = 5;

export function TalentFinderResults({ simulation, locale }: Props) {
  const t = useTranslations();

  const succeeded = (simulation.runs ?? []).filter(
    (r) => r.status === "succeeded",
  );
  const loadouts = simulation.loadouts ?? [];
  // Sweep runs tag their loadouts with tf_role; cluster runs don't.
  const sweepMode = loadouts.some((l) => l.tf_role);

  const roleOf = (run: SimulationRunOut): string =>
    loadouts[run.loadout_index]?.tf_role || "";

  // Final ranking pool: for a sweep, only the combine round is sized at
  // the tight target_error, so rank those; the phase-1 baseline/flip
  // sims are looser screening. Cluster runs: rank everything.
  let rankPool = sweepMode
    ? succeeded.filter((r) => roleOf(r) === "combine")
    : succeeded;
  if (rankPool.length === 0) rankPool = succeeded; // defensive fallback

  const ranked: RankedBuild[] = rankPool
    .map((run) => ({ run, loadout: loadouts[run.loadout_index] }))
    .sort((a, b) => b.run.dps_mean - a.run.dps_mean)
    .map((entry, idx) => ({
      rank: idx + 1,
      label: entry.loadout?.name || entry.run.loadout_name || `#${idx + 1}`,
      loadout_code: entry.loadout?.loadout_code || "",
      dps_mean: entry.run.dps_mean,
    }));

  const [winner, ...rest] = ranked;
  if (!winner) {
    return (
      <Card>
        <p className="text-sm text-zinc-400">
          {t("simulate.talentFinder.resultsWaiting")}
        </p>
      </Card>
    );
  }
  const topFive = [winner, ...rest].slice(0, TOP_N_RENDERED);

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-start gap-3">
          <Sparkles className="mt-1 h-5 w-5 shrink-0 text-amber-400" aria-hidden />
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-semibold">
              {t("simulate.talentFinder.resultsTitle")}
            </h2>
            <p className="mt-1 text-sm text-zinc-400">
              {t("simulate.talentFinder.resultsHint", {
                total: simulation.runs?.length ?? 0,
                shown: topFive.length,
              })}
            </p>
          </div>
        </div>
      </Card>

      {sweepMode && (
        <SweepScreening
          simulation={simulation}
          succeeded={succeeded}
          roleOf={roleOf}
        />
      )}

      {topFive.map((b) => (
        <BuildCard
          key={`${b.rank}-${b.label}`}
          build={b}
          winnerDps={winner.dps_mean}
          isWinner={b.rank === 1}
          locale={locale}
        />
      ))}
    </div>
  );
}

interface SweepScreeningProps {
  simulation: Simulation;
  succeeded: SimulationRunOut[];
  roleOf: (run: SimulationRunOut) => string;
}

/** The phase-1 single-flip screening: each talent change and its
 * measured DPS delta vs the consensus baseline. */
function SweepScreening({ simulation, succeeded, roleOf }: SweepScreeningProps) {
  const t = useTranslations();
  const loadouts = simulation.loadouts ?? [];

  const baseline = succeeded.find((r) => roleOf(r) === "baseline");
  const flips = succeeded
    .filter((r) => roleOf(r) === "sweep")
    .map((r) => {
      const lo = loadouts[r.loadout_index];
      return {
        name: lo?.tf_flip?.node_name || lo?.name || "?",
        dps: r.dps_mean,
      };
    });

  if (!baseline || flips.length === 0) return null;
  const baseDps = baseline.dps_mean;

  const rows = flips
    .map((f) => ({
      ...f,
      deltaPct: baseDps > 0 ? ((f.dps - baseDps) / baseDps) * 100 : 0,
    }))
    .sort((a, b) => b.deltaPct - a.deltaPct);

  return (
    <Card>
      <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
        {t("simulate.talentFinder.screeningTitle")}
      </h3>
      <p className="mt-1 text-xs text-zinc-500">
        {t("simulate.talentFinder.screeningHint", {
          baseline: formatNumberPlain(baseDps),
        })}
      </p>
      <ul className="mt-3 divide-y divide-bg-3">
        {rows.map((r, i) => (
          <li
            key={`${r.name}-${i}`}
            className="flex items-center justify-between gap-3 py-1.5 text-sm"
          >
            <span className="min-w-0 truncate text-zinc-300">{r.name}</span>
            <span
              className={
                r.deltaPct >= 0
                  ? "inline-flex items-center gap-1 font-mono text-emerald-400"
                  : "inline-flex items-center gap-1 font-mono text-rose-400"
              }
            >
              {r.deltaPct >= 0 ? (
                <TrendingUp className="h-3.5 w-3.5" aria-hidden />
              ) : (
                <TrendingDown className="h-3.5 w-3.5" aria-hidden />
              )}
              {r.deltaPct >= 0 ? "+" : ""}
              {r.deltaPct.toFixed(2)}%
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function formatNumberPlain(n: number): string {
  return Math.round(n).toLocaleString();
}

interface BuildCardProps {
  build: RankedBuild;
  winnerDps: number;
  isWinner: boolean;
  locale: Locale;
}

function BuildCard({ build, winnerDps, isWinner, locale }: BuildCardProps) {
  const t = useTranslations();
  const [copied, setCopied] = useState(false);

  const deltaPct =
    winnerDps > 0 ? ((build.dps_mean - winnerDps) / winnerDps) * 100 : 0;

  const onCopy = async () => {
    if (!build.loadout_code) return;
    try {
      await navigator.clipboard.writeText(build.loadout_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore clipboard failures (e.g. http context, no permission)
    }
  };

  return (
    <Card className={isWinner ? "ring-1 ring-amber-500/50" : undefined}>
      <div className="flex flex-wrap items-baseline justify-between gap-4">
        <div className="flex items-center gap-2">
          {isWinner ? (
            <Crown className="h-5 w-5 text-amber-400" aria-hidden />
          ) : (
            <span className="font-mono text-sm text-zinc-500">
              #{build.rank}
            </span>
          )}
          <span className="font-mono text-sm text-zinc-300">{build.label}</span>
        </div>
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-xl tabular-nums text-zinc-100">
            {formatNumber(build.dps_mean, locale)}
          </span>
          <span className="text-xs text-zinc-500">DPS</span>
          {!isWinner && (
            <span
              className={
                deltaPct >= 0 ? "text-xs text-emerald-400" : "text-xs text-rose-400"
              }
            >
              {deltaPct >= 0 ? "+" : ""}
              {deltaPct.toFixed(2)}%
            </span>
          )}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-stretch gap-2">
        <code className="min-w-0 flex-1 break-all rounded-md border border-bg-3 bg-bg-1/40 px-3 py-2 font-mono text-xs text-zinc-400">
          {build.loadout_code || t("simulate.talentFinder.noLoadoutCode")}
        </code>
        <Button
          type="button"
          variant="ghost"
          onClick={onCopy}
          disabled={!build.loadout_code}
          className="inline-flex items-center gap-2"
        >
          {copied ? (
            <>
              <Check className="h-4 w-4" aria-hidden />
              {t("simulate.talentFinder.copied")}
            </>
          ) : (
            <>
              <Copy className="h-4 w-4" aria-hidden />
              {t("simulate.talentFinder.copy")}
            </>
          )}
        </Button>
      </div>
    </Card>
  );
}
