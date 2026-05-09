"""GraphQL strings for the WCL queries we use.

Kept in one file so they can be reviewed/lint-checked together. Each query
is intentionally minimal (only the fields we map to our DB) to keep response
size low.
"""
from __future__ import annotations

REPORT_OVERVIEW = """
query ReportOverview($code: String!) {
  reportData {
    report(code: $code) {
      code
      title
      startTime
      endTime
      zone { id name }
      region { compactName }
      revision
      fights(killType: All) {
        id
        encounterID
        name
        difficulty
        keystoneLevel
        kill
        bossPercentage
        startTime
        endTime
        phaseTransitions { id startTime }
      }
    }
  }
}
"""

REPORT_TABLES = """
query ReportTable(
  $code: String!,
  $fightIDs: [Int]!,
  $dataType: TableDataType!
) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: $dataType)
    }
  }
}
"""

REPORT_PLAYER_DETAILS = """
query ReportPlayerDetails($code: String!, $fightIDs: [Int]!) {
  reportData {
    report(code: $code) {
      playerDetails(fightIDs: $fightIDs, includeCombatantInfo: true)
    }
  }
}
"""

REPORT_CASTS = """
query ReportCasts(
  $code: String!,
  $fightIDs: [Int]!,
  $sourceID: Int!
) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Casts, sourceID: $sourceID)
    }
  }
}
"""

# Buffs *applied to* a specific player during the fight (their personal aura
# uptime — useful for tracking major CDs, trinket procs, tier-set buffs).
REPORT_BUFFS_FOR_PLAYER = """
query ReportBuffs($code: String!, $fightIDs: [Int]!, $sourceID: Int!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Buffs, sourceID: $sourceID)
    }
  }
}
"""

# Debuffs the player has applied to enemies (DoT/debuff uptime — relevant for
# Affliction Warlocks, Shadow Priests, Subtlety Rogues, etc.).
REPORT_DEBUFFS_BY_PLAYER = """
query ReportDebuffs($code: String!, $fightIDs: [Int]!, $sourceID: Int!) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: Debuffs, sourceID: $sourceID)
    }
  }
}
"""

# Damage *taken* by a player (filtered to enemy sources) — exposes how often
# a player got hit by avoidable boss mechanics.
REPORT_DAMAGE_TAKEN_FOR_PLAYER = """
query ReportDamageTaken(
  $code: String!,
  $fightIDs: [Int]!,
  $targetID: Int!
) {
  reportData {
    report(code: $code) {
      table(
        fightIDs: $fightIDs,
        dataType: DamageTaken,
        targetID: $targetID,
        hostilityType: Enemies
      )
    }
  }
}
"""

# Per-fight player rankings (parse percentile + ilvl-bracket "best" percentile)
# for every player in the report. Returns a JSON blob with one entry per fight,
# split by role (tanks/healers/dps), each containing the ranked characters.
REPORT_RANKINGS = """
query ReportRankings(
  $code: String!,
  $fightIDs: [Int]!,
  $playerMetric: ReportRankingMetricType
) {
  reportData {
    report(code: $code) {
      rankings(fightIDs: $fightIDs, playerMetric: $playerMetric)
    }
  }
}
"""


# All zones WCL knows about. Each zone has its expansion + encounters list,
# plus a ``brackets`` block we ignore. We use this to discover the current
# retail raid tier without hardcoding encounter IDs.
WORLD_ZONES = """
query WorldZones {
  worldData {
    zones {
      id
      name
      frozen
      expansion { id name }
      encounters { id name }
    }
  }
}
"""


# Top rankings — extended with serverRegion + difficulty filters.
ENCOUNTER_RANKINGS = """
query EncounterRankings(
  $encounterID: Int!,
  $specName: String,
  $className: String,
  $metric: CharacterRankingMetricType!,
  $page: Int!,
  $partition: Int,
  $serverRegion: String,
  $difficulty: Int
) {
  worldData {
    encounter(id: $encounterID) {
      id
      name
      characterRankings(
        specName: $specName,
        className: $className,
        metric: $metric,
        page: $page,
        partition: $partition,
        serverRegion: $serverRegion,
        difficulty: $difficulty
      )
    }
  }
}
"""
