"""Talent-finder orchestrator: WCL rankings → MaterializedBuild list.

This service stitches together the pieces:

  WCL characterRankings (1 page = 100 entries, talents inline)
        ↓ decoded_from_talent_tree
  list[DecodedLoadout]  (rank-ordered, best DPS first)
        ↓ cluster_loadouts (hero-tree split + consensus/contested)
  ClusterResult
        ↓ generate_build_variants (Cartesian over contested)
  list[variant: dict[entry_id, rank]]
        ↓ materialize_variants (round-trip via encode/decode)
  list[MaterializedBuild]    ← what the simc worker consumes

We fetch ``characterRankings`` *directly* rather than reading the cached
``TopLog`` table: one query returns 100 ranked players with their
talents inline, which is enough to cover every hero-tree a spec uses —
including a minority tree that the plain top-15 would miss entirely.
That minority-coverage matters because the *one-button* optimum can sit
on a hero-tree the manual-play meta doesn't favour.

The "auto threshold raise" behaviour: if the user's chosen threshold
produces a variants explosion (raised by ``generate_build_variants``),
we step the threshold up and retry until the build count fits.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, GameSpec, TalentRankingSnapshot
from app.schemas.talent_finder import EncounterMap
from app.services.talents import (
    DecodedLoadout,
    TraitDataset,
    get_dataset,
)
from app.services.talents.decoder import decoded_from_talent_tree
from app.services.talents.finder import (
    ClusterResult,
    MaterializedBuild,
    cluster_loadouts,
    distinct_builds,
    generate_build_variants,
    hero_sub_tree,
    loadout_variant,
    materialize_variants,
)
from app.services.wcl.client import WclClient
from app.services.wcl.queries import ENCOUNTER_RANKING_TALENTS

logger = logging.getLogger(__name__)

ENCOUNTER_MAP_SETTING_KEY = "talent_finder_encounter_map"
"""The single ``AppSetting`` row that stores the admin-configured
(fight_profile → encounter) mapping. JSON value shape matches
:class:`EncounterMap`."""


# ---------------------------------------------------------------------------
# Admin: encounter mapping
# ---------------------------------------------------------------------------


async def read_encounter_map(session: AsyncSession) -> EncounterMap:
    """Return the current map (empty defaults if the setting is absent)."""
    row = (
        await session.execute(
            select(AppSetting).where(AppSetting.key == ENCOUNTER_MAP_SETTING_KEY)
        )
    ).scalar_one_or_none()
    if row is None or not isinstance(row.value, dict):
        return EncounterMap()
    return EncounterMap.model_validate(row.value)


async def write_encounter_map(
    session: AsyncSession, encounter_map: EncounterMap
) -> None:
    """Upsert the encounter map setting. Caller is responsible for the commit."""
    payload = encounter_map.model_dump(mode="json")
    stmt = pg_insert(AppSetting).values(
        key=ENCOUNTER_MAP_SETTING_KEY, value=payload
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": stmt.excluded.value},
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Spec resolution from a /simc profile
# ---------------------------------------------------------------------------


# simc concatenates compound class names (DeathKnight → "deathknight").
# Our DB uses the underscored form. Only two classes actually differ;
# the rest are single-word and match directly.
_SIMC_TO_DB_CLASS_SLUG = {
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

_CLASS_LINE_RE = re.compile(
    r"^\s*("
    + "|".join(re.escape(k) for k in _SIMC_TO_DB_CLASS_SLUG)
    + r")\s*=", re.IGNORECASE,
)
_SPEC_LINE_RE = re.compile(r"^\s*spec\s*=\s*([a-z_]+)\s*$", re.IGNORECASE)


def parse_class_and_spec(profile: str) -> tuple[str, str]:
    """Pull ``(class_db_slug, spec_simc)`` out of a /simc profile.

    Raises ``ValueError`` if the profile doesn't look like a /simc
    paste (no class line, or no spec line). Callers should rescue and
    surface a 422 to the user with a clear "paste the /simc text"
    hint, same as the standard simulation endpoint does.
    """
    class_db: str | None = None
    spec_simc: str | None = None
    for raw in profile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if class_db is None:
            m = _CLASS_LINE_RE.match(line)
            if m:
                class_db = _SIMC_TO_DB_CLASS_SLUG[m.group(1).lower()]
                continue
        if spec_simc is None:
            m = _SPEC_LINE_RE.match(line)
            if m:
                spec_simc = m.group(1).lower()
        if class_db and spec_simc:
            break
    if class_db is None:
        raise ValueError(
            "Profile doesn't contain a recognized class= line. Paste the "
            "full /simc text the in-game command produced."
        )
    if spec_simc is None:
        raise ValueError(
            "Profile is missing a spec= line. Activate the talent loadout "
            "in-game and re-run /simc."
        )
    return class_db, spec_simc


async def resolve_game_spec(
    session: AsyncSession, class_db_slug: str, spec_simc: str
) -> GameSpec:
    """Map (class, spec) parsed from a /simc paste to the GameSpec row.

    The DB stores ``GameSpec.slug`` as ``{class_slug}_{spec_slug}``
    (see column comment in models/class_spec.py). We compose the
    expected slug, then double-check the class matches in case the
    user pasted a doctored profile.
    """
    expected = f"{class_db_slug}_{spec_simc}"
    row = (
        await session.execute(select(GameSpec).where(GameSpec.slug == expected))
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(
            f"Unknown spec '{expected}'. Either the WoW data tables "
            f"haven't been imported yet, or the profile combines a "
            f"class with an invalid spec."
        )
    return row


DEFAULT_TOP_N = 15
"""How many ranked loadouts to keep *per hero tree* before clustering."""

DEFAULT_MAX_BUILDS = 256
"""Hard cap on variants per run. ~256 builds × 1000 iter × 32 cores ≈
several minutes on the user's DL380."""

