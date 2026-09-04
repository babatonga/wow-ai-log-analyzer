"""Compose the AI prompt from DB data and run an analysis."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.errors import NoTopLogsError, NotFoundError, UpstreamError
from app.models import (
    Analysis,
    AnalysisStatus,
    AppSetting,
    Report,
    ReportFight,
    ReportPlayer,
    ReportPlayerCast,
    TopLog,
    WowLocalization,
)
from app.services.ai.anthropic_provider import AnthropicProvider
from app.services.ai.base import AiProvider, AiResponse
from app.services.ai.openai_provider import OpenAiCompatibleProvider
from app.services.ai.prompts import build_user_prompt, system_prompt_for
from app.services.wcl.client import WclClient
from app.services.wcl.parser import (
    aggregate_mana_recovery,
    parse_aura_table,
    parse_casts_table,
    parse_damage_taken_table,
    parse_player_resource_events_page,
    parse_report_rankings_for_player,
)
from app.services.wcl.queries import (
    REPORT_BUFFS_FOR_PLAYER,
    REPORT_CASTS,
    REPORT_DAMAGE_TAKEN_FOR_PLAYER,
    REPORT_DEBUFFS_BY_PLAYER,
    REPORT_PLAYER_RESOURCE_EVENTS,
    REPORT_RANKINGS,
)
from app.services.wcl_oauth_service import build_user_wcl_client
from app.services.wow_data_service import (
    lookup_names,
    resolve_encounter_names_with_fallback,
)

logger = logging.getLogger(__name__)


_VALID_PROVIDERS = {"anthropic", "openai", "local"}


async def _resolve_provider_choice(session: AsyncSession) -> str:
    """Pick the active provider. Admin UI (AppSetting) wins over .env."""
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == "ai_provider"))
    ).scalar_one_or_none()
    if row and row.value:
        chosen = (row.value or {}).get("value")
        if chosen in _VALID_PROVIDERS or chosen == "disabled":
            return chosen
    return settings.ai_provider


def _provider_for(choice: str, *, openai_reasoning_effort: str | None = None) -> AiProvider:
    if choice == "anthropic":
        return AnthropicProvider()
    if choice == "openai":
        # The admin Settings panel can override the env-level
        # ``OPENAI_REASONING_EFFORT`` via the ``openai_reasoning_effort``
        # AppSetting; the caller resolves it and passes it here so we don't
        # silently inherit the env default when an admin has explicitly set
        # the override to "" (= off).
        return OpenAiCompatibleProvider(
            mode="openai", reasoning_effort=openai_reasoning_effort
        )
    if choice == "local":
        return OpenAiCompatibleProvider(mode="local")
    raise UpstreamError(f"Unsupported AI provider: {choice}")


async def _resolve_openai_reasoning_effort(session: AsyncSession) -> str | None:
    """Read the admin override for OpenAI reasoning_effort.

    Semantics mirror the per-user BYOK dropdown:

      - "minimal" / "low" / "medium" / "high" → admin chose this value;
        the provider uses it.
      - "" or no AppSetting row → no override; the provider falls back to
        ``settings.openai_reasoning_effort`` (env).

    Returning ``None`` from this helper covers both "no row stored" and
    "explicitly empty string saved" — both mean "fall through to env".
    """
    row = (
        await session.execute(
            select(AppSetting).where(AppSetting.key == "openai_reasoning_effort")
        )
    ).scalar_one_or_none()
    if not row or not row.value:
        return None
    value = str((row.value or {}).get("value") or "").strip().lower()
    if value in {"minimal", "low", "medium", "high"}:
        return value
    return None


async def _resolve_model(session: AsyncSession, choice: str) -> str:
    # The local provider always serves the model the local-ai container was
    # started with — switching it requires editing LOCAL_AI_MODEL in .env and
    # restarting that container, so the admin AI-Model dropdown is moot here.
    if choice == "local":
        return settings.local_ai_model
    row = (
        await session.execute(select(AppSetting).where(AppSetting.key == "ai_model"))
    ).scalar_one_or_none()
    if row and row.value:
        return str((row.value or {}).get("value") or settings.ai_model)
    return settings.ai_model


async def _ensure_references(
    session: AsyncSession,
    *,
    spec_slug: str,
    encounter_id: int,
    metric: str,
    difficulty: int | None = None,
) -> bool:
    """Synchronously seed top logs for (spec, encounter, metric) so the
    analyser has reference data.

    Returns True iff the seed produced at least one row. False means WCL
    genuinely has no public top-log entries for this combination yet (very
    new boss / low log volume) — in that case the caller falls back to a
    general-best-practice analysis without delta comparisons.

    The first analysis on a new boss therefore takes ~1-3 min longer
    (rankings + composition checks + 5 detail fetches); every subsequent
    analysis on the same boss is fast.

    Implementation note: the seeding writes happen on a SEPARATE session
    so committing them doesn't pollute the caller's transaction context.
    The original implementation called ``session.commit()`` mid-flight
    inside the worker's outer ``session.begin()`` block, which closed
    the outer transaction and made every subsequent query on that
    session raise ``InvalidRequestError: Can't operate on closed
    transaction``. The refs are committed eagerly so they're cached for
    later requests even if the analysis itself fails later.
    """
    from app.db import async_session_factory
    from app.models import GameSpec
    from app.services.top_logs_service import refresh_top_logs_for_spec_encounter
    from app.services.wcl.client import WclClient

    spec = (
        await session.execute(
            select(GameSpec).where(GameSpec.slug == spec_slug)
        )
    ).scalar_one_or_none()
    if not spec:
        return False

    try:
        async with async_session_factory() as seed_session:
            async with seed_session.begin():
                async with WclClient() as wcl:
                    rows = await refresh_top_logs_for_spec_encounter(
                        seed_session,
                        spec=spec,
                        encounter_id=encounter_id,
                        metric=metric,
                        # ``is_raid`` defaults the difficulty filter; M+
                        # encounters produce empty rankings via the same
                        # path which is fine.
                        is_raid=True,
                        # The caller passes the difficulty of the user's
                        # own fight so a Heroic log is compared against
                        # Heroic top logs; None falls back to the .env
                        # default (Mythic).
                        difficulty=difficulty,
                        wcl_client=wcl,
                    )
                # context manager commits the seed_session transaction
        logger.info(
            "sync-seed for spec=%s encounter=%s metric=%s difficulty=%s → %s rows",
            spec_slug,
            encounter_id,
            metric,
            difficulty,
            len(rows),
        )
        return len(rows) > 0
    except Exception:  # noqa: BLE001
        logger.exception(
            "sync-seed failed for spec=%s encounter=%s metric=%s",
            spec_slug,
            encounter_id,
            metric,
        )
        return False


async def _fetch_top_log_references(
    session: AsyncSession,
    *,
    spec_slug: str,
    encounter_id: int | None,
    role: str,
    difficulty: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if not encounter_id or not spec_slug:
        return []
    metric = "hps" if role == "healer" else "dps"
    # Only references with detail_payload are useful for the AI — rows
    # without detail (e.g., where the original top-log fetch couldn't match
    # the player) carry only metadata and produce empty cast/gear arrays in
    # the prompt. Slide-down logic in top_logs_service ensures the lowest
    # ``top_logs_detail_count`` ranks have detail; we use the same filter
    # here so a higher-ranked slot with missing detail can't crowd out a
    # slightly lower-ranked one that has it.
    stmt = (
        select(TopLog)
        .where(
            TopLog.spec_slug == spec_slug,
            TopLog.encounter_id == encounter_id,
            TopLog.metric == metric,
            TopLog.detail_payload.is_not(None),
        )
        .order_by(TopLog.rank.asc())
        .limit(limit)
    )
    # Raid analyses compare against top logs of the SAME difficulty as the
    # user's own fight (Heroic run → Heroic references). ``None`` (M+ or
    # unknown difficulty) keeps the historical unfiltered behaviour.
    if difficulty is not None:
        stmt = stmt.where(TopLog.difficulty == difficulty)
    rows = (await session.execute(stmt)).scalars().all()
    # Drop rows whose detail blob was written by an older code shape —
    # they'd feed the AI a payload missing fields (e.g. boss_casts on
    # pre-v0.3.0 detail). When all rows are stale this returns ``[]``,
    # which makes the caller fall through to ``_ensure_references`` →
    # incremental refresh → fresh detail with the current version stamp.
    from app.services.top_logs_service import TOP_LOG_DETAIL_VERSION as _DV

    rows = [
        r
        for r in rows
        if int((r.detail_payload or {}).get("_v") or 0) >= _DV
    ]
    out: list[dict[str, Any]] = []
    for r in rows:
        detail = r.detail_payload or {}
        out.append(
            {
                "rank": r.rank,
                "amount": r.amount,
                "item_level": r.item_level,
                "duration_ms": r.duration_ms,
                "character": f"{r.character_name}-{r.server}",
                "region": r.region,
                "wcl_report_code": r.wcl_report_code,
                "wcl_fight_id": r.wcl_fight_id,
                "composition": (r.payload or {}).get("composition") or {},
                # Full detail (casts, gear, buffs, debuffs, damage_taken,
                # talents, stats) for top performers — empty dict if we
                # never fetched it.
                "detail": {
                    "talents_loadout": detail.get("talents_loadout"),
                    "talent_ids": detail.get("talent_ids") or [],
                    "stats": detail.get("stats") or {},
                    "casts": detail.get("casts") or [],
                    "gear": detail.get("gear") or [],
                    "buffs": detail.get("buffs") or [],
                    "debuffs": detail.get("debuffs") or [],
                    "damage_taken": detail.get("damage_taken") or [],
                    # Boss-cast cycle timeline of the reference kill, same
                    # shape as the user fight's ``fight.boss_casts`` so the
                    # AI can compare cycle alignment directly.
                    "boss_casts": detail.get("boss_casts") or [],
                },
            }
        )
    return out


async def _enrich_player_with_aura_and_damage_taken(
    session: AsyncSession,
    *,
    report: Report,
    fight: ReportFight,
    player: ReportPlayer,
    requested_by_id: uuid.UUID | None,
) -> None:
    """Fetch buffs/debuffs/damage-taken for the analyzed player from WCL.

    Caches the result in ``player.extras`` so subsequent analyses of the same
    player don't re-fetch. Uses the requesting user's WCL OAuth token if they
    have connected, otherwise falls back to the global client_credentials token
    (which only works for public reports).
    """
    extras = dict(player.extras or {})
    has_auras = {"buffs", "debuffs", "damage_taken"}.issubset(extras.keys())
    has_casts = bool(player.casts)
    # ``parse_metrics`` is a marker dict — present (even with all-None values)
    # means "we've already asked WCL". Avoids re-fetching for fights where WCL
    # genuinely has no rankings (very fresh boss / non-public log).
    has_parse_metrics = "parse_metrics" in extras
    # Only fetched for healers — DPS / tanks don't run on a mana-pressure
    # budget worth analysing. We mark the key explicitly (even with zero
    # recovery events) so subsequent analyses of the same player don't
    # re-query WCL.
    needs_mana_recovery = (
        player.role == "healer" and "mana_recovery" not in extras
    )
    if has_auras and has_casts and has_parse_metrics and not needs_mana_recovery:
        return

    user_client: WclClient | None = None
    if requested_by_id is not None:
        try:
            user_client = await build_user_wcl_client(session, user_id=requested_by_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to build user WCL client; falling back to client_credentials")
    client = user_client or WclClient()
    own = user_client is None  # if we built our own client we must close it

    code = report.wcl_code
    fight_id = fight.fight_id
    actor_id = player.actor_id
    try:
        if not has_casts:
            try:
                casts_payload = await client.query(
                    REPORT_CASTS,
                    {"code": code, "fightIDs": [fight_id], "sourceID": actor_id},
                )
                casts = parse_casts_table(casts_payload)
                for c in sorted(casts, key=lambda c: c.get("total", 0), reverse=True)[:25]:
                    session.add(ReportPlayerCast(player_id=player.id, **c))
            except Exception:  # noqa: BLE001
                logger.exception("Casts fetch failed; continuing without")

        if not has_auras:
            try:
                buffs_payload = await client.query(
                    REPORT_BUFFS_FOR_PLAYER,
                    {"code": code, "fightIDs": [fight_id], "sourceID": actor_id},
                )
                extras["buffs"] = parse_aura_table(buffs_payload)
            except Exception:  # noqa: BLE001
                logger.exception("Buffs fetch failed; continuing without")
                extras.setdefault("buffs", [])

            try:
                debuffs_payload = await client.query(
                    REPORT_DEBUFFS_BY_PLAYER,
                    {"code": code, "fightIDs": [fight_id], "sourceID": actor_id},
                )
                extras["debuffs"] = parse_aura_table(debuffs_payload)
            except Exception:  # noqa: BLE001
                logger.exception("Debuffs fetch failed; continuing without")
                extras.setdefault("debuffs", [])

            try:
                dmg_taken_payload = await client.query(
                    REPORT_DAMAGE_TAKEN_FOR_PLAYER,
                    {"code": code, "fightIDs": [fight_id], "targetID": actor_id},
                )
                extras["damage_taken"] = parse_damage_taken_table(dmg_taken_payload)
            except Exception:  # noqa: BLE001
                logger.exception("DamageTaken fetch failed; continuing without")
                extras.setdefault("damage_taken", [])

        if not has_parse_metrics:
            # WCL's report.rankings returns the player's parse percentile
            # (rankPercent → "Parse %" — vs all public logs) and the
            # ilvl-bracket percentile (bracketPercent → "iLvl %" — vs the
            # same item-level bracket, gear-normalised). Used by the AI
            # prompt to calibrate tone (99-parse player gets praise +
            # nitpicks; low parser gets fundamentals first) and shown next
            # to the score in the analysis card.
            metric_for_rankings = "hps" if player.role == "healer" else "dps"
            try:
                rankings_payload = await client.query(
                    REPORT_RANKINGS,
                    {
                        "code": code,
                        "fightIDs": [fight_id],
                        "playerMetric": metric_for_rankings,
                    },
                )
                extras["parse_metrics"] = parse_report_rankings_for_player(
                    rankings_payload,
                    fight_id=fight_id,
                    actor_id=actor_id,
                    player_name=player.name,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Rankings fetch failed; continuing without parse %")
                extras["parse_metrics"] = {
                    "rank_percent": None,
                    "ilvl_percent": None,
                    "total_parses": None,
                    "rank": None,
                    "out_of": None,
                }

        if needs_mana_recovery:
            # Healers only — paginate the player's resource events (= mana
            # grants from Mana Tea, Innervate, potions, buff procs), drop
            # any non-mana entries, and aggregate per ability with their
            # fight-relative timestamps. WCL events are report-relative,
            # so we need the WCL fight start offset that the import flow
            # stashes in ``fight.extras['wcl_fight_start_ms']``. Falls
            # back to 0 (so seconds will look weird) only for fights
            # imported before that field existed.
            fight_extras = fight.extras or {}
            fight_start_ms = int(fight_extras.get("wcl_fight_start_ms") or 0)
            try:
                collected: list[dict[str, Any]] = []
                next_ts: float | None = None
                for _ in range(8):  # cap = 8k events worst case, plenty
                    args: dict[str, Any] = {
                        "code": code,
                        "fightIDs": [fight_id],
                        "sourceID": actor_id,
                    }
                    if next_ts is not None:
                        args["startTime"] = next_ts
                    resp = await client.query(REPORT_PLAYER_RESOURCE_EVENTS, args)
                    page, next_ts = parse_player_resource_events_page(resp)
                    collected.extend(page)
                    if next_ts is None:
                        break
                extras["mana_recovery"] = aggregate_mana_recovery(
                    collected, fight_start_ms=fight_start_ms
                )
            except Exception:  # noqa: BLE001
                logger.exception("Mana recovery events fetch failed; continuing without")
                extras.setdefault(
                    "mana_recovery",
                    {"total_recovered_pct": 0.0, "recovery_sources": []},
                )
    finally:
        if own:
            await client.aclose()

    player.extras = extras
    await session.flush()
    # Reload casts so the prompt builder sees the rows we just inserted.
    if not has_casts:
        await session.refresh(player, attribute_names=["casts"])


def _avg(values: list[float | int]) -> float | None:
    valid = [float(v) for v in values if v is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _ilvl_context(player_ilvl: float | None, refs: list[dict[str, Any]]) -> dict[str, Any]:
    ref_ilvls = [r.get("item_level") for r in refs if r.get("item_level")]
    avg_top = _avg(ref_ilvls)
    delta = None
    if player_ilvl is not None and avg_top is not None:
        delta = float(player_ilvl) - avg_top
    return {
        "player_ilvl": player_ilvl,
        "top_logs_avg_ilvl": avg_top,
        "delta_vs_top_logs": delta,
        "note": (
            "Higher item level translates roughly linearly into more "
            "primary stat and weapon damage. When comparing absolute DPS/HPS "
            "between the player and top-log references, mentally adjust by "
            "this delta before drawing rotation/cooldown conclusions."
        ),
    }


async def _collect_localized_names(
    session: AsyncSession,
    *,
    locale: str,
    fight_summary: dict[str, Any],
    player_summary: dict[str, Any],
    casts: list[dict[str, Any]],
    gear: list[dict[str, Any]],
    references: list[dict[str, Any]],
) -> dict[str, str]:
    """Walk every nested dict that might contain a spell/item/encounter ID
    and ask the WoW data cache for the matching localised name."""
    spell_ids: set[int] = set()
    item_ids: set[int] = set()
    encounter_ids: set[int] = set()
    # Talent IDs from combatantInfo.talentTree are TraitNodeEntry IDs, NOT
    # SpellIDs — the namespaces collide (96173 hits "Egg Shell" instead of
    # "Festermight"). Resolve them against kind=talent which is the
    # pre-baked TraitNodeEntry → SpellName chain.
    talent_ids: set[int] = set()

    encounter_pairs: list[tuple[int, str]] = []
    if fight_summary.get("encounter_id"):
        encounter_pairs.append(
            (int(fight_summary["encounter_id"]), str(fight_summary.get("encounter_name") or ""))
        )

    for source in (player_summary.get("buffs") or [], player_summary.get("debuffs") or []):
        for entry in source:
            if entry.get("ability_id"):
                spell_ids.add(int(entry["ability_id"]))
    for entry in player_summary.get("damage_taken") or []:
        if entry.get("ability_id"):
            spell_ids.add(int(entry["ability_id"]))
    for tid in player_summary.get("talent_ids") or []:
        if tid:
            talent_ids.add(int(tid))
    for c in casts:
        if c.get("ability_id"):
            spell_ids.add(int(c["ability_id"]))
    for g in gear:
        if g.get("item_id"):
            item_ids.add(int(g["item_id"]))
    # Boss-cast timeline IDs are spell IDs (the boss is casting actual
    # spells), so they belong under kind=spell. Same goes for the
    # reference-detail boss_casts further down.
    for entry in fight_summary.get("boss_casts") or []:
        if entry.get("ability_id"):
            spell_ids.add(int(entry["ability_id"]))
    # Mana-recovery ability IDs (Mana Tea / Innervate / potion / etc.)
    # are real spells — the AI should be able to cite them by name.
    for entry in (player_summary.get("mana_recovery") or {}).get("recovery_sources") or []:
        if entry.get("ability_id"):
            spell_ids.add(int(entry["ability_id"]))

    for ref in references:
        detail = ref.get("detail") or {}
        for c in detail.get("casts") or []:
            if c.get("ability_id"):
                spell_ids.add(int(c["ability_id"]))
        for g in detail.get("gear") or []:
            if g.get("item_id"):
                item_ids.add(int(g["item_id"]))
        for source in (detail.get("buffs") or [], detail.get("debuffs") or []):
            for entry in source:
                if entry.get("ability_id"):
                    spell_ids.add(int(entry["ability_id"]))
        for entry in detail.get("damage_taken") or []:
            if entry.get("ability_id"):
                spell_ids.add(int(entry["ability_id"]))
        for tid in detail.get("talent_ids") or []:
            if tid:
                talent_ids.add(int(tid))
        for entry in detail.get("boss_casts") or []:
            if entry.get("ability_id"):
                spell_ids.add(int(entry["ability_id"]))

    names = await lookup_names(
        session,
        locale=locale,
        spell_ids=spell_ids,
        item_ids=item_ids,
        encounter_ids=set(),  # encounters resolved separately with EN-name fallback
        talent_ids=talent_ids,
    )
    if encounter_pairs:
        encounter_names = await resolve_encounter_names_with_fallback(
            session, locale=locale, encounters=encounter_pairs
        )
        for eid, name in encounter_names.items():
            names[f"encounter:{eid}"] = name
    return names


# ItemEffect.TriggerType → human label for the prompt.
_EFFECT_TRIGGER_LABEL = {0: "use", 1: "equip", 2: "proc"}
# Effect texts are tooltips with formatting variables ($s1, ${...}) — long
# outliers get truncated so 30+ items can't blow up the prompt budget.
_EFFECT_TEXT_MAX = 400


async def _collect_item_effect_context(
    session: AsyncSession,
    *,
    locale: str,
    gear: list[dict[str, Any]],
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the trinket-effect / set-bonus context for the prompt.

    Sources the ``itemfx`` / ``itemset`` / ``spelldesc`` rows that
    ``wow_data_service._import_item_effects`` mirrors from wago.tools, so
    the AI can compare what the player's trinkets and tier bonuses DO
    against the top logs' — instead of guessing from item names.

    Returns ``{"item_effects": {item_id: [{trigger, text}]},
    "set_bonuses": {"player": [...], "references": {rank: [...]}}}`` —
    empty maps when the effect tables haven't been imported yet, which
    the prompt builder treats as "section absent" (graceful downgrade).
    """
    from collections import Counter

    from app.models import WowLocalization

    player_item_ids = [int(g["item_id"]) for g in gear if g.get("item_id")]
    ref_item_ids: dict[int, list[int]] = {}
    for ref in references:
        detail = ref.get("detail") or {}
        ids = [int(g["item_id"]) for g in (detail.get("gear") or []) if g.get("item_id")]
        if ids:
            ref_item_ids[int(ref.get("rank") or 0)] = ids
    all_item_ids = set(player_item_ids)
    for ids in ref_item_ids.values():
        all_item_ids.update(ids)
    if not all_item_ids:
        return {}

    fx_rows = (
        await session.execute(
            select(WowLocalization.game_id, WowLocalization.extras).where(
                WowLocalization.kind == "itemfx",
                WowLocalization.locale == "xx",
                WowLocalization.game_id.in_(all_item_ids),
            )
        )
    ).all()
    if not fx_rows:
        return {}

    effects_of_item: dict[int, list[dict[str, int]]] = {}
    set_of_item: dict[int, int] = {}
    for game_id, extras in fx_rows:
        extras = extras or {}
        if extras.get("effects"):
            effects_of_item[int(game_id)] = extras["effects"]
        if extras.get("set_id"):
            set_of_item[int(game_id)] = int(extras["set_id"])

    # Localized set rows (name + bonus list), preferred locale with EN fallback.
    sets: dict[int, dict[str, Any]] = {}
    if set_of_item:
        set_rows = (
            await session.execute(
                select(
                    WowLocalization.game_id,
                    WowLocalization.locale,
                    WowLocalization.name,
                    WowLocalization.extras,
                ).where(
                    WowLocalization.kind == "itemset",
                    WowLocalization.game_id.in_(set(set_of_item.values())),
                    WowLocalization.locale.in_([locale, "en"]),
                )
            )
        ).all()
        for game_id, row_locale, name, extras in sorted(
            set_rows, key=lambda r: r[1] != "en"  # EN first so `locale` overwrites
        ):
            sets[int(game_id)] = {"name": name, "bonuses": (extras or {}).get("bonuses") or []}

    spell_ids: set[int] = set()
    for effs in effects_of_item.values():
        spell_ids.update(int(e["spell_id"]) for e in effs if e.get("spell_id"))
    for srow in sets.values():
        spell_ids.update(int(b["spell_id"]) for b in srow["bonuses"] if b.get("spell_id"))

    descs: dict[int, str] = {}
    if spell_ids:
        desc_rows = (
            await session.execute(
                select(
                    WowLocalization.game_id, WowLocalization.locale, WowLocalization.name
                ).where(
                    WowLocalization.kind == "spelldesc",
                    WowLocalization.game_id.in_(spell_ids),
                    WowLocalization.locale.in_([locale, "en"]),
                )
            )
        ).all()
        for game_id, row_locale, name in sorted(desc_rows, key=lambda r: r[1] != "en"):
            descs[int(game_id)] = name[:_EFFECT_TEXT_MAX]

    item_effects: dict[str, list[dict[str, Any]]] = {}
    for item_id, effs in effects_of_item.items():
        entries = [
            {
                "trigger": _EFFECT_TRIGGER_LABEL.get(int(e.get("trigger") or 0), "other"),
                "text": descs[int(e["spell_id"])],
            }
            for e in effs
            if int(e.get("spell_id") or 0) in descs
        ]
        if entries:
            item_effects[str(item_id)] = entries

    def _set_summary(item_ids: list[int]) -> list[dict[str, Any]]:
        counts = Counter(set_of_item[i] for i in item_ids if i in set_of_item)
        out = []
        for set_id, n in counts.items():
            srow = sets.get(set_id)
            if not srow:
                continue
            bonuses = [
                {
                    "pieces_required": int(b.get("threshold") or 0),
                    "active": n >= int(b.get("threshold") or 0),
                    # ChrSpecID 0 = all specs. We can't map our spec slugs
                    # to Blizzard spec IDs, so spec-specific bonus texts
                    # are all included — the model picks the matching one
                    # from the ability names in the text.
                    "spec_specific": bool(b.get("spec_id")),
                    "text": descs.get(int(b.get("spell_id") or 0)),
                }
                for b in srow["bonuses"]
            ]
            bonuses = [b for b in bonuses if b["text"]]
            if bonuses:
                out.append(
                    {"set_name": srow["name"], "pieces_equipped": n, "bonuses": bonuses}
                )
        return out

    set_bonuses: dict[str, Any] = {}
    player_sets = _set_summary(player_item_ids)
    if player_sets:
        set_bonuses["player"] = player_sets
    ref_sets = {
        str(rank): summary
        for rank, ids in ref_item_ids.items()
        if (summary := _set_summary(ids))
    }
    if ref_sets:
        set_bonuses["top_log_references_by_rank"] = ref_sets

    out: dict[str, Any] = {}
    if item_effects:
        out["item_effects"] = item_effects
    if set_bonuses:
        out["set_bonuses"] = set_bonuses
    return out


