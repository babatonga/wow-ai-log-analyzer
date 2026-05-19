"""Talent-finder orchestrator: TopLog → MaterializedBuild list.

This service stitches together the pieces:

  TopLog rows (cached)
        ↓ extract talents_loadout
  Blizzard base64 codes
        ↓ decode_loadout
  list[DecodedLoadout]
        ↓ cluster_loadouts (consensus / contested / minority)
  ClusterResult
        ↓ generate_build_variants (Cartesian over contested)
  list[variant: dict[entry_id, rank]]
        ↓ materialize_variants (round-trip via encode/decode)
  list[MaterializedBuild]    ← what the simc worker consumes

The "auto threshold raise" behaviour: if the user's chosen threshold
produces a variants explosion (raised by ``generate_build_variants``),
we step the threshold up and retry until the build count fits. This is
the same intuition the user expressed during scope discussion — if too
many slots look contested at the strict threshold, demand stronger
consensus until the search space is manageable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameSpec, TopLog
from app.services.talents import (
    DecodedLoadout,
    TraitDataset,
    decode_loadout,
    get_dataset,
)
from app.services.talents.decoder import TalentDecodeError
from app.services.talents.finder import (
    BuildExplosionError,
    ClusterResult,
    MaterializedBuild,
    cluster_loadouts,
    generate_build_variants,
    materialize_variants,
)

logger = logging.getLogger(__name__)

# Threshold ladder used when the first attempt explodes. Strict-er
# values demand stronger consensus among top performers; 0.95 means
# "near unanimous" and produces the smallest variant set.
_THRESHOLD_LADDER = (0.30, 0.50, 0.67, 0.80, 0.95)

DEFAULT_TOP_N = 15
"""How many ranking rows to pull when the caller doesn't override."""

DEFAULT_MAX_BUILDS = 256
"""Hard cap on variants per run. ~256 builds × 1000 iter × 32 cores ≈
several minutes on the user's DL380."""


