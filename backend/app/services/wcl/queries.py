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
      # Phase metadata per encounter (id → name, is_intermission). We zip
      # this into each fight's ``phase_transitions`` so the AI prompt has
      # semantically meaningful labels instead of bare numeric IDs.
      phases {
        encounterID
        separatesWipes
        phases { id name isIntermission }
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
      # ``startTime`` here is ms relative to the report's start. We need it
      # in top-log detail fetches to convert event timestamps (also report-
      # relative) into fight-relative seconds for boss-cast timelines.
      fights(fightIDs: $fightIDs) { id startTime endTime }
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

# Resource-change event stream for one player. Used to build the mana-
# recovery timeline (Mana Tea / Innervate / potions / buff procs that
# grant mana). WCL's public GraphQL does NOT expose absolute resource
# levels or cast costs — only explicit *grants* show up here. The
# analyser combines this with the player's cast counts and the LLM's
# class knowledge to reason about mana pressure.
REPORT_PLAYER_RESOURCE_EVENTS = """
query ReportPlayerResourceEvents(
  $code: String!,
  $fightIDs: [Int]!,
  $sourceID: Int!,
  $startTime: Float
) {
  reportData {
    report(code: $code) {
      events(
        dataType: Resources,
        fightIDs: $fightIDs,
        sourceID: $sourceID,
        startTime: $startTime,
        limit: 1000
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""


# Per-cast event stream for enemy actors only — used to extract boss-cast
# *timestamps* (not just aggregated counts like ``REPORT_CASTS`` for a
# player). ``report.events`` is paginated: each response carries up to
# ~300 events plus a ``nextPageTimestamp`` we have to feed back as
# ``startTime`` until it comes back null. ``data`` is shipped either as
# a JSON-encoded string or as an already-decoded list (WCL has been
# inconsistent), the parser tolerates both.
REPORT_ENEMY_CAST_EVENTS = """
query ReportEnemyCastEvents(
  $code: String!,
  $fightIDs: [Int]!,
  $startTime: Float
) {
  reportData {
    report(code: $code) {
      events(
        dataType: Casts,
        hostilityType: Enemies,
        fightIDs: $fightIDs,
        startTime: $startTime,
        limit: 1000
      ) {
        data
        nextPageTimestamp
      }
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


# Talent-Finder query: one page of ``characterRankings`` with
# ``includeCombatantInfo`` so every ranking entry carries its ``talents``
# array inline (the ``{talentID, points}`` structured shape). One page is
# 100 entries — enough to cover every hero-tree a spec uses, including
# minority picks the plain top-15 would miss. We don't need gear/casts
# here, just talents, so this is a far lighter fetch than the AI
# analyzer's per-log detail crawl.
ENCOUNTER_RANKING_TALENTS = """
query EncounterRankingTalents(
  $encounterID: Int!,
  $className: String!,
  $specName: String!,
  $metric: CharacterRankingMetricType!,
  $page: Int!,
  $difficulty: Int
) {
  worldData {
    encounter(id: $encounterID) {
      id
      name
      characterRankings(
        className: $className,
        specName: $specName,
        metric: $metric,
        page: $page,
        difficulty: $difficulty,
        includeCombatantInfo: true
      )
    }
  }
}
"""