RANKING_SNAPSHOT_TTL_DAYS = 7
"""How long a cached WCL rankings snapshot stays fresh. Past this we
refetch — matches the weekly cadence of the other WCL caches."""

RANKING_POOL_PAGES = 10
"""WCL characterRankings pages to fetch (100 logs each). 10 = top-1000:
enough build diversity for the sweep's screening pool, and a minority
hero tree shows up if anyone runs it."""


@dataclass
class TalentFinderRun:
    """The full output of :func:`build_variants_for_spec_encounter`."""

    spec: GameSpec
    encounter_id: int
    n_logs_considered: int
    """How many ranking entries (with usable talents) we pulled."""

    n_logs_used: int
    """How many loadouts actually went into clusters (after the
    per-hero-tree top-N cap)."""

    threshold_used: float
    """Final threshold after the auto-raise ladder finished."""

    cluster: ClusterResult
    """For UI: hero-tree distribution + per-node classification."""

    builds: list[MaterializedBuild]
    """Ready-to-sim variants — what gets fed to the simc worker."""

    diagnostics: list[str]
    """Human-readable notes — threshold raises, cache hits, etc.;
    surfaced to the UI under the result list."""


# ---------------------------------------------------------------------------
# WCL rankings fetch + snapshot cache
# ---------------------------------------------------------------------------


def _wcl_class_name(class_slug: str) -> str:
    """``death_knight`` → ``DeathKnight`` (WCL's className slug)."""
    return "".join(word.capitalize() for word in class_slug.split("_"))


def _wcl_spec_name(name_en: str) -> str:
    """``Unholy`` → ``Unholy`` (WCL's specName slug)."""
    return "".join(word.capitalize() for word in name_en.split())