@dataclass
class TalentFinderRun:
    """The full output of :func:`build_variants_for_spec_encounter`."""

    spec: GameSpec
    encounter_id: int
    n_logs_considered: int
    """How many TopLog rows we pulled before filtering."""

    n_logs_used: int
    """How many had a usable base64 loadout."""

    threshold_used: float
    """Final threshold after the auto-raise ladder finished."""

    cluster: ClusterResult
    """For UI: hero-tree distribution + per-node classification."""

    builds: list[MaterializedBuild]
    """Ready-to-sim variants — what gets fed to the simc worker."""

    diagnostics: list[str]
    """Human-readable notes — e.g. "raised threshold to 0.50 after
    explosion at 0.30"; surfaced to the UI under the result list."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def build_variants_for_spec_encounter(
    session: AsyncSession,
    *,
    spec: GameSpec,
    encounter_id: int,
    metric: str = "dps",
    top_n: int = DEFAULT_TOP_N,
    initial_threshold: float = 0.30,
    max_builds: int = DEFAULT_MAX_BUILDS,
    dataset: TraitDataset | None = None,
) -> TalentFinderRun:
    """End-to-end: pick up cached TopLogs and produce a variant batch.

    Assumes the top-logs table is already populated for this (spec,
    encounter, metric) by the periodic refresh. Returns even if zero
    builds were produced (``builds == []``) so the caller can surface
    the diagnostic.
    """
    if dataset is None:
        dataset = get_dataset()

    rows = await _load_top_logs(
        session, spec_slug=spec.slug, encounter_id=encounter_id,
        metric=metric, limit=top_n,
    )
    n_considered = len(rows)

    decoded, decode_diagnostics = _decode_all(rows, dataset)
    n_used = len(decoded)

    diagnostics: list[str] = list(decode_diagnostics)

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
            diagnostics=diagnostics + ["no decodable loadouts in top logs"],
        )

    # Use the first decoded loadout to pin the Blizzard spec_id. This
    # protects against a stale ``wcl_spec_id`` value if WCLs and
    # Blizzards id schemes ever diverge on a new spec.
    blizzard_spec_id = decoded[0].spec_id
    if blizzard_spec_id != spec.wcl_spec_id:
        diagnostics.append(
            f"warning: WCL spec_id={spec.wcl_spec_id} differs from "
            f"loadout-encoded {blizzard_spec_id}; trusting the loadout"
        )

    # Auto-raise threshold until the variant count fits under max_builds.
    cluster: ClusterResult | None = None
    builds: list[MaterializedBuild] = []
    chosen_thresh = initial_threshold
    ladder = [t for t in _THRESHOLD_LADDER if t >= initial_threshold]
    if initial_threshold not in ladder:
        ladder = [initial_threshold] + ladder

    for thresh in ladder:
        cluster = cluster_loadouts(
            decoded, dataset, spec_id=blizzard_spec_id, threshold=thresh,
        )
        try:
            variants = generate_build_variants(cluster, dataset, max_builds=max_builds)
        except BuildExplosionError as exc:
            diagnostics.append(
                f"threshold {thresh:g} produced too many variants — "
                f"trying a stricter consensus ({exc.args[0] if exc.args else ''})"
            )
            continue
        builds = materialize_variants(variants, dataset, spec_id=blizzard_spec_id)
        chosen_thresh = thresh
        break
    else:
        # All thresholds exploded — should be impossible since 0.95
        # demands near-unanimity, but handle gracefully.
        diagnostics.append(
            "every threshold up to 0.95 produced too many variants — "
            "your top-N is too small or too divergent; aborting"
        )
        cluster = cluster_loadouts(
            decoded, dataset, spec_id=blizzard_spec_id, threshold=0.95,
        )

    assert cluster is not None  # for type narrowing

    if chosen_thresh != initial_threshold:
        diagnostics.append(
            f"final threshold: {chosen_thresh:g} (raised from {initial_threshold:g})"
        )

    return TalentFinderRun(
        spec=spec,
        encounter_id=encounter_id,
        n_logs_considered=n_considered,
        n_logs_used=n_used,
        threshold_used=chosen_thresh,
        cluster=cluster,
        builds=builds,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_top_logs(
    session: AsyncSession,
    *,
    spec_slug: str,
    encounter_id: int,
    metric: str,
    limit: int,
) -> list[TopLog]:
    """Return TopLog rows for (spec, encounter, metric), best ranks first."""
    stmt = (
        select(TopLog)
        .where(
            TopLog.spec_slug == spec_slug,
            TopLog.encounter_id == encounter_id,
            TopLog.metric == metric,
        )
        .order_by(TopLog.rank.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


def _decode_all(
    rows: Iterable[TopLog],
    dataset: TraitDataset,
) -> tuple[list[DecodedLoadout], list[str]]:
    """Decode every TopLog's stored loadout code we can.

    Skips rows where:

    * ``detail_payload`` is missing or has no ``talents_loadout`` field
    * ``talents_loadout`` is the legacy list-of-dicts shape (we'd need
      a separate codec to handle that — out of scope for v1)
    * the base64 decode itself fails

    Returns the successfully-decoded loadouts plus per-skip
    diagnostics so the UI can explain why fewer than top-N usable
    loadouts came back.
    """
    decoded: list[DecodedLoadout] = []
    diag: list[str] = []
    for r in rows:
        detail = r.detail_payload or {}
        loadout = detail.get("talents_loadout") if isinstance(detail, dict) else None
        if not loadout:
            diag.append(
                f"rank {r.rank} ({r.character_name}): no talent loadout in detail"
            )
            continue
        if not isinstance(loadout, str):
            # Legacy combatantInfo.talentTree shape — list of dicts.
            # Building a Blizzard base64 from this is doable (we have
            # the encoder) but needs node_id → entry_id mapping; punt
            # until we hit logs that don't carry the modern string.
            diag.append(
                f"rank {r.rank} ({r.character_name}): legacy talentTree "
                "shape (not yet supported)"
            )
            continue
        try:
            decoded.append(decode_loadout(loadout, dataset=dataset))
        except TalentDecodeError as exc:
            diag.append(
                f"rank {r.rank} ({r.character_name}): decode failed — {exc}"
            )
            logger.warning(
                "talent-finder: decode failed for top_log id=%s rank=%s: %s",
                r.id, r.rank, exc,
            )
    return decoded, diag


__all__ = [
    "DEFAULT_MAX_BUILDS",
    "DEFAULT_TOP_N",
    "TalentFinderRun",
    "build_variants_for_spec_encounter",
]
