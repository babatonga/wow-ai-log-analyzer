"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button, Card, FieldError, Input, Label } from "@/components/ui";
import { ApiClientError, apiFetch } from "@/lib/api";
import type {
  FightProfileKey,
  Simulation,
  TalentFinderEncounterMap,
  TalentFinderStrategy,
} from "@/types/api";

interface Props {
  onSimulationStarted: (sim: Simulation) => void;
}

const SUPPORTED_PROFILES: Exclude<FightProfileKey, "custom">[] = [
  "single_target",
  "council",
  "mythic_plus",
];

const PROFILE_LABELS: Record<typeof SUPPORTED_PROFILES[number], { en: string; de: string }> = {
  single_target: { en: "Single Target", de: "Single Target" },
  council: { en: "Council", de: "Council" },
  mythic_plus: { en: "Mythic+", de: "Mythic+" },
};

export function TalentFinderForm({ onSimulationStarted }: Props) {
  const t = useTranslations();

  const [profile, setProfile] = useState("");
  const [label, setLabel] = useState("");
  const [fightProfile, setFightProfile] = useState<
    typeof SUPPORTED_PROFILES[number]
  >("single_target");
  const [strategy, setStrategy] = useState<TalentFinderStrategy>("sweep");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [topN, setTopN] = useState(15);
  const [threshold, setThreshold] = useState(0.25);
  const [err, setErr] = useState<string | null>(null);

  const mapQ = useQuery({
    queryKey: ["talent-finder-encounter-map"],
    queryFn: () =>
      apiFetch<TalentFinderEncounterMap>("/api/v1/talent-finder/encounter-map"),
    staleTime: 5 * 60 * 1000,
  });

  const runMut = useMutation({
    mutationFn: () =>
      apiFetch<Simulation>("/api/v1/talent-finder/run", {
        method: "POST",
        body: {
          label,
          simc_profile: profile,
          fight_profile_key: fightProfile,
          precision: "fast",
          top_n: topN,
          threshold,
          strategy,
        },
      }),
    onSuccess: (sim) => {
      setErr(null);
      onSimulationStarted(sim);
    },
    onError: (e) => {
      setErr(e instanceof ApiClientError ? e.message : t("errors.generic"));
    },
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (profile.trim().length < 20) {
      setErr(t("simulate.talentFinder.errorEmptyProfile"));
      return;
    }
    if (!mapQ.data?.[fightProfile]) {
      setErr(t("simulate.talentFinder.errorProfileNotConfigured"));
      return;
    }
    runMut.mutate();
  };

  // Disable a fight-profile option when the admin hasn't mapped an
  // encounter to it yet; the backend would 422 anyway, but it's a
  // better UX to surface that up front.
  const isConfigured = (key: typeof SUPPORTED_PROFILES[number]): boolean =>
    Boolean(mapQ.data?.[key]);

  return (
    <Card>
      <div className="flex items-start gap-3">
        <Sparkles className="mt-1 h-5 w-5 shrink-0 text-amber-400" aria-hidden />
        <div>
          <h2 className="text-lg font-semibold">
            {t("simulate.talentFinder.title")}
          </h2>
          <p className="mt-1 text-sm text-zinc-400">
            {t("simulate.talentFinder.intro")}
          </p>
        </div>
      </div>

      <form onSubmit={submit} className="mt-5 space-y-4">
        <div>
          <Label>{t("simulate.talentFinder.profileLabel")}</Label>
          <textarea
            value={profile}
            onChange={(e) => setProfile(e.target.value)}
            placeholder={t("simulate.talentFinder.profilePlaceholder")}
            rows={10}
            className="mt-1 block w-full rounded-md border border-bg-3 bg-bg-1/40 px-3 py-2 font-mono text-xs text-zinc-200 focus:border-amber-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
          />
          <p className="mt-1 text-xs text-zinc-500">
            {t("simulate.talentFinder.profileHint")}
          </p>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <Label>{t("simulate.talentFinder.fightProfileLabel")}</Label>
            <div className="mt-1 grid grid-cols-3 gap-2">
              {SUPPORTED_PROFILES.map((k) => {
                const enabled = isConfigured(k);
                const active = fightProfile === k;
                return (
                  <button
                    key={k}
                    type="button"
                    disabled={!enabled}
                    onClick={() => setFightProfile(k)}
                    title={
                      enabled
                        ? mapQ.data?.[k]?.encounter_name || undefined
                        : t("simulate.talentFinder.profileNotConfigured")
                    }
                    className={[
                      "rounded-md border px-3 py-2 text-sm transition",
                      active
                        ? "border-amber-500 bg-amber-500/10 text-amber-300"
                        : enabled
                          ? "border-bg-3 bg-bg-2 text-zinc-300 hover:border-zinc-500"
                          : "cursor-not-allowed border-bg-3 bg-bg-2/40 text-zinc-600 line-through",
                    ].join(" ")}
                  >
                    {PROFILE_LABELS[k].en}
                  </button>
                );
              })}
            </div>
            {mapQ.data?.[fightProfile] && (
              <p className="mt-1 text-xs text-zinc-500">
                {t("simulate.talentFinder.miningFrom", {
                  encounter:
                    mapQ.data[fightProfile]?.encounter_name ||
                    `Encounter ${mapQ.data[fightProfile]?.encounter_id}`,
                })}
              </p>
            )}
          </div>

          <div>
            <Label htmlFor="tf-label">
              {t("simulate.talentFinder.labelLabel")}
            </Label>
            <Input
              id="tf-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder={t("simulate.talentFinder.labelPlaceholder")}
            />
          </div>
        </div>

        <div>
          <Label>{t("simulate.talentFinder.strategyLabel")}</Label>
          <div className="mt-1 grid gap-2 sm:grid-cols-2">
            {(["sweep", "cluster"] as const).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setStrategy(s)}
                className={[
                  "rounded-md border px-3 py-2 text-left text-sm transition",
                  strategy === s
                    ? "border-amber-500 bg-amber-500/10"
                    : "border-bg-3 bg-bg-2 hover:border-zinc-500",
                ].join(" ")}
              >
                <div
                  className={
                    strategy === s ? "font-medium text-amber-300" : "text-zinc-300"
                  }
                >
                  {t(`simulate.talentFinder.strategy_${s}`)}
                </div>
                <div className="mt-0.5 text-xs text-zinc-500">
                  {t(`simulate.talentFinder.strategy_${s}_hint`)}
                </div>
              </button>
            ))}
          </div>
        </div>

        <details
          open={advancedOpen}
          onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
        >
          <summary className="cursor-pointer text-sm text-zinc-400 hover:text-zinc-200">
            {t("simulate.talentFinder.advanced")}
          </summary>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div>
              <Label htmlFor="tf-top-n">
                {t("simulate.talentFinder.topNLabel")}
              </Label>
              <Input
                id="tf-top-n"
                type="number"
                min={1}
                max={30}
                value={topN}
                onChange={(e) => setTopN(parseInt(e.target.value, 10) || 15)}
              />
              <p className="mt-1 text-xs text-zinc-500">
                {t("simulate.talentFinder.topNHint")}
              </p>
            </div>
            <div>
              <Label htmlFor="tf-threshold">
                {t("simulate.talentFinder.thresholdLabel")}
              </Label>
              <Input
                id="tf-threshold"
                type="number"
                min={0.05}
                max={0.95}
                step={0.05}
                value={threshold}
                onChange={(e) =>
                  setThreshold(parseFloat(e.target.value) || 0.3)
                }
              />
              <p className="mt-1 text-xs text-zinc-500">
                {t("simulate.talentFinder.thresholdHint")}
              </p>
            </div>
          </div>
        </details>

        <div className="flex items-center gap-3 pt-2">
          <Button
            type="submit"
            disabled={runMut.isPending || profile.trim().length < 20}
            className="inline-flex items-center gap-2"
          >
            {runMut.isPending ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                {t("simulate.talentFinder.starting")}
              </>
            ) : (
              t("simulate.talentFinder.submit")
            )}
          </Button>
          <span className="text-xs text-zinc-500">
            {t("simulate.talentFinder.fixedRotation")}
          </span>
        </div>
        <FieldError>{err}</FieldError>
      </form>
    </Card>
  );
}