async def _wcl_fetch_rankings(
    spec: GameSpec,
    encounter_id: int,
    wcl_client: WclClient | None,
    *,
    pages: int = RANKING_POOL_PAGES,
) -> list[dict]:
    """Fetch up to ``pages`` pages (100 logs each) of characterRankings
    with inline talents.

    Returns a rank-ordered list of
    ``{"rank": int, "amount": float, "talents": [...]}`` — entries with
    no talent data (private logs) are dropped. Stops early if a page
    comes back short (fewer than 100 ranked logs exist).
    """

    async def _one_page(client: WclClient, page: int) -> list[dict]:
        payload = await client.query(
            ENCOUNTER_RANKING_TALENTS,
            {
                "encounterID": encounter_id,
                "className": _wcl_class_name(spec.class_slug),
                "specName": _wcl_spec_name(spec.name_en),
                "metric": "dps",
                "page": page,
            },
        )
        enc = (payload.get("worldData") or {}).get("encounter") or {}
        cr = enc.get("characterRankings") or {}
        return cr.get("rankings") or []

    out: list[dict] = []
    global_rank = 0

    async def _collect(client: WclClient) -> None:
        nonlocal global_rank
        for page in range(1, pages + 1):
            raw = await _one_page(client, page)
            if not raw:
                break  # no more ranked logs
            for r in raw:
                global_rank += 1
                talents = r.get("talents")
                if not talents or not isinstance(talents, list):
                    continue  # private log / no combatant info
                out.append(
                    {
                        "rank": global_rank,
                        "amount": r.get("amount", 0),
                        "talents": talents,
                    }
                )
            if len(raw) < 100:
                break  # last page

    if wcl_client is not None:
        await _collect(wcl_client)
    else:
        async with WclClient() as client:
            await _collect(client)
    return out


