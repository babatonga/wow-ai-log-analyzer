"use client";

import { useState } from "react";
import { Check, Copy, Crown, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button, Card } from "@/components/ui";
import type { Locale } from "@/i18n/config";
import { formatNumber } from "@/lib/format";
import type { Simulation } from "@/types/api";

interface Props {
  simulation: Simulation;
  locale: Locale;
}

interface RankedBuild {
  rank: number;
  label: string;
  loadout_code: string;
  dps_mean: number;
  dps_stddev: number;
}

const TOP_N_RENDERED = 5;

export function TalentFinderResults({ simulation, locale }: Props) {
  const t = useTranslations();

  const succeeded = (simulation.runs ?? []).filter(
    (r) => r.status === "succeeded",
  );

  // Sort by mean DPS descending, then index into the parent's loadouts
  // to recover the per-variant Blizzard code we stored at create time.
  const ranked: RankedBuild[] = succeeded
    .map((run) => {
      const loadout = simulation.loadouts?.[run.loadout_index];
      return {
        run,
        loadout,
      };
    })
    .sort((a, b) => b.run.dps_mean - a.run.dps_mean)
    .map((entry, idx) => ({
      rank: idx + 1,
      label: entry.loadout?.name || entry.run.loadout_name || `#${idx + 1}`,
      loadout_code: entry.loadout?.loadout_code || "",
      dps_mean: entry.run.dps_mean,
      dps_stddev: entry.run.dps_stddev,
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
    <Card
      className={isWinner ? "ring-1 ring-amber-500/50" : undefined}
    >
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
