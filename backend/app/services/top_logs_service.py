"""Fetch + cache the WCL top logs per spec/encounter, with rich detail.

For every (spec, encounter, metric) we:

1. Fetch the world ranking page (filtered to ``settings.top_logs_region`` and,
   for raid metrics, ``settings.top_logs_raid_difficulty``).
2. For each candidate ranking, fetch the report's player-details so we can:
   - count healers (HPS rankings drop logs below ``top_logs_min_healers``);
   - know the composition (useful context for the AI prompt).
3. For the top ``top_logs_detail_count`` survivors we *also* fetch their casts,
   gear, buffs, debuffs and damage-taken so the analyzer prompt can compare
   the user concretely against them.

All fetches are throttled by tenacity retries inside ``WclClient``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import GameSpec, Role, TopLog
from app.services.wcl.client import WclClient
from app.services.report_service import fetch_boss_cast_timeline
from app.services.wcl.parser import (
    composition_from_player_details,
    parse_aura_table,
    parse_casts_table,
    parse_damage_taken_table,
    parse_encounter_rankings,
    parse_gear_from_player_details,
    parse_player_details,
)
from app.services.wcl.queries import (
    ENCOUNTER_RANKINGS,
    REPORT_BUFFS_FOR_PLAYER,
    REPORT_CASTS,
    REPORT_DAMAGE_TAKEN_FOR_PLAYER,
    REPORT_DEBUFFS_BY_PLAYER,
    REPORT_PLAYER_DETAILS,
)

logger = logging.getLogger(__name__)

# Bumped whenever ``TopLog.detail_payload`` shape gains a new field the
# analyser depends on. The incremental refresh path snapshots existing
# detail rows for cache-reuse — stale rows (``_v`` below this constant,
# or missing entirely) are skipped from the snapshot so they get re-
# fetched freshly. The analyser also filters stale rows out of its
# reference query, so a fully-stale (spec, encounter, metric) bucket
# triggers a clean re-seed instead of feeding the AI old detail.
#
# History:
#   v1: initial detail shape (casts / gear / buffs / debuffs / damage_taken).
#   v2 (v0.3.0): added stats, talent_ids, boss_casts. Phase-transition
#                shape on the parent report is also a v3 thing but not
#                duplicated into detail_payload, so v2 stays the right
#                stamp.
TOP_LOG_DETAIL_VERSION = 2


def _metric_for_role(role: Role) -> str:
    if role == Role.healer:
        return "hps"
    return "dps"


def _wcl_class_name(class_slug: str) -> str:
    return "".join(word.capitalize() for word in class_slug.split("_"))


def _wcl_spec_name(name_en: str) -> str:
    return "".join(word.capitalize() for word in name_en.split())


async def refresh_top_logs_for_spec_encounter(
    session: AsyncSession,
    *,
    spec: GameSpec,
    encounter_id: int,
    encounter_name: str | None = None,
    metric: str | None = None,
    limit: int | None = None,
    detail_count: int | None = None,
    is_raid: bool = True,
    difficulty: int | None = None,
    wcl_client: WclClient | None = None,
    incremental: bool = True,
) -> list[TopLog]:
    """Refresh the top-log cache for a (spec, encounter, metric, difficulty).

    By default this is *incremental*: detail data (casts/gear/buffs/etc.) is
    only re-fetched from WCL for entries that weren't already in the cache.
    For entries we already had, the existing ``detail_payload`` is reused
    even if their rank shifted slightly. Logs that fell out of the top-N
    are dropped, new entries get fresh detail fetched, lasting WCL API
    budget where it matters. Pass ``incremental=False`` to force a full
    re-fetch.

    ``difficulty`` overrides the .env-wide ``TOP_LOGS_RAID_DIFFICULTY``
    for raid encounters — the analyzer passes the difficulty of the
    user's own fight so a Heroic log is compared against Heroic top logs
    (and works at season start, when Mythic has no public entries yet).
    The cache is scoped per difficulty: rows for different difficulties
    of the same encounter coexist and refreshes only replace their own
    difficulty's rows. ``None`` keeps the settings default.
    """
    metric = metric or _metric_for_role(spec.role)
    effective_difficulty: int | None = (
        difficulty
        if difficulty is not None
        else (settings.top_logs_raid_difficulty if is_raid else None)
    )
    if not is_raid:
        effective_difficulty = None
    limit = limit or settings.top_logs_limit
    detail_count = detail_count if detail_count is not None else settings.top_logs_detail_count
    own = wcl_client is None
    client = wcl_client or WclClient()

    # Snapshot the cache so we can reuse detail data for stable entries.
    cached_detail: dict[tuple[str, int], dict[str, Any]] = {}
    if incremental:
        existing_rows = (
            await session.execute(
                select(TopLog).where(
                    TopLog.spec_slug == spec.slug,
                    TopLog.encounter_id == encounter_id,
                    TopLog.metric == metric,
                    TopLog.difficulty == effective_difficulty,
                )
            )
        ).scalars().all()
        for r in existing_rows:
            detail = r.detail_payload
            if not detail:
                continue
            # Only reuse detail blobs written by the current code shape.
            # Stale ones (``_v`` below the current ``TOP_LOG_DETAIL_VERSION``
            # — or missing entirely, which counts as version 0) get dropped
            # from the cache so the fetch loop below re-pulls them, and
            # the row gets re-inserted with fresh data + current version.
            if int(detail.get("_v") or 0) < TOP_LOG_DETAIL_VERSION:
                continue
            cached_detail[(r.wcl_report_code, int(r.wcl_fight_id))] = detail

    try:
        spec_name = _wcl_spec_name(spec.name_en)
        class_name = _wcl_class_name(spec.class_slug)
        rankings_args: dict[str, Any] = {
            "encounterID": encounter_id,
            "specName": spec_name,
            "className": class_name,
            "metric": metric,
            "page": 1,
            "partition": None,
            "serverRegion": settings.top_logs_region or None,
            "difficulty": effective_difficulty,
        }
        payload = await client.query(ENCOUNTER_RANKINGS, rankings_args)
        rankings = parse_encounter_rankings(payload)
        if encounter_name:
            for r in rankings:
                r["encounter_name"] = encounter_name

        # We may need to drop rankings that don't pass the healer-count filter
        # (HPS only). Pull a buffer (up to 2× limit) so we have room to filter.
        candidates = rankings[: max(limit * 2, limit + detail_count)]

        kept: list[dict[str, Any]] = []
        for cand in candidates:
            if len(kept) >= limit:
                break
            comp = await _composition_for(client, cand["wcl_report_code"], cand["wcl_fight_id"])
            cand["composition"] = comp
            if metric == "hps" and comp.get("healers", 0) < settings.top_logs_min_healers:
                logger.debug(
                    "skipping rank=%s (healers=%s < %s)",
                    cand["rank"],
                    comp.get("healers"),
                    settings.top_logs_min_healers,
                )
                continue
            kept.append(cand)

        # Re-rank consecutively after filtering so the UI shows 1..N.
        for new_rank, c in enumerate(kept, start=1):
            c["rank"] = new_rank

        # Slide-down detail fetch: walk ``kept`` from rank 1 onward and grab
        # detail until we have ``detail_count`` *valid* entries. If a fetch
        # fails, returns None, or doesn't match the player (Anonymous, private
        # report, etc.), the slot rolls forward to the next ranking. Reuses
        # cached detail for entries already in the DB (matched by
        # report_code + fight_id, which are stable across leaderboard shuffles).
        skipped_reuse = 0
        fetched_new = 0
        slide_skips = 0
        valid_count = 0
        for c in kept:
            if valid_count >= detail_count:
                break
            key = (c["wcl_report_code"], int(c["wcl_fight_id"]))
            cached = cached_detail.get(key)
            if cached is not None:
                c["detail"] = cached
                skipped_reuse += 1
                valid_count += 1
                continue
            try:
                detail = await _fetch_detail_for_top_log(client, c, metric=metric)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Detail fetch failed for code=%s fight=%s — sliding to next rank",
                    c["wcl_report_code"],
                    c["wcl_fight_id"],
                )
                detail = None
            if detail is None:
                # Mark explicitly so the row gets stored with SQL NULL and
                # so we don't accidentally count it as a valid reference.
                c["detail"] = None
                slide_skips += 1
                continue
            c["detail"] = detail
            fetched_new += 1
            valid_count += 1

        # Any remaining entries (ranks beyond the detail-count slice we
        # just filled, OR entries we explicitly skipped above) stay in the
        # leaderboard but without detail data. Reuse cached detail when
        # available so we don't lose data for free, but never fetch new.
        for c in kept:
            if "detail" in c:
                continue
            key = (c["wcl_report_code"], int(c["wcl_fight_id"]))
            c["detail"] = cached_detail.get(key)  # may be None

        if incremental:
            logger.info(
                "incremental refresh: spec=%s metric=%s reused=%s new_fetches=%s slide_skips=%s",
                spec.slug,
                metric,
                skipped_reuse,
                fetched_new,
                slide_skips,
            )
    finally:
        if own:
            await client.aclose()

    await session.execute(
        delete(TopLog).where(
            TopLog.spec_slug == spec.slug,
            TopLog.encounter_id == encounter_id,
            TopLog.metric == metric,
            # Scoped per difficulty so e.g. Heroic and Mythic caches for
            # the same encounter can coexist — a refresh only replaces
            # its own difficulty's rows.
            TopLog.difficulty == effective_difficulty,
        )
    )

    rows: list[TopLog] = []
    now = datetime.now(UTC)
    for r in kept:
        detail = r.get("detail")
        rows.append(
            TopLog(
                spec_slug=spec.slug,
                encounter_id=r["encounter_id"],
                encounter_name=r["encounter_name"],
                difficulty=effective_difficulty,
                metric=metric,
                rank=r["rank"],
                amount=r["amount"],
                item_level=r.get("item_level"),
                duration_ms=r.get("duration_ms"),
                character_name=r["character_name"],
                server=r["server"],
                region=r["region"],
                wcl_report_code=r["wcl_report_code"],
                wcl_fight_id=r["wcl_fight_id"],
                recorded_at=now,
                payload={
                    "raw": r.get("raw") or {},
                    "composition": r.get("composition") or {},
                },
                detail_payload=detail,
            )
        )
    session.add_all(rows)
    await session.flush()
    logger.info(
        "top-logs refresh: spec=%s encounter=%s metric=%s kept=%s detail=%s",
        spec.slug,
        encounter_id,
        metric,
        len(kept),
        sum(1 for r in kept if r.get("detail")),
    )
    return rows


async def _composition_for(client: WclClient, code: str, fight_id: int) -> dict[str, int]:
    payload = await client.query(REPORT_PLAYER_DETAILS, {"code": code, "fightIDs": [fight_id]})
    return composition_from_player_details(payload)


async def _fetch_detail_for_top_log(
    client: WclClient, ranking: dict[str, Any], *, metric: str
) -> dict[str, Any] | None:
    """Pull the matching player's casts/gear/buffs/(debuffs)/damage-taken/talents."""
    code = ranking["wcl_report_code"]
    fight_id = ranking["wcl_fight_id"]
    if not code or not fight_id:
        return None

    pd_payload = await client.query(REPORT_PLAYER_DETAILS, {"code": code, "fightIDs": [fight_id]})
    players = parse_player_details(pd_payload)
    if not players:
        return None
    target = _match_player(players, ranking)
    if target is None:
        return None
    actor_id = target["actor_id"]

    # Pull the fight's report-relative start offset from the same payload —
    # we extended REPORT_PLAYER_DETAILS to include ``fights{startTime}`` so
    # the boss-cast timeline below can convert event timestamps into
    # fight-relative seconds without a separate WCL roundtrip.
    fight_start_ms = 0
    pd_report = (pd_payload.get("reportData") or {}).get("report") or {}
    for fmeta in pd_report.get("fights") or []:
        try:
            if int(fmeta.get("id", -1)) == int(fight_id):
                fight_start_ms = int(fmeta.get("startTime") or 0)
                break
        except (TypeError, ValueError):
            continue

    boss_casts = await fetch_boss_cast_timeline(
        client, code=code, fight_id=int(fight_id), fight_start_ms=fight_start_ms
    )

    casts_payload = await client.query(
        REPORT_CASTS, {"code": code, "fightIDs": [fight_id], "sourceID": actor_id}
    )
    casts = parse_casts_table(casts_payload)

    buffs_payload = await client.query(
        REPORT_BUFFS_FOR_PLAYER, {"code": code, "fightIDs": [fight_id], "sourceID": actor_id}
    )
    buffs = parse_aura_table(buffs_payload)

    # Debuffs the player applies are mostly relevant for DPS; cheap enough to always include.
    debuffs_payload = await client.query(
        REPORT_DEBUFFS_BY_PLAYER,
        {"code": code, "fightIDs": [fight_id], "sourceID": actor_id},
    )
    debuffs = parse_aura_table(debuffs_payload)

    dmg_taken_payload = await client.query(
        REPORT_DAMAGE_TAKEN_FOR_PLAYER,
        {"code": code, "fightIDs": [fight_id], "targetID": actor_id},
    )
    damage_taken = parse_damage_taken_table(dmg_taken_payload)

    gear = parse_gear_from_player_details(target["raw"])
    gear = [
        {k: v for k, v in g.items() if k not in {"raw"}}
        for g in gear
    ]

    return {
        "actor_id": actor_id,
        "name": target.get("name", ""),
        "server": target.get("server", ""),
        "class_slug": target.get("class_slug", ""),
        "spec_slug": target.get("spec_slug", ""),
        "role": target.get("role", ""),
        "item_level": target.get("item_level"),
        "talents_loadout": target.get("talents_loadout"),
        "talent_ids": target.get("talent_ids") or [],
        # Same shape as the player's own stats — peak combatantInfo
        # values per stat name (Strength/Mastery/Crit/...).
        "stats": target.get("stats") or {},
        "casts": casts[:25],
        "gear": gear,
        "buffs": buffs,
        "debuffs": debuffs,
        "damage_taken": damage_taken,
        # Per-ability boss-cast timeline for THIS fight (fight-relative
        # seconds since fight start). Same shape as the user-fight's
        # ``fight_summary.boss_casts`` so the AI can compare cycle periods
        # between the user's pull and the reference kill directly.
        "boss_casts": boss_casts,
        "metric": metric,
        # Version stamp — the incremental cache snapshot in
        # ``refresh_top_logs_for_spec_encounter`` only reuses a detail
        # blob when this matches ``TOP_LOG_DETAIL_VERSION``. Bumping the
        # constant forces a clean re-fetch.
        "_v": TOP_LOG_DETAIL_VERSION,
    }


