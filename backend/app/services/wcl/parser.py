"""Parse WCL URLs and the JSON payloads returned by our GraphQL queries.

WCL URL shapes we accept (all yield the same code):
- https://www.warcraftlogs.com/reports/{CODE}
- https://www.warcraftlogs.com/reports/{CODE}#fight=42
- https://classic.warcraftlogs.com/reports/{CODE}
- bare {CODE} (16-character alphanumeric WCL id)
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.core.errors import ValidationAppError

_WCL_CODE_RE = re.compile(r"^[A-Za-z0-9]{16}$")
_WCL_URL_RE = re.compile(r"warcraftlogs\.com/reports/([A-Za-z0-9]{16})")


def parse_report_input(value: str) -> str:
    """Return the bare WCL report code from a URL or pasted code."""
    value = (value or "").strip()
    if not value:
        raise ValidationAppError("No report URL or code provided.")
    if _WCL_CODE_RE.match(value):
        return value
    m = _WCL_URL_RE.search(value)
    if m:
        return m.group(1)
    raise ValidationAppError("Could not extract a Warcraft Logs report code from the input.")


def _ms_to_dt(ms: float | int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def parse_report_overview(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise the ``reportData.report`` overview into a dict ready for ORM use."""
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    if not report:
        raise ValidationAppError("Report not found on Warcraft Logs (private or invalid code?).")
    zone = report.get("zone") or {}
    region = report.get("region") or {}
    fights_in = report.get("fights") or []
    fights = []
    for f in fights_in:
        start_ms = report["startTime"] + f.get("startTime", 0)
        end_ms = report["startTime"] + f.get("endTime", f.get("startTime", 0))
        # Phase transitions are reported in ms relative to fight start; we keep
        # them like that for compactness and let the AI/UI normalise as needed.
        phase_transitions = [
            {"id": int(p.get("id", 0)), "start_ms": int(p.get("startTime", 0))}
            for p in (f.get("phaseTransitions") or [])
            if p
        ]
        fights.append(
            {
                "fight_id": int(f["id"]),
                "encounter_id": int(f["encounterID"]) if f.get("encounterID") else None,
                "name": f.get("name") or "",
                "difficulty": f.get("difficulty"),
                "keystone_level": f.get("keystoneLevel"),
                "is_kill": bool(f.get("kill")),
                "boss_percentage": f.get("bossPercentage"),
                "duration_ms": max(0, int(end_ms - start_ms)),
                "start_time": _ms_to_dt(start_ms),
                "extras": {"phase_transitions": phase_transitions},
            }
        )
    return {
        "wcl_code": report["code"],
        "title": report.get("title") or "",
        "zone_id": zone.get("id"),
        "zone_name": zone.get("name") or "",
        "region": region.get("compactName") or "",
        # WCL no longer exposes gameVersion on Report. We're hitting the
        # retail API endpoints, so we hardcode "retail".
        "game_version": "retail",
        "start_time": _ms_to_dt(report["startTime"]),
        "end_time": _ms_to_dt(report["endTime"]),
        "fights": fights,
    }


_WCL_CLASS_TO_SLUG = {
    "deathknight": "death_knight",
    "demonhunter": "demon_hunter",
    "druid": "druid",
    "evoker": "evoker",
    "hunter": "hunter",
    "mage": "mage",
    "monk": "monk",
    "paladin": "paladin",
    "priest": "priest",
    "rogue": "rogue",
    "shaman": "shaman",
    "warlock": "warlock",
    "warrior": "warrior",
}

