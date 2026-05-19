"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { Button, Card, FieldError, Input, Label } from "@/components/ui";
import { ApiClientError, apiFetch } from "@/lib/api";
import type {
  TalentFinderEncounterMap,
  TalentFinderEncounterMapEntry,
} from "@/types/api";

type ProfileKey = "single_target" | "council" | "mythic_plus";

const PROFILE_ORDER: ProfileKey[] = ["single_target", "council", "mythic_plus"];

const PROFILE_LABELS: Record<ProfileKey, { en: string; de: string }> = {
  single_target: { en: "Single Target", de: "Single Target" },
  council: { en: "Council", de: "Council" },
  mythic_plus: { en: "Mythic+", de: "Mythic+" },
};

interface RowState {
  encounter_id: string; // bound to <Input>, so string
  encounter_name: string;
  difficulty: string;
  is_raid: boolean;
}

function entryToRow(entry: TalentFinderEncounterMapEntry | null): RowState {
  if (!entry) {
    return { encounter_id: "", encounter_name: "", difficulty: "Mythic", is_raid: true };
  }
  return {
    encounter_id: String(entry.encounter_id),
    encounter_name: entry.encounter_name ?? "",
    difficulty: entry.difficulty ?? "",
    is_raid: entry.is_raid,
  };
}

function rowToEntry(row: RowState): TalentFinderEncounterMapEntry | null {
  const id = parseInt(row.encounter_id.trim(), 10);
  if (!Number.isFinite(id) || id <= 0) return null;
  return {
    encounter_id: id,
    encounter_name: row.encounter_name.trim(),
    difficulty: row.difficulty.trim() || null,
    is_raid: row.is_raid,
  };
}

export function TalentFinderEncounterMapCard() {
  const t = useTranslations();
  const qc = useQueryClient();
  const [rows, setRows] = useState<Record<ProfileKey, RowState>>(() => ({
    single_target: { encounter_id: "", encounter_name: "", difficulty: "Mythic", is_raid: true },
    council: { encounter_id: "", encounter_name: "", difficulty: "Mythic", is_raid: true },
    mythic_plus: { encounter_id: "", encounter_name: "", difficulty: "", is_raid: false },
  }));
  const [err, setErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  const mapQ = useQuery({
    queryKey: ["admin-talent-finder-encounter-map"],
    queryFn: () =>
      apiFetch<TalentFinderEncounterMap>("/api/v1/admin/talent-finder/encounter-map"),
  });

  useEffect(() => {
    if (!mapQ.data) return;
    setRows({
      single_target: entryToRow(mapQ.data.single_target),
      council: entryToRow(mapQ.data.council),
      mythic_plus: entryToRow(mapQ.data.mythic_plus),
    });
  }, [mapQ.data]);

  const saveMut = useMutation({
    mutationFn: (next: TalentFinderEncounterMap) =>
      apiFetch<TalentFinderEncounterMap>("/api/v1/admin/talent-finder/encounter-map", {
        method: "PUT",
        body: next,
      }),
    onSuccess: () => {
      setFlash(t("admin.talentFinderMapSaved"));
      setErr(null);
      qc.invalidateQueries({ queryKey: ["admin-talent-finder-encounter-map"] });
    },
    onError: (e) =>
      setErr(e instanceof ApiClientError ? e.message : t("errors.generic")),
  });

  const updateRow = (key: ProfileKey, patch: Partial<RowState>) =>
    setRows((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }));

  const onSave = (e: React.FormEvent) => {
    e.preventDefault();
    setFlash(null);
    setErr(null);
    const next: TalentFinderEncounterMap = {
      single_target: rowToEntry(rows.single_target),
      council: rowToEntry(rows.council),
      mythic_plus: rowToEntry(rows.mythic_plus),
      // ``custom`` is not configurable here — leave whatever's on file.
      custom: mapQ.data?.custom ?? null,
    };
    saveMut.mutate(next);
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold">{t("admin.talentFinderMapTitle")}</h2>
      <p className="mt-1 text-sm text-zinc-400">{t("admin.talentFinderMapHelp")}</p>

      <form onSubmit={onSave} className="mt-4 space-y-4">
        {PROFILE_ORDER.map((key) => {
          const row = rows[key];
          const label = PROFILE_LABELS[key].en; // i18n: hard-code for now,
          // both EN/DE labels are identical for the three profiles.
          return (
            <div
              key={key}
              className="grid gap-3 rounded-lg border border-bg-3 bg-bg-2/30 p-3 md:grid-cols-[140px_120px_1fr_140px_auto]"
            >
              <Label className="self-center">{label}</Label>
              <Input
                inputMode="numeric"
                placeholder={t("admin.talentFinderMapEncounterIdPlaceholder")}
                value={row.encounter_id}
                onChange={(e) => updateRow(key, { encounter_id: e.target.value })}
              />
              <Input
                placeholder={t("admin.talentFinderMapEncounterNamePlaceholder")}
                value={row.encounter_name}
                onChange={(e) => updateRow(key, { encounter_name: e.target.value })}
              />
              <Input
                placeholder={t("admin.talentFinderMapDifficultyPlaceholder")}
                value={row.difficulty}
                onChange={(e) => updateRow(key, { difficulty: e.target.value })}
              />
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={row.is_raid}
                  onChange={(e) => updateRow(key, { is_raid: e.target.checked })}
                  className="h-4 w-4 accent-amber-500"
                />
                {t("admin.talentFinderMapIsRaid")}
              </label>
            </div>
          );
        })}

        <div className="flex items-center gap-3">
          <Button type="submit" disabled={saveMut.isPending}>
            {saveMut.isPending ? t("common.loading") : t("admin.talentFinderMapSave")}
          </Button>
          {flash && <span className="text-sm text-emerald-300">{flash}</span>}
        </div>
        <FieldError>{err}</FieldError>
      </form>
    </Card>
  );
}