def _match_player(players: list[dict[str, Any]], ranking: dict[str, Any]) -> dict[str, Any] | None:
    """Find the player in playerDetails that matches the ranking (by name+server)."""
    name = (ranking.get("character_name") or "").lower()
    server = (ranking.get("server") or "").lower()
    for p in players:
        if (p.get("name") or "").lower() == name and (
            not server or (p.get("server") or "").lower() == server
        ):
            return p
    # Fallback: just by name.
    for p in players:
        if (p.get("name") or "").lower() == name:
            return p
    return None


async def list_top_logs(
    session: AsyncSession,
    *,
    spec_slug: str,
    encounter_id: int | None = None,
    metric: str | None = None,
    difficulty: int | None = None,
) -> list[TopLog]:
    stmt = select(TopLog).where(TopLog.spec_slug == spec_slug)
    if encounter_id is not None:
        stmt = stmt.where(TopLog.encounter_id == encounter_id)
    if metric is not None:
        stmt = stmt.where(TopLog.metric == metric)
    if difficulty is not None:
        stmt = stmt.where(TopLog.difficulty == difficulty)
    # Secondary difficulty ordering keeps the list deterministic now that
    # several difficulties of the same encounter can be cached at once
    # (highest first, so Mythic ranks lead when both exist).
    stmt = stmt.order_by(
        TopLog.encounter_id, TopLog.difficulty.desc().nulls_last(), TopLog.rank.asc()
    )
    return list((await session.execute(stmt)).scalars().all())