_WCL_SPEC_NAME_TO_SLUG = {
    # death knight
    ("death_knight", "Blood"): "death_knight_blood",
    ("death_knight", "Frost"): "death_knight_frost",
    ("death_knight", "Unholy"): "death_knight_unholy",
    # dh
    ("demon_hunter", "Havoc"): "demon_hunter_havoc",
    ("demon_hunter", "Vengeance"): "demon_hunter_vengeance",
    # druid
    ("druid", "Balance"): "druid_balance",
    ("druid", "Feral"): "druid_feral",
    ("druid", "Guardian"): "druid_guardian",
    ("druid", "Restoration"): "druid_restoration",
    # evoker
    ("evoker", "Devastation"): "evoker_devastation",
    ("evoker", "Preservation"): "evoker_preservation",
    ("evoker", "Augmentation"): "evoker_augmentation",
    # hunter
    ("hunter", "BeastMastery"): "hunter_beast_mastery",
    ("hunter", "Beast Mastery"): "hunter_beast_mastery",
    ("hunter", "Marksmanship"): "hunter_marksmanship",
    ("hunter", "Survival"): "hunter_survival",
    # mage
    ("mage", "Arcane"): "mage_arcane",
    ("mage", "Fire"): "mage_fire",
    ("mage", "Frost"): "mage_frost",
    # monk
    ("monk", "Brewmaster"): "monk_brewmaster",
    ("monk", "Mistweaver"): "monk_mistweaver",
    ("monk", "Windwalker"): "monk_windwalker",
    # paladin
    ("paladin", "Holy"): "paladin_holy",
    ("paladin", "Protection"): "paladin_protection",
    ("paladin", "Retribution"): "paladin_retribution",
    # priest
    ("priest", "Discipline"): "priest_discipline",
    ("priest", "Holy"): "priest_holy",
    ("priest", "Shadow"): "priest_shadow",
    # rogue
    ("rogue", "Assassination"): "rogue_assassination",
    ("rogue", "Outlaw"): "rogue_outlaw",
    ("rogue", "Subtlety"): "rogue_subtlety",
    # shaman
    ("shaman", "Elemental"): "shaman_elemental",
    ("shaman", "Enhancement"): "shaman_enhancement",
    ("shaman", "Restoration"): "shaman_restoration",
    # warlock
    ("warlock", "Affliction"): "warlock_affliction",
    ("warlock", "Demonology"): "warlock_demonology",
    ("warlock", "Destruction"): "warlock_destruction",
    # warrior
    ("warrior", "Arms"): "warrior_arms",
    ("warrior", "Fury"): "warrior_fury",
    ("warrior", "Protection"): "warrior_protection",
}


def class_slug_from_wcl(wcl_class: str | None) -> str:
    if not wcl_class:
        return ""
    key = wcl_class.replace(" ", "").lower()
    return _WCL_CLASS_TO_SLUG.get(key, "")


def spec_slug_from_wcl(class_slug: str, wcl_spec: str | None) -> str:
    if not wcl_spec:
        return ""
    key = (class_slug, wcl_spec.replace(" ", ""))
    if key in _WCL_SPEC_NAME_TO_SLUG:
        return _WCL_SPEC_NAME_TO_SLUG[key]
    key2 = (class_slug, wcl_spec)
    return _WCL_SPEC_NAME_TO_SLUG.get(key2, "")


def role_from_spec_slug(spec_slug: str, fallback: str = "dps") -> str:
    """Best-effort role inference; the real source of truth is the GameSpec table."""
    healers = {
        "druid_restoration",
        "evoker_preservation",
        "monk_mistweaver",
        "paladin_holy",
        "priest_discipline",
        "priest_holy",
        "shaman_restoration",
    }
    tanks = {
        "death_knight_blood",
        "demon_hunter_vengeance",
        "druid_guardian",
        "monk_brewmaster",
        "paladin_protection",
        "warrior_protection",
    }
    if spec_slug in healers:
        return "healer"
    if spec_slug in tanks:
        return "tank"
    return fallback