async def _collect_talent_spell_ids(
    session: AsyncSession,
    *,
    player_summary: dict[str, Any],
    references: list[dict[str, Any]],
) -> dict[str, int]:
    """Build a ``{TraitNodeEntry.ID: SpellID}`` map for every talent ID
    that appears in the player_summary or any top-log reference detail.

    Frontend uses this to turn ``[Label](talent:<id>)`` inline markdown
    links into real Wowhead spell links — the TraitNodeEntry IDs WCL
    ships collide with old MoP-era spell IDs, so we have to resolve
    through ``wow_localizations.extras['spell_id']`` (populated by
    ``_import_talents``) before linking. Missing entries silently fall
    back to bold text on the frontend.
    """
    talent_ids: set[int] = set()
    for tid in player_summary.get("talent_ids") or []:
        if tid:
            talent_ids.add(int(tid))
    for ref in references:
        for tid in (ref.get("detail") or {}).get("talent_ids") or []:
            if tid:
                talent_ids.add(int(tid))
    if not talent_ids:
        return {}
    # One row per talent (en locale — extras['spell_id'] is locale-
    # agnostic). UNIQUE constraint on (kind, game_id, locale) means
    # we only ever get one row per ID.
    rows = (
        await session.execute(
            select(WowLocalization.game_id, WowLocalization.extras)
            .where(WowLocalization.kind == "talent")
            .where(WowLocalization.locale == "en")
            .where(WowLocalization.game_id.in_(talent_ids))
        )
    ).all()
    out: dict[str, int] = {}
    for game_id, extras in rows:
        spell_id = (extras or {}).get("spell_id")
        if not spell_id:
            continue
        try:
            out[str(int(game_id))] = int(spell_id)
        except (TypeError, ValueError):
            continue
    return out


