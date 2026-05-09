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
)
from app.services.ai.anthropic_provider import AnthropicProvider
from app.services.ai.base import AiProvider, AiResponse
from app.services.ai.openai_provider import OpenAiCompatibleProvider
from app.services.ai.prompts import build_user_prompt, system_prompt_for
from app.services.wcl.client import WclClient
from app.services.wcl.parser import (
    parse_aura_table,
    parse_casts_table,
    parse_damage_taken_table,
    parse_report_rankings_for_player,
)
from app.services.wcl.queries import (
    REPORT_BUFFS_FOR_PLAYER,
    REPORT_CASTS,
    REPORT_DAMAGE_TAKEN_FOR_PLAYER,
    REPORT_DEBUFFS_BY_PLAYER,
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


def _provider_for(choice: str) -> AiProvider:
    if choice == "anthropic":
        return AnthropicProvider()
    if choice == "openai":
        return OpenAiCompatibleProvider(mode="openai")
    if choice == "local":
        return OpenAiCompatibleProvider(mode="local")
    raise UpstreamError(f"Unsupported AI provider: {choice}")


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
                        wcl_client=wcl,
                    )
                # context manager commits the seed_session transaction
        logger.info(
            "sync-seed for spec=%s encounter=%s metric=%s → %s rows",
            spec_slug,
            encounter_id,
            metric,
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
    session: AsyncSession, *, spec_slug: str, encounter_id: int | None, role: str, limit: int = 5
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
    rows = (await session.execute(stmt)).scalars().all()
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
                # Full detail (casts, gear, buffs, debuffs, damage_taken, talents)
                # for top performers — empty dict if we never fetched it.
                "detail": {
                    "talents_loadout": detail.get("talents_loadout"),
                    "talent_ids": detail.get("talent_ids") or [],
                    "casts": detail.get("casts") or [],
                    "gear": detail.get("gear") or [],
                    "buffs": detail.get("buffs") or [],
                    "debuffs": detail.get("debuffs") or [],
                    "damage_taken": detail.get("damage_taken") or [],
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
    if has_auras and has_casts and has_parse_metrics:
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
            spell_ids.add(int(tid))
    for c in casts:
        if c.get("ability_id"):
            spell_ids.add(int(c["ability_id"]))
    for g in gear:
        if g.get("item_id"):
            item_ids.add(int(g["item_id"]))

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
                spell_ids.add(int(tid))

    names = await lookup_names(
        session,
        locale=locale,
        spell_ids=spell_ids,
        item_ids=item_ids,
        encounter_ids=set(),  # encounters resolved separately with EN-name fallback
    )
    if encounter_pairs:
        encounter_names = await resolve_encounter_names_with_fallback(
            session, locale=locale, encounters=encounter_pairs
        )
        for eid, name in encounter_names.items():
            names[f"encounter:{eid}"] = name
    return names


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

    references = await _fetch_top_log_references(
        session, spec_slug=player.spec_slug, encounter_id=fight.encounter_id, role=role_focus
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
        )
        if seeded:
            references = await _fetch_top_log_references(
                session,
                spec_slug=player.spec_slug,
                encounter_id=fight.encounter_id,
                role=role_focus,
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
        "buffs": player_extras.get("buffs") or [],
        "debuffs": player_extras.get("debuffs") or [],
        "damage_taken": player_extras.get("damage_taken") or [],
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
        fight_minutes,
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

    # Apply same normalisation to the player extras we just enriched, and to
    # each top-log reference's detail data (each ref has its own duration).
    player_summary["damage_taken"] = _annotate_damage_taken(
        player_summary.get("damage_taken") or [], fight_minutes
    )
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
        used_provider = provider or _provider_for(chosen_provider)

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