def parse_player_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the ``playerDetails`` field into a list of role-tagged players.

    WCL has been creative about how deep it nests the role buckets:
    - shape A: ``{tanks: [], healers: [], dps: []}``
    - shape B: ``{data: {tanks: [], healers: [], dps: []}}``
    - shape C: ``{data: {playerDetails: {tanks: [], healers: [], dps: []}}}``
    We walk down until we find a dict that has at least one of those keys.
    """
    out: list[dict[str, Any]] = []
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    if not report:
        return out
    raw = report.get("playerDetails") or {}

    inner = raw if isinstance(raw, dict) else {}
    for _ in range(3):  # drill at most 3 levels
        if isinstance(inner, dict) and any(
            k in inner for k in ("tanks", "healers", "dps")
        ):
            break
        if isinstance(inner, dict) and "playerDetails" in inner:
            inner = inner.get("playerDetails") or {}
            continue
        if isinstance(inner, dict) and "data" in inner:
            inner = inner.get("data") or {}
            continue
        break
    if not isinstance(inner, dict):
        return out

    for role_key, role_value in (("dps", "dps"), ("healers", "healer"), ("tanks", "tank")):
        for p in inner.get(role_key, []) or []:
            class_slug = class_slug_from_wcl(p.get("type"))
            specs = p.get("specs") or []
            best_spec = max(specs, key=lambda s: int(s.get("count", 0))) if specs else {}
            spec_slug = spec_slug_from_wcl(class_slug, best_spec.get("spec")) if class_slug else ""
            ilvl = p.get("maxItemLevel") or p.get("minItemLevel")
            raw_ci = p.get("combatantInfo")
            if isinstance(raw_ci, list):
                combatant = raw_ci[0] if raw_ci else {}
            elif isinstance(raw_ci, dict):
                combatant = raw_ci
            else:
                combatant = {}
            talent_ids: list[int] = []
            for t in (combatant.get("talents") or []) if isinstance(combatant, dict) else []:
                if isinstance(t, dict) and t.get("guid") is not None:
                    try:
                        talent_ids.append(int(t["guid"]))
                    except (TypeError, ValueError):
                        continue
                elif isinstance(t, int):
                    talent_ids.append(t)
            loadout = (
                p.get("talentLoadout")
                or (combatant.get("talentTree") if isinstance(combatant, dict) else None)
                or (combatant.get("talents_string") if isinstance(combatant, dict) else None)
            )
            # Modern WoW (DF/TWW) ships `combatantInfo.talents` as `[]` and
            # puts the actual picks in the talent-tree loadout — list of
            # `{id, rank, nodeID}` entries. Backfill talent_ids from the
            # loadout so the AI prompt always has a flat ID list to cite,
            # regardless of expansion.
            if not talent_ids and isinstance(loadout, list):
                for t in loadout:
                    if not isinstance(t, dict):
                        continue
                    tid = t.get("id") or t.get("talentID") or t.get("guid")
                    if tid is None:
                        continue
                    try:
                        talent_ids.append(int(tid))
                    except (TypeError, ValueError):
                        continue
            # Secondary stats + primary attributes from combatantInfo.stats.
            # WCL packages them as ``{"Strength": {"min": x, "max": y}, ...}``;
            # we collapse to the max value (peak gear/buffs) for AI prompts.
            stats: dict[str, int] = {}
            raw_stats = (
                combatant.get("stats") if isinstance(combatant, dict) else None
            )
            if isinstance(raw_stats, dict):
                for stat_name, val in raw_stats.items():
                    if isinstance(val, dict):
                        # ``max`` is the peak value during the fight (with
                        # buffs/procs); ``min`` is unbuffed baseline. AI
                        # cares about peak — that's what shows in armoury.
                        v = val.get("max") if val.get("max") is not None else val.get("min")
                    elif isinstance(val, (int, float)):
                        v = val
                    else:
                        continue
                    try:
                        stats[str(stat_name)] = int(v)
                    except (TypeError, ValueError):
                        continue
            out.append(
                {
                    "actor_id": int(p["id"]),
                    "name": p.get("name") or "",
                    "server": p.get("server") or "",
                    "class_slug": class_slug,
                    "spec_slug": spec_slug,
                    "role": role_value,
                    "item_level": float(ilvl) if ilvl is not None else None,
                    "talents_loadout": loadout,
                    "talent_ids": talent_ids,
                    "stats": stats,
                    "raw": p,
                }
            )
    return out


def composition_from_player_details(payload: dict[str, Any]) -> dict[str, int]:
    """Return a small dict with raid composition counts (tanks/healers/dps)."""
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    if not report:
        return {"tanks": 0, "healers": 0, "dps": 0}
    raw = report.get("playerDetails") or {}
    inner = raw if isinstance(raw, dict) else {}
    for _ in range(3):
        if isinstance(inner, dict) and any(
            k in inner for k in ("tanks", "healers", "dps")
        ):
            break
        if isinstance(inner, dict) and "playerDetails" in inner:
            inner = inner.get("playerDetails") or {}
            continue
        if isinstance(inner, dict) and "data" in inner:
            inner = inner.get("data") or {}
            continue
        break
    if not isinstance(inner, dict):
        return {"tanks": 0, "healers": 0, "dps": 0}
    return {
        "tanks": len(inner.get("tanks") or []),
        "healers": len(inner.get("healers") or []),
        "dps": len(inner.get("dps") or []),
    }


def parse_damage_done_table(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return ``{actor_id: {damage_done, dps}}`` from a DamageDone table response."""
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    table = (report or {}).get("table") or {}
    inner = table.get("data") or table
    entries = inner.get("entries") or []
    total_time = float(inner.get("totalTime") or 0)
    out: dict[int, dict[str, Any]] = {}
    for e in entries:
        aid = int(e.get("id", 0))
        total = int(e.get("total", 0))
        active = float(e.get("activeTime", total_time))
        dps = (total / active * 1000) if active else 0.0
        out[aid] = {"damage_done": total, "dps": dps}
    return out