async def _get_ranking_snapshot(
    session: AsyncSession,
    spec: GameSpec,
    encounter_id: int,
    *,
    force_refresh: bool,
    wcl_client: WclClient | None,
) -> tuple[list[dict], list[str]]:
    """Return ``(rankings, diagnostics)`` — cached if a fresh snapshot
    exists, freshly fetched (and snapshotted) otherwise.

    If WCL is unreachable but a stale snapshot exists we fall back to it
    rather than failing the whole run.
    """
    snap = await session.get(TalentRankingSnapshot, (spec.slug, encounter_id))
    now = datetime.now(UTC)
    # A snapshot from before the multi-page fetch holds only ~one page;
    # treat those as stale too so the pool actually grows to top-1000.
    big_enough = snap is not None and len(snap.rankings or []) >= 150
    fresh = (
        snap is not None
        and snap.fetched_at is not None
        and (now - snap.fetched_at) < timedelta(days=RANKING_SNAPSHOT_TTL_DAYS)
        and big_enough
    )
    if snap is not None and fresh and not force_refresh:
        age_d = (now - snap.fetched_at).days
        return list(snap.rankings or []), [
            f"using cached WCL rankings ({len(snap.rankings or [])} logs, {age_d}d old)"
        ]

    try:
        rankings = await _wcl_fetch_rankings(spec, encounter_id, wcl_client)
    except Exception as exc:  # noqa: BLE001
        if snap is not None:
            logger.warning(
                "talent-finder: WCL fetch failed, using stale snapshot: %s", exc
            )
            return list(snap.rankings or []), [
                f"WCL fetch failed ({exc.__class__.__name__}); "
                f"using stale snapshot from {snap.fetched_at:%Y-%m-%d}"
            ]
        raise

    # Upsert the snapshot (commits with the caller's transaction).
    stmt = pg_insert(TalentRankingSnapshot).values(
        spec_slug=spec.slug,
        encounter_id=encounter_id,
        fetched_at=now,
        rankings=rankings,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            TalentRankingSnapshot.spec_slug,
            TalentRankingSnapshot.encounter_id,
        ],
        set_={"fetched_at": stmt.excluded.fetched_at, "rankings": stmt.excluded.rankings},
    )
    await session.execute(stmt)
    return rankings, []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def build_variants_for_spec_encounter(
    session: AsyncSession,
    *,
    spec: GameSpec,
    encounter_id: int,
    top_n: int = DEFAULT_TOP_N,
    initial_threshold: float = 0.30,
    max_builds: int = DEFAULT_MAX_BUILDS,
    dataset: TraitDataset | None = None,
    wcl_client: WclClient | None = None,
    force_refresh: bool = False,
) -> TalentFinderRun:
    """End-to-end: WCL rankings → clustered → materialized variant batch.

    Fetches (or reuses a cached snapshot of) the encounter's top-100
    ``characterRankings``, decodes their inline talents, clusters by
    hero tree, and Cartesian-expands the contested slots. Every hero
    tree the meta uses is covered — including minority trees that the
    one-button optimum may actually favour.

    Returns even if zero builds were produced (``builds == []``) so the
    caller can surface the diagnostics.
    """
    if dataset is None:
        dataset = get_dataset()

    rankings, diagnostics = await _get_ranking_snapshot(
        session, spec, encounter_id,
        force_refresh=force_refresh, wcl_client=wcl_client,
    )

    # Decode every ranking entry's talents (rank order preserved).
    decoded: list[DecodedLoadout] = []
    for entry in rankings:
        ld = decoded_from_talent_tree(
            entry.get("talents") or [], spec_id=spec.wcl_spec_id, dataset=dataset
        )
        if ld.selections:
            decoded.append(ld)
    n_considered = len(rankings)

    if not decoded:
        return TalentFinderRun(
            spec=spec, encounter_id=encounter_id,
            n_logs_considered=n_considered, n_logs_used=0,
            threshold_used=initial_threshold,
            cluster=ClusterResult(
                spec_id=spec.wcl_spec_id,
                threshold=initial_threshold,
                n_loadouts_input=n_considered,
                n_loadouts_used=0,
                hero_tree_distribution={},
            ),
            builds=[],
            diagnostics=diagnostics + ["no usable talent data in WCL rankings"],
        )

    blizzard_spec_id = spec.wcl_spec_id

    # Cluster + expand. ``generate_build_variants`` greedily fits the
    # contested-node expansion within ``max_builds`` (most-split nodes
    # first), so no threshold auto-raise is needed — the threshold just
    # defines which nodes count as contested in the first place.
    # ``max_per_hero_tree=None`` → classify over the *whole* top-1000
    # pool: clustering is just counting (no sim cost) and more samples
    # make the consensus/contested split far less noisy.
    cluster = cluster_loadouts(
        decoded, dataset, spec_id=blizzard_spec_id,
        threshold=initial_threshold, max_per_hero_tree=None,
    )
    variants = generate_build_variants(cluster, dataset, max_builds=max_builds)
    builds = materialize_variants(variants, dataset, spec_id=blizzard_spec_id)

    return TalentFinderRun(
        spec=spec,
        encounter_id=encounter_id,
        n_logs_considered=n_considered,
        n_logs_used=sum(c.n_loadouts() for c in cluster.clusters),
        threshold_used=initial_threshold,
        cluster=cluster,
        builds=builds,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Sensitivity sweep — phase-1 plan (baseline + single choice-flips)
# ---------------------------------------------------------------------------


def sweep_loadout_entry(
    build: MaterializedBuild,
    *,
    role: str,
    hero_tree: int,
    spec_id: int,
    variant: dict[int, int] | None = None,
    flip: dict | None = None,
) -> dict:
    """One ``Simulation.loadouts`` entry, tagged so the sweep worker can
    correlate a sim back to its role (baseline / sweep / combine), hero
    tree, and — for baselines — the full build dict / — for flips — the
    flip delta needed to rebuild combine variants in phase 2.

    JSON-safe: ``variant`` keys are stringified (JSON object keys must
    be strings)."""
    entry: dict = {
        "name": build.label,
        "talents": build.simc_block,
        "loadout_code": build.loadout_code,
        "tf_role": role,
        "tf_hero_tree": hero_tree,
        "tf_spec_id": spec_id,
    }
    if variant is not None:
        entry["tf_variant"] = {str(k): v for k, v in variant.items()}
    if flip is not None:
        entry["tf_flip"] = flip
    return entry


SWEEP_SCREEN_POOL_CAP = 400
"""Max distinct builds to screen per run (across all hero trees). A
top-1000 pull dedups to a few hundred genuinely-different builds; this
caps the phase-0 sim count so a run stays bounded even for a wildly
divergent spec."""


@dataclass
class SweepScreenPool:
    """Phase-0 plan for a sensitivity-sweep run."""

    spec: GameSpec
    encounter_id: int
    loadouts: list[dict]
    """``Simulation.loadouts`` entries — the distinct real meta builds
    to screen under one-button, each tagged ``tf_role="screen"`` via
    :func:`sweep_loadout_entry`. The worker sims these, picks the best
    per hero tree as the sweep baseline, then generates choice-flips."""

    diagnostics: list[str]


async def build_sweep_screen_pool(
    session: AsyncSession,
    *,
    spec: GameSpec,
    encounter_id: int,
    dataset: TraitDataset | None = None,
    wcl_client: WclClient | None = None,
    force_refresh: bool = False,
) -> SweepScreenPool:
    """Build the phase-0 screening pool for a sweep run.

    Pulls the top-1000 WCL logs, dedups to the distinct real builds,
    groups them by hero tree. The worker sims every one under
    one-button (cheap screen), picks the best per tree as the sweep
    baseline — sidestepping the bad-baseline trap, since the pool's
    real builds already cover the meta's point-allocation diversity.
    """
    if dataset is None:
        dataset = get_dataset()

    rankings, diagnostics = await _get_ranking_snapshot(
        session, spec, encounter_id,
        force_refresh=force_refresh, wcl_client=wcl_client,
    )
    decoded: list[DecodedLoadout] = []
    for entry in rankings:
        ld = decoded_from_talent_tree(
            entry.get("talents") or [], spec_id=spec.wcl_spec_id, dataset=dataset
        )
        if ld.selections:
            decoded.append(ld)

    if not decoded:
        return SweepScreenPool(
            spec=spec, encounter_id=encounter_id, loadouts=[],
            diagnostics=diagnostics + ["no usable talent data in WCL rankings"],
        )

    # Group by hero tree, dedup to distinct builds (rank order kept).
    by_tree: dict[int, list[DecodedLoadout]] = {}
    for ld in decoded:
        sub = hero_sub_tree(ld)
        if sub is None or sub <= 0:
            continue
        by_tree.setdefault(sub, []).append(ld)

    loadouts: list[dict] = []
    total = 0
    for sub, lds in sorted(by_tree.items(), key=lambda kv: -len(kv[1])):
        distinct = distinct_builds(lds)
        if total + len(distinct) > SWEEP_SCREEN_POOL_CAP:
            distinct = distinct[: max(0, SWEEP_SCREEN_POOL_CAP - total)]
        if not distinct:
            continue
        total += len(distinct)
        variants = [loadout_variant(d) for d in distinct]
        builds = materialize_variants(
            variants, dataset, spec_id=spec.wcl_spec_id,
            label_prefix=f"st{sub}s",
        )
        for variant, build in zip(variants, builds):
            loadouts.append(
                sweep_loadout_entry(
                    build, role="screen", hero_tree=sub,
                    spec_id=spec.wcl_spec_id, variant=variant,
                )
            )

    if not loadouts:
        diagnostics.append("no hero-tree builds to screen")
    else:
        diagnostics.append(
            f"screening {len(loadouts)} distinct builds "
            f"across {len(by_tree)} hero tree(s)"
        )
    return SweepScreenPool(
        spec=spec, encounter_id=encounter_id,
        loadouts=loadouts, diagnostics=diagnostics,
    )


__all__ = [
    "DEFAULT_MAX_BUILDS",
    "DEFAULT_TOP_N",
    "RANKING_SNAPSHOT_TTL_DAYS",
    "SWEEP_SCREEN_POOL_CAP",
    "SweepScreenPool",
    "TalentFinderRun",
    "build_sweep_screen_pool",
    "build_variants_for_spec_encounter",
    "sweep_loadout_entry",
]