async def request_analysis(
    session: AsyncSession,
    *,
    report_id: uuid.UUID,
    fight_id: uuid.UUID,
    player_id: uuid.UUID,
    requested_by_id: uuid.UUID | None,
    locale: str,
    provider: AiProvider | None = None,
    analysis_id: uuid.UUID | None = None,
) -> Analysis:
    """Create + run an analysis and return it.

    When ``analysis_id`` is supplied (worker path), we mutate that pre-
    existing pending row instead of creating a new one. Otherwise the legacy
    path creates a fresh row — kept so nothing else has to change.
    """
    stmt = (
        select(ReportPlayer)
        .where(ReportPlayer.id == player_id)
        .options(selectinload(ReportPlayer.casts), selectinload(ReportPlayer.gear))
    )
    player = (await session.execute(stmt)).scalar_one_or_none()
    if not player:
        raise NotFoundError("Player not found.")
    fight = (
        await session.execute(select(ReportFight).where(ReportFight.id == fight_id))
    ).scalar_one_or_none()
    if not fight or fight.id != player.fight_id:
        raise NotFoundError("Fight does not match the supplied player.")
    report = (
        await session.execute(select(Report).where(Report.id == report_id))
    ).scalar_one_or_none()
    if not report or report.id != fight.report_id:
        raise NotFoundError("Report mismatch.")

    # ----- Auto-refresh stale data before running the analysis ---------------
    # When the code shipped a new field since this report (or its cached
    # top-log references) was imported, the analyser would otherwise read
    # missing fields and the AI prompt would silently lose features.
    # ``REPORT_DATA_VERSION`` / ``TOP_LOG_DETAIL_VERSION`` are bumped by the
    # importer code when the shape changes; we compare against the stored
    # ``_v`` stamp and re-fetch when behind. Failures are logged but never
    # blocking — we proceed with whatever data we have, the AI degrades
    # gracefully on missing fields.
    from app.services.report_service import (
        REPORT_DATA_VERSION as _RDV,
        report_data_is_current,
        run_report_import,
    )

    if not await report_data_is_current(session, report.id):
        logger.info(
            "report %s data older than v%d — auto-refreshing before analysis",
            report.id,
            _RDV,
        )
        try:
            async with WclClient() as wcl:
                await run_report_import(session, report_id=report.id, wcl_client=wcl)
            await session.commit()
            # Re-load fight + player after the import wiped + re-inserted them.
            fight = (
                await session.execute(
                    select(ReportFight).where(
                        ReportFight.report_id == report.id,
                        ReportFight.fight_id == fight.fight_id,
                    )
                )
            ).scalar_one_or_none()
            player = (
                await session.execute(
                    select(ReportPlayer)
                    .where(
                        ReportPlayer.fight_id == fight.id if fight else False,
                        ReportPlayer.actor_id == player.actor_id,
                    )
                    .options(
                        selectinload(ReportPlayer.casts),
                        selectinload(ReportPlayer.gear),
                    )
                )
            ).scalar_one_or_none()
            if not fight or not player:
                raise UpstreamError(
                    "Report data was refreshed but the original fight/player "
                    "rows could not be re-located — the underlying WCL log "
                    "may have changed."
                )
        except UpstreamError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "Auto-refresh of report %s failed — continuing with stale data",
                report.id,
            )

    role_focus = "healer" if player.role == "healer" else ("tank" if player.role == "tank" else "dps")

    # Lazy-fetch extras (buffs / debuffs / damage_taken) from WCL the first time
    # the player is analysed. Cached in ``player.extras`` thereafter.
    try:
        await _enrich_player_with_aura_and_damage_taken(
            session,
            report=report,
            fight=fight,
            player=player,
            requested_by_id=requested_by_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Player enrichment failed; continuing with what we have")

    # Raid fights compare against top logs of their own difficulty
    # (Heroic run → Heroic references) — crucial at season start, when
    # Mythic has no public logs yet. M+ fights (keystone_level set) keep
    # the difficulty-agnostic behaviour.
    ref_difficulty = fight.difficulty if fight.keystone_level is None else None

    references = await _fetch_top_log_references(
        session,
        spec_slug=player.spec_slug,
        encounter_id=fight.encounter_id,
        role=role_focus,
        difficulty=ref_difficulty,
    )

    # If no reference data is cached for this (spec, encounter, metric),
    # synchronously fetch it now and re-load. The first analysis on a new
    # boss is therefore slower (~1-3 min extra) but every analysis has
    # concrete top-log deltas to compare against.
    if not references and fight.encounter_id and player.spec_slug:
        metric_for_seed = "hps" if role_focus == "healer" else "dps"
        seeded = await _ensure_references(
            session,
            spec_slug=player.spec_slug,
            encounter_id=int(fight.encounter_id),
            metric=metric_for_seed,
            difficulty=ref_difficulty,
        )
        if seeded:
            references = await _fetch_top_log_references(
                session,
                spec_slug=player.spec_slug,
                encounter_id=fight.encounter_id,
                role=role_focus,
                difficulty=ref_difficulty,
            )

    if not references:
        # Without references the AI's advice would be hand-waving rather
        # than concrete deltas — refuse the request explicitly so the user
        # knows to retry once WCL has data, instead of getting a generic
        # critique that they might mistake for a real comparison.
        raise NoTopLogsError(
            "No public Warcraft Logs entries are available for this spec on "
            "this boss yet. Try again in a few days once more public logs "
            "have been uploaded.",
        )

    fight_extras = fight.extras or {}
    fight_summary = {
        "encounter_id": fight.encounter_id,
        "encounter_name": fight.name,
        "difficulty": fight.difficulty,
        "keystone_level": fight.keystone_level,
        "is_kill": fight.is_kill,
        "duration_ms": fight.duration_ms,
        "boss_percentage": fight.boss_percentage,
        "phase_transitions": fight_extras.get("phase_transitions") or [],
        # Per-ability boss-cast timeline (fight-relative seconds since
        # fight start). One entry per top-N boss ability with the cast
        # count and a sample of timestamps. Lets the AI infer the cycle
        # period of each ability and judge whether the player's defensive
        # cooldown frequency matched the boss-pressure cadence.
        "boss_casts": fight_extras.get("boss_casts") or [],
    }
    player_extras = player.extras or {}
    parse_metrics = player_extras.get("parse_metrics") or {}
    player_summary = {
        "name": player.name,
        "server": player.server,
        "class": player.class_slug,
        "spec": player.spec_slug,
        "role": player.role,
        "item_level": player.item_level,
        "dps": player.dps,
        "hps": player.hps,
        "damage_done": player.damage_done,
        "healing_done": player.healing_done,
        "deaths": player.deaths,
        "talents_loadout": player.talents_loadout,
        "talent_ids": player_extras.get("talent_ids") or [],
        # Stats snapshot from combatantInfo: {Strength|Agility|Intellect,
        # Stamina, Mastery, Crit, Haste, Versatility, Speed, Leech,
        # Avoidance, ...}. Peak (max) values, in armoury units. Empty {}
        # when WCL didn't include combatantInfo for this log.
        "stats": player_extras.get("stats") or {},
        "buffs": player_extras.get("buffs") or [],
        "debuffs": player_extras.get("debuffs") or [],
        "damage_taken": player_extras.get("damage_taken") or [],
        # Healer-only: explicit mana-grant timeline (Mana Tea / Innervate /
        # potion procs / buff-driven mana restoration). WCL's public API
        # does NOT expose absolute mana levels or cast costs — only these
        # explicit grants are observable. The AI combines this with the
        # player's cast count + class-spell knowledge from its training
        # to estimate whether mana pressure was an actual constraint.
        # Empty / zero on non-healers.
        "mana_recovery": player_extras.get("mana_recovery") or {
            "total_recovered_pct": 0.0,
            "recovery_sources": [],
        },
        # WCL parse percentile (vs all public logs) and ilvl-bracket
        # percentile (vs same-gear-bracket logs, gear-normalised). Both 0-100
        # (higher=better) or null when WCL has no ranking data for this
        # fight yet. Field names match WCL UI columns "Parse %" / "iLvl %".
        "parse_percent": parse_metrics.get("rank_percent"),
        "ilvl_percent": parse_metrics.get("ilvl_percent"),
        "rank": parse_metrics.get("rank"),
        "out_of": parse_metrics.get("out_of"),
    }
    # Fight-duration normalisation. Top-log fights are usually shorter than
    # the user's (kills vs. wipes / progression), so absolute cast counts
    # mislead the AI. We pre-compute per_minute values so the prompt makes
    # apples-to-apples comparison trivial.
    fight_minutes = max(
        0.001, (fight.duration_ms or 0) / 60_000
    )
    fight_summary["duration_minutes"] = round(fight_minutes, 2)

    # Player-active-time normalisation. On a kill, ``active_time_ms`` equals
    # the fight duration (player lived through it). On a wipe where the
    # player died early, it's the time they were actually alive and
    # contributing. We use the active time as the denominator for the
    # player's own per-minute rates so a death at 1:30 in a 6:00 wipe
    # doesn't make the rotation density look 4x too low — we'd be blaming
    # the player for casts they couldn't do because they were dead.
    # Falls back to fight_minutes when WCL didn't ship ``activeTime`` (very
    # old fights, parser couldn't extract). Top-log refs are always kills
    # so their duration === their active time — they keep using
    # ``duration_minutes`` as before.
    active_time_ms = int(player_extras.get("active_time_ms") or 0)
    active_minutes = (
        max(0.001, active_time_ms / 60_000) if active_time_ms else fight_minutes
    )
    player_summary["active_time_minutes"] = round(active_minutes, 2)

    def _annotate_casts(items: list[dict[str, Any]], minutes: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in items or []:
            casts = int(c.get("casts") or 0)
            total = int(c.get("total") or 0)
            out.append(
                {
                    **c,
                    "casts_per_minute": round(casts / minutes, 2) if minutes else 0,
                    "total_per_minute": round(total / minutes, 0) if minutes else 0,
                }
            )
        return out

    def _annotate_damage_taken(
        items: list[dict[str, Any]], minutes: float
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in items or []:
            hits = int(e.get("hits") or 0)
            total = int(e.get("total") or 0)
            out.append(
                {
                    **e,
                    "hits_per_minute": round(hits / minutes, 2) if minutes else 0,
                    "total_per_minute": round(total / minutes, 0) if minutes else 0,
                }
            )
        return out

    # Per-minute normalisation uses the player's active time, not the
    # fight's total duration — see comment above next to ``active_minutes``.
    casts = _annotate_casts(
        [
            {
                "ability_id": c.ability_id,
                "name": c.ability_name,
                "casts": c.casts,
                "hits": c.hits,
                "total": c.total,
            }
            for c in player.casts
        ],
        active_minutes,
    )
    gear = [
        {
            "slot": g.slot,
            "item_id": g.item_id,
            "item_level": g.item_level,
            "name": g.name,
            "enchant_id": g.enchant_id,
            "gem_ids": g.gem_ids,
        }
        for g in player.gear
    ]

    # Same active-time denominator for damage_taken — the player can only
    # eat mechanics while alive, so the rate of avoidable damage taken is
    # honestly measured against the time they were up.
    player_summary["damage_taken"] = _annotate_damage_taken(
        player_summary.get("damage_taken") or [], active_minutes
    )
    # ``duration_minutes`` on the player block stays as fight duration so
    # the AI can still see the wipe length; the prompt is told explicitly
    # to use ``active_time_minutes`` for per-minute math.
    player_summary["duration_minutes"] = round(fight_minutes, 2)

    annotated_refs: list[dict[str, Any]] = []
    for ref in references:
        ref_duration_ms = ref.get("duration_ms") or 0
        ref_minutes = (
            max(0.001, ref_duration_ms / 60_000) if ref_duration_ms else fight_minutes
        )
        ref = {**ref, "duration_minutes": round(ref_minutes, 2)}
        detail = ref.get("detail") or {}
        if detail:
            ref["detail"] = {
                **detail,
                "duration_minutes": round(ref_minutes, 2),
                "casts": _annotate_casts(detail.get("casts") or [], ref_minutes),
                "damage_taken": _annotate_damage_taken(
                    detail.get("damage_taken") or [], ref_minutes
                ),
            }
        annotated_refs.append(ref)
    references = annotated_refs

    # Resolve every spell/item/encounter ID that appears in the prompt to its
    # localised name so the AI doesn't have to translate (it usually gets the
    # German names wrong, the local Qwen finetune especially).
    localized_names = await _collect_localized_names(
        session,
        locale=locale,
        fight_summary=fight_summary,
        player_summary=player_summary,
        casts=casts,
        gear=gear,
        references=references,
    )
    talent_spell_ids = await _collect_talent_spell_ids(
        session, player_summary=player_summary, references=references
    )
    item_effect_context = await _collect_item_effect_context(
        session, locale=locale, gear=gear, references=references
    )

    # If the row already exists (worker path), it tells us whether BYOK was
    # requested. New rows fall through to the app-wide path.
    existing_row: Analysis | None = None
    if analysis_id is not None:
        existing_row = (
            await session.execute(select(Analysis).where(Analysis.id == analysis_id))
        ).scalar_one_or_none()
        if existing_row is None:
            raise NotFoundError("Analysis row was deleted before the worker picked it up.")

    use_byok = bool(existing_row and existing_row.uses_byok)

    if use_byok:
        # BYOK path: build provider from the requesting user's stored config.
        from app.services.user_ai_service import (
            get_config as _get_user_ai_cfg,
            provider_for_user_config,
        )

        user_cfg = (
            await _get_user_ai_cfg(session, requested_by_id) if requested_by_id else None
        )
        if user_cfg is None:
            raise UpstreamError(
                "BYOK requested but the user has no AI configuration on file."
            )
        used_provider = provider or provider_for_user_config(user_cfg)
        chosen_provider = f"byok:{user_cfg.provider_type}"
        chosen_model = user_cfg.model
    else:
        chosen_provider = await _resolve_provider_choice(session)
        if chosen_provider == "disabled":
            raise UpstreamError(
                "App-wide AI analysis is disabled by the admin. Configure your "
                "own AI provider in your profile and try again."
            )
        chosen_model = await _resolve_model(session, chosen_provider)
        # Only OpenAI listens to ``reasoning_effort`` — fetch it cheaply and
        # let ``_provider_for`` decide whether to plumb it through.
        admin_effort = (
            await _resolve_openai_reasoning_effort(session)
            if chosen_provider == "openai"
            else None
        )
        used_provider = provider or _provider_for(
            chosen_provider, openai_reasoning_effort=admin_effort
        )

    if existing_row is not None:
        analysis = existing_row
        analysis.status = AnalysisStatus.running
        analysis.provider = chosen_provider
        analysis.model = chosen_model
    else:
        analysis = Analysis(
            requested_by_id=requested_by_id,
            report_id=report.id,
            fight_id=fight.id,
            player_id=player.id,
            locale=locale,
            status=AnalysisStatus.running,
            provider=chosen_provider,
            model=chosen_model,
        )
        session.add(analysis)
    await session.flush()
    user_prompt = build_user_prompt(
        locale=locale if locale in ("en", "de") else "en",  # type: ignore[arg-type]
        role_focus=role_focus,  # type: ignore[arg-type]
        fight_summary=fight_summary,
        player_summary=player_summary,
        casts=casts,
        gear=gear,
        top_log_references=references,
        ilvl_context=_ilvl_context(player.item_level, references),
        localized_names=localized_names,
        item_effect_context=item_effect_context,
    )
    sys_prompt = system_prompt_for("de" if locale == "de" else "en")

    try:
        response: AiResponse = await used_provider.generate_structured(
            system_prompt=sys_prompt, user_prompt=user_prompt, model=chosen_model
        )
        analysis.status = AnalysisStatus.succeeded
        analysis.summary = response.text
        # Persist the localised name lookup we built for the prompt — the
        # frontend re-uses it to render friendly labels on spell/item chips
        # when ad blockers stop the wowhead tooltip script from doing it.
        # ``_parse_metrics`` is the WCL parse-percentile snapshot for this
        # player+fight; the frontend renders it next to the overall score.
        analysis.structured = {
            **(response.structured or {}),
            "_localized_names": localized_names,
            # ``[Label](talent:<id>)`` markdown links in the AI's prose
            # carry TraitNodeEntry IDs (different namespace from spell
            # IDs). Frontend resolves to the underlying spell ID via
            # this map before linking to Wowhead.
            "_talent_spell_ids": talent_spell_ids,
            "_parse_metrics": {
                "parse_percent": parse_metrics.get("rank_percent"),
                "ilvl_percent": parse_metrics.get("ilvl_percent"),
                "rank": parse_metrics.get("rank"),
                "out_of": parse_metrics.get("out_of"),
            },
        }
        analysis.prompt_tokens = response.prompt_tokens
        analysis.completion_tokens = response.completion_tokens
        analysis.model = response.model
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI analysis failed")
        analysis.status = AnalysisStatus.failed
        analysis.error = str(exc)
    return analysis