def parse_healing_done_table(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    table = (report or {}).get("table") or {}
    inner = table.get("data") or table
    entries = inner.get("entries") or []
    total_time = float(inner.get("totalTime") or 0)
    out: dict[int, dict[str, Any]] = {}
    for e in entries:
        aid = int(e.get("id", 0))
        total = int(e.get("total", 0))
        active = float(e.get("activeTime", total_time))
        hps = (total / active * 1000) if active else 0.0
        out[aid] = {"healing_done": total, "hps": hps}
    return out


def parse_deaths_table(payload: dict[str, Any]) -> dict[int, int]:
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    table = (report or {}).get("table") or {}
    inner = table.get("data") or table
    entries = inner.get("entries") or []
    counts: dict[int, int] = {}
    for e in entries:
        aid = int(e.get("id", 0))
        counts[aid] = counts.get(aid, 0) + 1
    return counts


def parse_casts_table(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    table = (report or {}).get("table") or {}
    inner = table.get("data") or table
    entries = inner.get("entries") or []
    out: list[dict[str, Any]] = []
    for e in entries:
        gid = e.get("guid") or e.get("id")
        if gid is None:
            continue
        out.append(
            {
                "ability_id": int(gid),
                "ability_name": str(e.get("name") or ""),
                "casts": int(e.get("hitCount") or e.get("total") or 0),
                "hits": int(e.get("hitCount") or 0),
                "total": int(e.get("total") or 0),
                "icon": e.get("abilityIcon"),
            }
        )
    return out


def parse_gear_from_player_details(player_raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract gear items from a player's combatantInfo.

    WCL has shipped two shapes for ``combatantInfo``:
    - dict: ``{"gear": [...], ...}`` (typical)
    - list:  ``[{"gear": [...], ...}, ...]`` (one entry per combat segment)
    """
    raw_ci = player_raw.get("combatantInfo")
    if isinstance(raw_ci, list):
        ci = raw_ci[0] if raw_ci else {}
    elif isinstance(raw_ci, dict):
        ci = raw_ci
    else:
        ci = {}

    gear_list = ci.get("gear") if isinstance(ci, dict) else None
    if not isinstance(gear_list, list):
        return []

    gear: list[dict[str, Any]] = []
    for slot, item in enumerate(gear_list):
        if not isinstance(item, dict) or not item:
            continue
        try:
            item_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        if not item_id:
            continue
        gem_ids: list[int] = []
        for g in item.get("gems") or []:
            try:
                if isinstance(g, dict) and g.get("id"):
                    gem_ids.append(int(g["id"]))
                elif isinstance(g, int):
                    gem_ids.append(int(g))
            except (TypeError, ValueError):
                continue
        bonus_ids: list[int] = []
        for b in item.get("bonusIDs") or []:
            try:
                bonus_ids.append(int(b))
            except (TypeError, ValueError):
                continue
        gear.append(
            {
                "slot": int(slot),
                "item_id": item_id,
                "item_level": int(item["itemLevel"]) if item.get("itemLevel") else None,
                "item_quality": int(item["quality"]) if item.get("quality") is not None else None,
                "name": str(item.get("name") or ""),
                "icon": item.get("icon"),
                "enchant_id": int(item["permanentEnchant"]) if item.get("permanentEnchant") else None,
                "gem_ids": gem_ids,
                "bonus_ids": bonus_ids,
            }
        )
    return gear


def parse_aura_table(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a Buffs/Debuffs table response into a list of uptime entries.

    Each entry is ``{ability_id, name, total_uptime_ms, uptime_pct, icon}``.
    Entries are sorted by uptime descending and trimmed to top 25 to keep the
    AI prompt manageable.
    """
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    table = (report or {}).get("table") or {}
    inner = table.get("data") or table
    total_time = float(inner.get("totalTime") or 0)
    auras = inner.get("auras") or inner.get("entries") or []
    out: list[dict[str, Any]] = []
    for a in auras:
        guid = a.get("guid") or a.get("id")
        if guid is None:
            continue
        uptime = float(a.get("totalUptime") or a.get("uptime") or 0)
        out.append(
            {
                "ability_id": int(guid),
                "name": str(a.get("name") or ""),
                "total_uptime_ms": int(uptime),
                "uptime_pct": (uptime / total_time * 100) if total_time else 0.0,
                "icon": a.get("abilityIcon") or a.get("icon"),
            }
        )
    out.sort(key=lambda e: e["total_uptime_ms"], reverse=True)
    return out[:25]


def parse_damage_taken_table(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a DamageTaken table (filtered to enemy sources) into per-ability totals."""
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    table = (report or {}).get("table") or {}
    inner = table.get("data") or table
    entries = inner.get("entries") or []
    out: list[dict[str, Any]] = []
    for e in entries:
        guid = e.get("guid") or e.get("id")
        if guid is None:
            continue
        out.append(
            {
                "ability_id": int(guid),
                "name": str(e.get("name") or ""),
                "total": int(e.get("total") or 0),
                "hits": int(e.get("hitCount") or e.get("uses") or 0),
                "icon": e.get("abilityIcon") or e.get("icon"),
            }
        )
    out.sort(key=lambda e: e["total"], reverse=True)
    # Keep the top 15 enemy abilities — that's typically the full mechanic
    # picture for a fight without flooding the prompt.
    return out[:15]


def _empty_rank_metrics() -> dict[str, float | int | None]:
    return {
        "rank_percent": None,
        "ilvl_percent": None,
        "total_parses": None,
        "rank": None,
        "out_of": None,
    }


def _safe_int(v: Any) -> int | None:
    """Best-effort int parse — tolerates WCL's approximate-rank strings.

    WCL returns ranks as plain ints up to a point, then switches to
    strings like ``"~908"`` for very high ranks where the exact position
    isn't tracked (the leading ``~`` means "approximately"). Float-strings
    like ``"908.0"`` and stray whitespace also show up occasionally.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None  # avoid bool-as-int surprise
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        cleaned = v.lstrip("~").strip()
        if not cleaned:
            return None
        try:
            return int(float(cleaned))
        except (TypeError, ValueError):
            return None
    return None


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        cleaned = v.lstrip("~").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return None
    return None


def _extract_rank_metrics(ch: dict[str, Any]) -> dict[str, float | int | None]:
    """Extract a character entry's parse/ilvl percentile data.

    WCL UI ↔ GraphQL field mapping:
        - "Parse %" → ``rankPercent`` (vs. all public logs)
        - "iLvl %"  → ``bracketPercent`` (vs. the same item-level bracket)
    ``bestPercent`` is the player's *historical best* on this encounter and
    is **not** the same as iLvl %; we keep it as a last-resort fallback for
    old API responses but prefer ``bracketPercent``.
    """
    ip = ch.get("bracketPercent")
    if ip is None:
        ip = ch.get("bestPercent")  # legacy fallback
    return {
        "rank_percent": _safe_float(ch.get("rankPercent")),
        "ilvl_percent": _safe_float(ip),
        "total_parses": _safe_int(ch.get("totalParses")),
        "rank": _safe_int(ch.get("rank")),
        "out_of": _safe_int(ch.get("outOf") or ch.get("totalParses")),
    }


def _walk_report_rankings(
    payload: dict[str, Any], fight_id: int, roles: set[str] | None = None
):
    """Yield ``(actor_id, character_dict, role_key)`` from a report.rankings
    JSON blob, optionally filtered to specific role buckets.

    The rankings JSON shape (current as of 2025-2026):
    ```
    {
      "data": [
        {"fightID": 1, "encounter": {...},
         "roles": {
           "tanks":   {"characters": [{...}]},
           "healers": {"characters": [{...}]},
           "dps":     {"characters": [{...}]}
         }
        }
      ]
    }
    ```
    """
    rd = (payload or {}).get("reportData", {})
    report = rd.get("report") if rd else None
    rankings = (report or {}).get("rankings") if report else None
    if not rankings:
        return
    # WCL has shipped both already-decoded dict and raw-JSON-string shapes.
    if isinstance(rankings, str):
        try:
            import json as _json

            rankings = _json.loads(rankings)
        except (ValueError, TypeError):
            return
    if not isinstance(rankings, dict):
        return
    data = rankings.get("data") or []
    if not isinstance(data, list):
        return
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if int(entry.get("fightID") or 0) != fight_id:
            continue
        roles_dict = entry.get("roles") or {}
        if not isinstance(roles_dict, dict):
            continue
        for role_key, role_value in roles_dict.items():
            if roles is not None and role_key not in roles:
                continue
            if not isinstance(role_value, dict):
                continue
            for ch in role_value.get("characters") or []:
                if not isinstance(ch, dict):
                    continue
                ch_id = ch.get("id")
                if ch_id is None:
                    continue
                try:
                    yield int(ch_id), ch, role_key
                except (TypeError, ValueError):
                    continue


def parse_report_rankings_for_player(
    payload: dict[str, Any],
    *,
    fight_id: int,
    actor_id: int | None = None,
    player_name: str | None = None,
) -> dict[str, float | int | None]:
    """Find one player's parse-percentile data in a report.rankings blob.

    Returns ``{rank_percent, ilvl_percent, total_parses, rank, out_of}``;
    fields are ``None`` when WCL hasn't ranked this player+fight yet.
    """
    name_lower = (player_name or "").lower()
    for ch_id, ch, _role in _walk_report_rankings(payload, fight_id):
        matched = False
        if actor_id is not None:
            try:
                matched = ch_id == int(actor_id)
            except (TypeError, ValueError):
                matched = False
        if not matched and name_lower and (ch.get("name") or "").lower() == name_lower:
            matched = True
        if matched:
            return _extract_rank_metrics(ch)
    return _empty_rank_metrics()


def parse_report_rankings_for_fight(
    payload: dict[str, Any],
    *,
    fight_id: int,
    roles: set[str] | None = None,
) -> dict[str, dict[str, float | int | None]]:
    """Return ``{name_lower: rank_metrics}`` for every character WCL ranks on
    this fight, optionally filtered to specific role buckets (``"healers"``,
    ``"dps"``, ``"tanks"``).

    Used at import time so we can populate parse-percentile data for *every*
    player in a fight with a single WCL call per metric, instead of
    re-querying per player at analysis time.

    Keyed by **lowercased character name**, not actor id — WCL's report-
    rankings response uses *global* character IDs (millions-range), while our
    ``ReportPlayer.actor_id`` is the *report-local* id from ``playerDetails``
    (1-50 range). Names are unique inside a single raid, so they're a stable
    join key.
    """
    out: dict[str, dict[str, float | int | None]] = {}
    for _ch_id, ch, _role in _walk_report_rankings(payload, fight_id, roles=roles):
        name = (ch.get("name") or "").strip().lower()
        if not name:
            continue
        out[name] = _extract_rank_metrics(ch)
    return out


def parse_encounter_rankings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``worldData.encounter.characterRankings`` into a list of dicts."""
    wd = (payload or {}).get("worldData", {})
    enc = wd.get("encounter") if wd else None
    if not enc:
        return []
    raw_rankings = enc.get("characterRankings") or {}
    if isinstance(raw_rankings, dict):
        rankings = raw_rankings.get("rankings", []) or []
    else:
        rankings = []
    out: list[dict[str, Any]] = []
    encounter_name = enc.get("name") or ""
    encounter_id = int(enc.get("id"))
    for idx, r in enumerate(rankings, start=1):
        if not isinstance(r, dict):
            continue

        # WCL has shipped two shapes for "server" depending on patch:
        #   - object: {"name": "Blackhand", "region": {"slug": "EU"}}
        #   - flat object: {"name": "Blackhand", "region": "EU"}
        #   - bare string: "Blackhand"
        server_field = r.get("server")
        server_name = ""
        region_slug = ""
        if isinstance(server_field, dict):
            server_name = server_field.get("name") or ""
            reg = server_field.get("region")
            if isinstance(reg, dict):
                region_slug = reg.get("slug") or reg.get("compactName") or ""
            elif isinstance(reg, str):
                region_slug = reg
        elif isinstance(server_field, str):
            server_name = server_field

        # Fallback to top-level "serverName"/"serverRegion" if WCL provides them.
        if not server_name and isinstance(r.get("serverName"), str):
            server_name = r["serverName"]
        if not region_slug and isinstance(r.get("serverRegion"), str):
            region_slug = r["serverRegion"]

        report_field = r.get("report")
        report_code = ""
        report_fight_id = 0
        if isinstance(report_field, dict):
            report_code = str(report_field.get("code") or "")
            try:
                report_fight_id = int(report_field.get("fightID") or 0)
            except (TypeError, ValueError):
                report_fight_id = 0

        try:
            ilvl = float(r["bracketData"]) if r.get("bracketData") is not None else None
        except (TypeError, ValueError):
            ilvl = None
        try:
            duration = int(r["duration"]) if r.get("duration") is not None else None
        except (TypeError, ValueError):
            duration = None

        out.append(
            {
                "encounter_id": encounter_id,
                "encounter_name": encounter_name,
                "rank": idx,
                "amount": float(r.get("amount") or 0),
                "item_level": ilvl,
                "duration_ms": duration,
                "character_name": str(r.get("name") or ""),
                "server": server_name,
                "region": region_slug,
                "wcl_report_code": report_code,
                "wcl_fight_id": report_fight_id,
                "raw": r,
            }
        )
    return out
