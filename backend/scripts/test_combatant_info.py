"""One-shot smoke test for the includeCombatantInfo: true fix.

Run inside the backend container so it has WCL creds + all deps:

    docker compose exec backend uv run python -m scripts.test_combatant_info <REPORT_CODE>

Prints what WCL returns for combatantInfo and what our parser extracts,
so we can verify both ends of the talent/gear/stats fix before tagging.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.services.wcl.client import WclClient
from app.services.wcl.parser import (
    parse_gear_from_player_details,
    parse_player_details,
)
from app.services.wcl.queries import REPORT_OVERVIEW, REPORT_PLAYER_DETAILS


def short(obj: Any, n: int = 400) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + f"... <+{len(s) - n} chars>"


async def main(code: str) -> None:
    print(f"\n=== Smoke test: includeCombatantInfo for report {code} ===\n")
    async with WclClient() as client:
        print("[1/3] Fetching report overview to pick a fight…")
        overview_payload = await client.query(REPORT_OVERVIEW, {"code": code})
        fights = (
            (overview_payload.get("reportData") or {})
            .get("report", {})
            .get("fights")
            or []
        )
        if not fights:
            print(f"  ! Report {code} has no fights — pick another report.")
            return
        fight = next((f for f in fights if f.get("kill") is not None), fights[0])
        fight_id = int(fight["id"])
        print(f"  -> fight_id={fight_id} name={fight.get('name')!r} kill={fight.get('kill')}")

        print("\n[2/3] Running REPORT_PLAYER_DETAILS with includeCombatantInfo: true…")
        payload = await client.query(
            REPORT_PLAYER_DETAILS, {"code": code, "fightIDs": [fight_id]}
        )
        report = (payload.get("reportData") or {}).get("report") or {}
        details = report.get("playerDetails") or {}
        # Drill the same way the parser does:
        inner = details
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
            print(f"  ! Unexpected playerDetails shape: {type(details).__name__}")
            print(f"    raw: {short(details)}")
            return

        counts = {role: len(inner.get(role) or []) for role in ("tanks", "healers", "dps")}
        print(f"  raw counts -> {counts}")

        all_players = (
            (inner.get("tanks") or [])
            + (inner.get("healers") or [])
            + (inner.get("dps") or [])
        )
        with_ci = sum(1 for p in all_players if p.get("combatantInfo"))
        print(f"  players with combatantInfo: {with_ci}/{len(all_players)}")

        if not all_players:
            print("  ! No players returned — pick a report with logged participants.")
            return

        sample = all_players[0]
        ci_raw = sample.get("combatantInfo")
        ci = ci_raw[0] if isinstance(ci_raw, list) and ci_raw else (
            ci_raw if isinstance(ci_raw, dict) else {}
        )
        print(f"\n  Sample player: {sample.get('name')!r} ({sample.get('type')})")
        print(f"    combatantInfo type: {type(ci_raw).__name__}")
        if isinstance(ci, dict):
            print(f"    combatantInfo keys: {sorted(ci.keys())}")
            talents = ci.get("talents") or []
            gear = ci.get("gear") or []
            stats = ci.get("stats") or {}
            print(f"    talents: {len(talents)}  -> first 3: {short(talents[:3], 200)}")
            print(f"    gear:    {len(gear)}  -> first item: {short(gear[0] if gear else {}, 200)}")
            print(f"    stats:   keys={sorted(stats.keys()) if isinstance(stats, dict) else type(stats).__name__}")
            if isinstance(stats, dict):
                print(f"             sample: {short({k: stats[k] for k in list(stats)[:5]}, 300)}")
        else:
            print(f"    !!! combatantInfo MISSING for first sample. raw={short(sample, 300)}")

        print("\n[3/3] Running our parser against the same payload…")
        parsed = parse_player_details(payload)
        print(f"  parse_player_details -> {len(parsed)} player rows")
        if not parsed:
            print("  ! parser returned no players")
            return
        with_talents = sum(1 for p in parsed if p["talent_ids"])
        with_stats = sum(1 for p in parsed if p["stats"])
        with_loadout = sum(1 for p in parsed if p["talents_loadout"])
        print(f"  with talent_ids:      {with_talents}/{len(parsed)}")
        print(f"  with stats:           {with_stats}/{len(parsed)}")
        print(f"  with talents_loadout: {with_loadout}/{len(parsed)}")

        first = parsed[0]
        gear_rows = parse_gear_from_player_details(first["raw"])
        print(f"\n  Parsed sample: name={first['name']!r} class={first['class_slug']} spec={first['spec_slug']} role={first['role']}")
        print(f"    talent_ids: {len(first['talent_ids'])} -> {short(first['talent_ids'][:8], 200)}")
        print(f"    stats keys: {sorted(first['stats'].keys())}")
        print(f"    stats sample: {short({k: first['stats'][k] for k in list(first['stats'])[:5]}, 200)}")
        print(f"    talents_loadout: {short(first['talents_loadout'], 80)}")
        print(f"    gear rows extracted: {len(gear_rows)} -> first: {short(gear_rows[0] if gear_rows else {}, 200)}")

    print("\n=== done ===\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m scripts.test_combatant_info <WCL_REPORT_CODE>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
