"""Import + cache a Warcraft Logs report into our local DB."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError
from app.models import (
    Report,
    ReportFight,
    ReportPlayer,
    ReportPlayerGear,
)
from app.services.wcl.client import WclClient
from app.services.wcl.parser import (
    parse_damage_done_table,
    parse_deaths_table,
    parse_gear_from_player_details,
    parse_healing_done_table,
    parse_player_details,
    parse_report_input,
    parse_report_overview,
    parse_report_rankings_for_fight,
)
from app.services.wcl.queries import (
    REPORT_OVERVIEW,
    REPORT_PLAYER_DETAILS,
    REPORT_RANKINGS,
    REPORT_TABLES,
)

logger = logging.getLogger(__name__)


async def create_import_skeleton(
    session: AsyncSession, *, raw_input: str, owner_user_id: Any | None
) -> Report:
    """Quickly insert (or return) a Report row in ``importing`` state.

    The HTTP endpoint calls this synchronously so it can return immediately
    with a stable ``report.id`` the frontend can poll. The actual WCL fetch
    happens in :func:`run_report_import` on the arq worker.

    Idempotent: re-importing a code that is already in the DB returns the
    existing row (whether ``importing``/``ready``/``failed``). For ``failed``
    rows the caller may want to re-trigger the worker — that's handled at
    the API layer, not here.
    """
    code = parse_report_input(raw_input)
    existing = (
        await session.execute(select(Report).where(Report.wcl_code == code))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    report = Report(
        wcl_code=code,
        title="",
        owner_user_id=owner_user_id,
        import_status="importing",
        raw_meta={},
    )
    session.add(report)
    await session.flush()
    return report


async def run_report_import(
    session: AsyncSession,
    *,
    report_id: Any,
    wcl_client: WclClient | None = None,
) -> None:
    """Fetch the WCL overview + per-fight player rollups for a skeleton report.

    Drives ``import_status``: ``importing`` → ``ready`` (or ``failed`` on
    exception). Idempotent — calling on a ``ready`` row that already has
    fights does nothing besides refreshing the status.
    """
    report = (
        await session.execute(select(Report).where(Report.id == report_id))
    ).scalar_one_or_none()
    if report is None:
        raise NotFoundError("Report not found.")
    if report.import_status == "ready" and report.start_time is not None:
        return  # already populated; nothing to do

    code = report.wcl_code
    owner_user_id = report.owner_user_id
    own_client = wcl_client is None
    if wcl_client is None and owner_user_id is not None:
        from app.services.wcl_oauth_service import build_user_wcl_client

        wcl_client = await build_user_wcl_client(session, user_id=owner_user_id)
    client = wcl_client or WclClient()
    try:
        overview = parse_report_overview(await client.query(REPORT_OVERVIEW, {"code": code}))
        report.title = overview["title"]
        report.zone_id = overview["zone_id"]
        report.zone_name = overview["zone_name"]
        report.region = overview["region"]
        report.game_version = overview["game_version"]
        report.start_time = overview["start_time"]
        report.end_time = overview["end_time"]
        await session.flush()

        fights_by_id: dict[int, ReportFight] = {}
        for f in overview["fights"]:
            fight_extras = f.pop("extras", {}) or {}
            fight = ReportFight(report_id=report.id, **f, extras=fight_extras)
            session.add(fight)
            fights_by_id[f["fight_id"]] = fight
        await session.flush()

        all_fight_ids = list(fights_by_id.keys())
        if all_fight_ids:
            await _populate_players(session, client, code, all_fight_ids, fights_by_id)

        report.import_status = "ready"
        report.import_error = None
    finally:
        if own_client:
            await client.aclose()


async def _get_report_with_data(session: AsyncSession, code: str) -> Report | None:
    stmt = (
        select(Report)
        .where(Report.wcl_code == code)
        .options(
            selectinload(Report.fights)
            .selectinload(ReportFight.players)
            .selectinload(ReportPlayer.casts),
            selectinload(Report.fights)
            .selectinload(ReportFight.players)
            .selectinload(ReportPlayer.gear),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _populate_players(
    session: AsyncSession,
    client: WclClient,
    code: str,
    fight_ids: list[int],
    fights_by_id: dict[int, ReportFight],
) -> None:
    """Materialise one ``ReportPlayer`` per (fight, player), with **per-fight**
    role/spec/dps/hps numbers.

    We query playerDetails + DamageDone + Healing + Deaths *per fight* rather
    than pooling across the whole report, because a single character can play
    a different spec on different bosses (a monk who tanks Mug'Zee but heals
    Imperator Averzian). Pooled data assigns the player whatever role they
    had in *most* of the report, which is wrong on the fights where they
    swapped — and exactly the case the user wants to analyse.

    Per-fight casts / buffs / debuffs / damage_taken are *deliberately not*
    fetched here — they're lazy-loaded by the analyser the first time a
    specific (player, fight) is analysed, and cached on the player row.
    """
    for fight_id in fight_ids:
        fight = fights_by_id.get(fight_id)
        if fight is None:
            continue
        try:
            details_payload = await client.query(
                REPORT_PLAYER_DETAILS, {"code": code, "fightIDs": [fight_id]}
            )
        except Exception:  # noqa: BLE001
            logger.exception("playerDetails fetch failed for fight=%s", fight_id)
            continue
        players = parse_player_details(details_payload)
        if not players:
            continue

        try:
            damage_payload = await client.query(
                REPORT_TABLES,
                {"code": code, "fightIDs": [fight_id], "dataType": "DamageDone"},
            )
            damage_by_actor = parse_damage_done_table(damage_payload)
        except Exception:  # noqa: BLE001
            logger.exception("DamageDone fetch failed for fight=%s", fight_id)
            damage_by_actor = {}
        try:
            healing_payload = await client.query(
                REPORT_TABLES,
                {"code": code, "fightIDs": [fight_id], "dataType": "Healing"},
            )
            healing_by_actor = parse_healing_done_table(healing_payload)
        except Exception:  # noqa: BLE001
            logger.exception("Healing fetch failed for fight=%s", fight_id)
            healing_by_actor = {}
        try:
            deaths_payload = await client.query(
                REPORT_TABLES,
                {"code": code, "fightIDs": [fight_id], "dataType": "Deaths"},
            )
            deaths_by_actor = parse_deaths_table(deaths_payload)
        except Exception:  # noqa: BLE001
            logger.exception("Deaths fetch failed for fight=%s", fight_id)
            deaths_by_actor = {}

        # Fetch WCL parse-percentile data for *every* player in this fight
        # in two calls (one per metric). The dps call gives us tank+dps
        # rankings, the hps call gives us healer rankings — we read each
        # player from the bucket matching their per-fight role. The maps
        # are keyed by lowercased character name (WCL rankings use *global*
        # character IDs while our ``actor_id`` is report-local).
        hps_metrics: dict[str, dict[str, Any]] = {}
        dps_metrics: dict[str, dict[str, Any]] = {}
        try:
            hps_payload = await client.query(
                REPORT_RANKINGS,
                {"code": code, "fightIDs": [fight_id], "playerMetric": "hps"},
            )
            hps_metrics = parse_report_rankings_for_fight(
                hps_payload, fight_id=fight_id, roles={"healers"}
            )
        except Exception:  # noqa: BLE001
            logger.exception("HPS rankings fetch failed for fight=%s", fight_id)
        try:
            dps_payload = await client.query(
                REPORT_RANKINGS,
                {"code": code, "fightIDs": [fight_id], "playerMetric": "dps"},
            )
            dps_metrics = parse_report_rankings_for_fight(
                dps_payload, fight_id=fight_id, roles={"dps", "tanks"}
            )
        except Exception:  # noqa: BLE001
            logger.exception("DPS rankings fetch failed for fight=%s", fight_id)

        for p in players:
            actor_id = p["actor_id"]
            damage = damage_by_actor.get(actor_id, {})
            healing = healing_by_actor.get(actor_id, {})
            deaths = deaths_by_actor.get(actor_id, 0)
            gear = parse_gear_from_player_details(p["raw"])
            name_key = (p.get("name") or "").strip().lower()
            metrics = (
                hps_metrics.get(name_key)
                if p["role"] == "healer"
                else dps_metrics.get(name_key)
            )

            db_player = ReportPlayer(
                fight_id=fight.id,
                actor_id=actor_id,
                name=p["name"],
                server=p["server"],
                class_slug=p["class_slug"],
                spec_slug=p["spec_slug"],
                role=p["role"],
                item_level=p.get("item_level"),
                damage_done=int(damage.get("damage_done", 0)),
                dps=float(damage.get("dps", 0)) or None,
                healing_done=int(healing.get("healing_done", 0)),
                hps=float(healing.get("hps", 0)) or None,
                deaths=int(deaths),
                talents_loadout=p.get("talents_loadout"),
                extras={
                    "talent_ids": p.get("talent_ids") or [],
                    # Primary attribute (Str/Agi/Int) + secondary stats
                    # (Mastery/Crit/Haste/Versatility) + tertiary
                    # (Speed/Leech/Avoidance) snapshot at peak (max). Used
                    # by the AI to compare against top-log references and
                    # call out under-statted slots vs the percentile.
                    "stats": p.get("stats") or {},
                    # Always set ``parse_metrics`` (even when WCL has no
                    # ranking data) so the analyzer's ``has_parse_metrics``
                    # marker is True and we don't re-fetch later.
                    "parse_metrics": metrics or {
                        "rank_percent": None,
                        "ilvl_percent": None,
                        "total_parses": None,
                        "rank": None,
                        "out_of": None,
                    },
                },
            )
            session.add(db_player)
            await session.flush()
            for g in gear:
                session.add(ReportPlayerGear(player_id=db_player.id, **g))

    await session.flush()


async def get_report(session: AsyncSession, *, code: str) -> Report:
    report = await _get_report_with_data(session, code)
    if not report:
        raise NotFoundError("Report not found.")
    return report
