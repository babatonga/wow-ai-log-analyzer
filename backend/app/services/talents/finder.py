"""Talent-Finder: derive a small set of high-quality build variants from
a cluster of top-performer loadouts.

Pipeline overview (this module is step *3* in the chain):

  WCL  →  decoder.py  →  **finder.py (HERE)**  →  encoder.py  →  simc

We expect the caller to hand us a list of :class:`DecodedLoadout`
objects for the same spec, then to feed our generated variants back
through :func:`encode_loadout` + :func:`build_talent_block` for the
actual simc input.

The core idea:

* Most slots in a top-15 cluster are *consensus* — every top performer
  picks the same option. We freeze those.
* A few slots are genuinely *contested* — meaningful disagreement among
  top performers. We expand those into a Cartesian product.
* The rest are *minority* noise (one player's quirky pick). We discard.
* Hero-tree picks split into clusters first: if both trees are well
  represented we analyse each cluster separately and merge their
  variants at the end.

A "node" here is a TraitNode (one slot on the tree). It can hold:

* A normal/tiered talent (1 entry, possibly variable rank)
* A choice node (2 entries, pick one)
* A selection node (hero-tree gateway)
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import product
from typing import Iterable, Literal

from app.services.talents.decoder import DecodedLoadout, SelectedEntry, decode_loadout
from app.services.talents.encoder import encode_loadout
from app.services.talents.simc_input import build_talent_block
from app.services.talents.trait_data import (
    NODE_CHOICE,
    NODE_TIERED,
    TREE_HERO,
    TREE_SELECTION,
    TraitDataset,
)

logger = logging.getLogger(__name__)

Classification = Literal["consensus", "contested", "minority_only"]
"""How a node was classified after counting picks across the cluster.

* ``consensus`` — at least ``1 - threshold`` of loadouts agree on one
  (entry_id, rank) combination. We freeze that pick.
* ``contested`` — multiple combinations each cross the threshold. We
  enumerate all that do.
* ``minority_only`` — fewer loadouts touch this node than the
  threshold demands. We drop the node entirely (it isn't picked by the
  cluster majority — likely a low-value talent for this build).
"""


# A "PickBundle" is the *complete* allocation a single loadout made at one
# node. For choice nodes that's a single ``(entry_id, 1)`` pair. For tiered
# nodes it's the set of ``(entry_id, rank)`` pairs the loadout invested
# points into. Two loadouts agree at a node iff their bundles are equal —
# this unifies choice/tiered/normal under one comparison.
PickBundle = frozenset[tuple[int, int]]

# The "absent" bundle (= loadout chose not to take this node at all). Worth
# tracking separately because "skip this slot" can itself be a contested
# decision in the cluster.
EMPTY_BUNDLE: PickBundle = frozenset()


@dataclass
class NodePicks:
    """Aggregated bundles at a single tree node across the cluster."""

    node_id: int
    tree_index: int
    sub_tree_id: int
    node_type: int
    name: str
    """Display name of the (first) entry — for log/debug output."""

    counts: Counter = field(default_factory=Counter)
    """:class:`PickBundle` → number of loadouts that chose exactly this bundle."""

    n_total: int = 0
    """How many loadouts in the cluster could have picked this node
    (= the cluster size). Used as the denominator for thresholds."""

    classification: Classification = "minority_only"
    consensus_pick: PickBundle | None = None
    contested_picks: list[PickBundle] = field(default_factory=list)

    @property
    def is_choice(self) -> bool:
        return self.node_type == NODE_CHOICE

    @property
    def is_tiered(self) -> bool:
        return self.node_type == NODE_TIERED

    def pick_ratio(self, bundle: PickBundle) -> float:
        return self.counts[bundle] / self.n_total if self.n_total else 0.0

    @property
    def contest_strength(self) -> int:
        """How genuinely split this node is — the support count of the
        *runner-up* bundle. A node where the 2nd pick has 7/15 backing
        is more worth exploring than one where it has 5/15. Zero for
        consensus/minority nodes (only relevant for contested ones)."""
        if len(self.contested_picks) < 2:
            return 0
        return self.counts[self.contested_picks[1]]


@dataclass
class HeroTreeCluster:
    """A sub-set of the input loadouts that all chose the same hero tree.

    We analyse contested vs consensus *within* a hero-tree cluster
    because the rest of the build often correlates with the hero
    choice. Variants generated for tree A do not get mixed with picks
    that only make sense in tree B.
    """

    sub_tree_id: int
    sub_tree_name: str
    loadouts: list[DecodedLoadout]
    nodes: dict[int, NodePicks] = field(default_factory=dict)
    """node_id → NodePicks (only nodes any loadout in this cluster touched)."""

    def n_loadouts(self) -> int:
        return len(self.loadouts)


@dataclass
class ClusterResult:
    """The full output of :func:`cluster_loadouts`."""

    spec_id: int
    threshold: float
    n_loadouts_input: int
    n_loadouts_used: int
    """Input minus ones we couldn't classify (no hero-tree, no entries)."""

    hero_tree_distribution: dict[int, int]
    """sub_tree_id → loadout count (across the *whole* input, before threshold)."""

    clusters: list[HeroTreeCluster] = field(default_factory=list)
    """Hero-tree clusters that survived the cluster threshold and will
    be expanded into variant builds."""

    dropped_hero_trees: dict[int, int] = field(default_factory=dict)
    """sub_tree_id → count for hero trees below the threshold (logged
    but not expanded)."""

    def total_variants(self) -> int:
        return sum(_cluster_variant_count(c) for c in self.clusters)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cluster_loadouts(
    loadouts: list[DecodedLoadout],
    dataset: TraitDataset,
    *,
    spec_id: int,
    threshold: float = 0.30,
    max_per_hero_tree: int | None = None,
) -> ClusterResult:
    """Group loadouts by hero tree and classify every node's picks.

    Parameters
    ----------
    loadouts:
        Decoded loadouts, **rank-ordered** (best DPS first). Loadouts
        for a different spec are silently dropped (logged at INFO).
    dataset:
        Trait dataset used to look up node metadata (name, type).
    spec_id:
        The target spec. Loadouts must match.
    threshold:
        Per-node consensus threshold *within* a hero-tree cluster: a
        pick is "supported" iff at least ``ceil(threshold * n)``
        loadouts in the cluster agree on it. ``0.30`` ≙ "5-of-15".
    max_per_hero_tree:
        Keep at most this many loadouts per hero tree (the top ones,
        since input is rank-ordered). ``None`` = no cap. Every hero
        tree the meta uses becomes its own cluster regardless of how
        small — a one-button optimum can live on a minority tree, so
        we never drop a tree by popularity.

    Returns
    -------
    :class:`ClusterResult`. Empty clusters list if no input survives.
    """
    if not 0.0 < threshold < 1.0:
        raise ValueError(f"threshold must be in (0,1), got {threshold}")

    # 1. Filter by spec and require at least one HERO selection so we
    #    have a basis for the hero-tree split.
    relevant: list[DecodedLoadout] = []
    for ld in loadouts:
        if ld.spec_id != spec_id:
            logger.info(
                "talent-finder: drop loadout for spec=%s (need %s)",
                ld.spec_id, spec_id,
            )
            continue
        if not any(s.tree_index == TREE_HERO for s in ld.selections):
            logger.info("talent-finder: drop loadout with no hero-tree picks")
            continue
        relevant.append(ld)

    if not relevant:
        return ClusterResult(
            spec_id=spec_id,
            threshold=threshold,
            n_loadouts_input=len(loadouts),
            n_loadouts_used=0,
            hero_tree_distribution={},
        )

    # 2. Compute the hero-tree distribution and split into clusters.
    hero_dist: Counter[int] = Counter()
    by_tree: dict[int, list[DecodedLoadout]] = defaultdict(list)
    for ld in relevant:
        sub = _hero_sub_tree(ld)
        if sub is None:
            continue
        hero_dist[sub] += 1
        by_tree[sub].append(ld)

    # 3. Build one cluster per hero tree. Every tree the meta uses is
    #    kept — we never drop a tree by popularity, because the
    #    one-button optimum can sit on a minority hero tree the
    #    manual-play meta doesn't favour. Each tree is capped to its
    #    top ``max_per_hero_tree`` loadouts (input is rank-ordered).
    clusters: list[HeroTreeCluster] = []
    for sub, lds in by_tree.items():
        capped = lds[:max_per_hero_tree] if max_per_hero_tree else lds
        clusters.append(
            HeroTreeCluster(
                sub_tree_id=sub,
                sub_tree_name=_sub_tree_name(dataset, sub),
                loadouts=capped,
            )
        )

    # 4. Classify every node touched by any loadout in each cluster.
    for cluster in clusters:
        _classify_cluster(cluster, dataset, threshold=threshold)

    return ClusterResult(
        spec_id=spec_id,
        threshold=threshold,
        n_loadouts_input=len(loadouts),
        n_loadouts_used=sum(c.n_loadouts() for c in clusters),
        hero_tree_distribution=dict(hero_dist),
        clusters=clusters,
        dropped_hero_trees={},
    )


def generate_build_variants(
    cluster: ClusterResult,
    dataset: TraitDataset,
    *,
    max_builds: int = 1024,
) -> list[dict[int, int]]:
    """Expand the contested nodes into ready-to-sim variant dicts.

    Each generated variant is a ``{entry_id: rank}`` dict ready for
    :func:`encode_loadout`. Consensus picks are baked in.

    A fully Cartesian expansion of every contested node would blow up
    exponentially (2^N for N contested choice nodes). Instead, within
    each hero-tree cluster we expand only the *most contested* nodes —
    sorted by how strongly the runner-up pick is backed — and freeze
    the rest at their majority pick, stopping once the build budget for
    that cluster is used up. The genuinely-split slots get explored;
    the near-unanimous ones don't waste sim time.

    ``max_builds`` is the total budget, split evenly across the hero
    clusters. The result is always ``<= max_builds``.
    """
    clusters = cluster.clusters or []
    if not clusters:
        return []
    variants: list[dict[int, int]] = []
    remaining_budget = max_builds
    remaining_clusters = len(clusters)
    for hc in clusters:
        per_cluster = max(1, remaining_budget // remaining_clusters)
        cv = _cluster_variants(hc, dataset, budget=per_cluster)
        variants.extend(cv)
        remaining_budget -= len(cv)
        remaining_clusters -= 1
    return variants


class BuildExplosionError(RuntimeError):
    """Kept for API back-compat. No longer raised — :func:`generate_build_variants`
    greedily fits within the budget instead of aborting."""


@dataclass(frozen=True)
class MaterializedBuild:
    """One ready-to-sim variant.

    * ``variant`` — the flat ``{entry_id: rank}`` dict (input format for
      :func:`encode_loadout`).
    * ``loadout_code`` — base64 Blizzard loadout string. Use this for
      the UI's "copy to clipboard" export-string.
    * ``simc_block`` — multi-line ``class_talents=/spec_talents=/hero_talents=``
      text suitable for splicing into a simc profile.
    * ``label`` — stable identifier ("v0001" etc.) we feed simcs
      ``profileset`` so results can be correlated back.
    """

    label: str
    variant: dict[int, int]
    loadout_code: str
    simc_block: str


def materialize_variants(
    variants: list[dict[int, int]],
    dataset: TraitDataset,
    *,
    spec_id: int,
    label_prefix: str = "tf",
) -> list[MaterializedBuild]:
    """Encode every variant dict into the two forms simc and the UI need.

    Internally we round-trip through the Blizzard codec: encode → decode
    → render. The decode pass adds the hero-tree gateway anchor (same
    auto-fix the rest of our codebase relies on) so the produced simc
    block is always complete enough for simc to accept.
    """
    out: list[MaterializedBuild] = []
    for i, variant in enumerate(variants):
        code = encode_loadout(spec_id=spec_id, selected=variant, dataset=dataset)
        decoded = decode_loadout(code, dataset=dataset)
        block = build_talent_block(decoded)
        out.append(
            MaterializedBuild(
                label=f"{label_prefix}{i + 1:04d}",
                variant=variant,
                loadout_code=code,
                simc_block=block,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Sensitivity sweep — sim-driven, one-button-grounded talent search
# ---------------------------------------------------------------------------
#
# The cluster approach above mines the *manual-play* meta. But the
# one-button optimum can sit on a talent the whole meta agrees on (a
# consensus node the cluster freezes). The sweep fixes that: start from
# the meta-consensus build, flip each choice node one at a time, and let
# the simulator measure which flips actually gain DPS under one-button.
# No talent->spell dependency graph, no AI — the sim is ground truth.


@dataclass(frozen=True)
class TalentFlip:
    """A single-talent change relative to a baseline build.

    ``remove`` / ``add`` are ``(entry_id, rank)`` pairs — applying the
    flip drops ``remove`` from the baseline and sets ``add``. Stage 1
    only flips CHOICE nodes (pick the sibling entry), so both are
    rank-1 singletons.
    """

    node_id: int
    node_name: str
    remove: tuple[int, int]
    add: tuple[int, int]


def consensus_baseline(cluster: HeroTreeCluster) -> dict[int, int]:
    """The single most-representative build for a hero-tree cluster.

    Every consensus pick, plus the *majority* pick at every contested
    node. Minority-only nodes are dropped. Returned as a flat
    ``{entry_id: rank}`` dict — the sweep's starting point. Costs no
    sims.
    """
    base: dict[int, int] = {}
    for np_ in cluster.nodes.values():
        if np_.classification == "consensus" and np_.consensus_pick is not None:
            for entry_id, rank in np_.consensus_pick:
                base[entry_id] = rank
        elif np_.classification == "contested" and np_.contested_picks:
            for entry_id, rank in np_.contested_picks[0]:  # most-supported
                base[entry_id] = rank
    return base


def apply_flips(
    baseline: dict[int, int], flips: Iterable[TalentFlip]
) -> dict[int, int]:
    """Return ``baseline`` with every flip applied (non-mutating)."""
    variant = dict(baseline)
    for f in flips:
        variant.pop(f.remove[0], None)
        variant[f.add[0]] = f.add[1]
    return variant


def enumerate_choice_flips(
    baseline: dict[int, int],
    cluster: HeroTreeCluster,
    dataset: TraitDataset,
) -> list[TalentFlip]:
    """Every single-choice-node flip available from ``baseline``.

    For each CHOICE node the baseline cleanly picks one entry of, emit
    a flip to each sibling entry. Nodes the baseline skips entirely are
    left for Stage 2 (point reallocation).
    """
    flips: list[TalentFlip] = []
    for np_ in cluster.nodes.values():
        if np_.node_type != NODE_CHOICE:
            continue
        entries = dataset.entries_at_node(np_.node_id)
        picked = [
            (e.entry_id, baseline[e.entry_id])
            for e in entries
            if baseline.get(e.entry_id, 0) > 0
        ]
        if len(picked) != 1:
            continue  # baseline doesn't cleanly pick one — skip
        cur_id, cur_rank = picked[0]
        for alt in entries:
            if alt.entry_id == cur_id:
                continue
            flips.append(
                TalentFlip(
                    node_id=np_.node_id,
                    node_name=np_.name,
                    remove=(cur_id, cur_rank),
                    add=(alt.entry_id, 1),
                )
            )
    return flips


def combine_flip_variants(
    baseline: dict[int, int],
    flips: list[TalentFlip],
    *,
    max_builds: int,
) -> list[dict[int, int]]:
    """Cartesian-combine the given flips into variant dicts.

    Each flip is an independent on/off toggle, so N flips yield 2^N
    variants (the empty subset == the baseline itself). Capped: only
    the first ``floor(log2(max_builds))`` flips are combined — callers
    should pass ``flips`` already sorted best-delta-first so the cap
    keeps the most promising ones.
    """
    if not flips:
        return [dict(baseline)]
    k = min(len(flips), max(0, max_builds.bit_length() - 1))
    selected = flips[:k]
    variants: list[dict[int, int]] = []
    for mask in range(1 << len(selected)):
        subset = [selected[i] for i in range(len(selected)) if mask & (1 << i)]
        variants.append(apply_flips(baseline, subset))
    return variants


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _hero_sub_tree(ld: DecodedLoadout) -> int | None:
    """The hero sub_tree_id this loadout is locked into.

    We prefer the TREE_SELECTION anchor (always present after the
    decoder re-adds it for stale loadouts), and fall back to the first
    HERO selection if for some reason the anchor is absent.
    """
    for sel in ld.selections:
        if sel.tree_index == TREE_SELECTION and sel.sub_tree_id > 0:
            return sel.sub_tree_id
    for sel in ld.selections:
        if sel.tree_index == TREE_HERO and sel.sub_tree_id > 0:
            return sel.sub_tree_id
    return None


def _sub_tree_name(dataset: TraitDataset, sub_tree_id: int) -> str:
    """Pick a human-readable label for the hero tree.

    TREE_SELECTION entries in our dataset have empty names (the .inc
    parser reads them as the literal string "0"). The reliable label
    is the row-1 HERO talent — that's the hero-tree gateway ("Rider's
    Champion", "Vampiric Strike", "Aldrachi Reaver", ...).
    """
    candidates = [
        e for e in dataset.entries
        if e.sub_tree_id == sub_tree_id
        and e.tree_index == TREE_HERO
        and e.row > 0
    ]
    if not candidates:
        return f"sub_tree_{sub_tree_id}"
    candidates.sort(key=lambda e: (e.row, e.col, e.selection_index, e.entry_id))
    return candidates[0].name


def _classify_cluster(
    cluster: HeroTreeCluster,
    dataset: TraitDataset,
    *,
    threshold: float,
) -> None:
    """Aggregate per-node bundles across the cluster and classify."""
    n = cluster.n_loadouts()

    # First pass: per (loadout, node) collect the full bundle of
    # (entry_id, rank) pairs that loadout chose at that node. For
    # tiered nodes this is the multi-entry allocation; for choice nodes
    # it's a singleton; for unpicked nodes it's the empty bundle.
    bundles_per_loadout: list[dict[int, set[tuple[int, int]]]] = []
    nodes_touched: set[int] = set()
    for ld in cluster.loadouts:
        per_node: dict[int, set[tuple[int, int]]] = defaultdict(set)
        for sel in ld.selections:
            if sel.is_granted:
                continue
            per_node[sel.node_id].add((sel.entry_id, sel.rank))
            nodes_touched.add(sel.node_id)
        bundles_per_loadout.append(per_node)

    # Second pass: for every touched node, count bundle frequencies
    # across all loadouts (loadouts that didn't touch the node
    # contribute the empty bundle).
    min_support = max(1, math.ceil(threshold * n))
    for node_id in nodes_touched:
        entries = dataset.entries_at_node(node_id)
        if not entries:
            logger.warning("talent-finder: node %s missing from dataset", node_id)
            continue
        counter: Counter[PickBundle] = Counter()
        for per_node in bundles_per_loadout:
            bundle: PickBundle = frozenset(per_node.get(node_id, set()))
            counter[bundle] += 1

        ref = entries[0]
        np_ = NodePicks(
            node_id=node_id,
            tree_index=ref.tree_index,
            sub_tree_id=ref.sub_tree_id,
            node_type=ref.node_type,
            name=ref.name,
            counts=counter,
            n_total=n,
        )

        # A bundle is "supported" iff at least ceil(threshold * n)
        # loadouts agree on it. Empty bundle ("skip this slot") is a
        # valid supported choice — sometimes the meta is "don't pick
        # this filler at all".
        supported = [b for b, c in counter.items() if c >= min_support]
        if len(supported) == 0:
            np_.classification = "minority_only"
        elif len(supported) == 1:
            np_.classification = "consensus"
            np_.consensus_pick = supported[0]
        else:
            np_.classification = "contested"
            supported.sort(
                key=lambda b: (-counter[b], sorted(b)),
            )
            np_.contested_picks = supported

        cluster.nodes[node_id] = np_


def _cluster_variants(
    cluster: HeroTreeCluster,
    dataset: TraitDataset,
    *,
    budget: int,
) -> list[dict[int, int]]:
    """Expand one hero-tree cluster into at most ``budget`` variants.

    Consensus picks are baked into every variant. Contested nodes are
    sorted by :attr:`NodePicks.contest_strength` (most genuinely-split
    first); we Cartesian-expand them greedily while the running product
    stays within ``budget``, and freeze the rest at their majority
    pick. A variant is a flat ``{entry_id: rank}`` dict.
    """
    base: dict[int, int] = {}
    contested: list[NodePicks] = []

    for np_ in cluster.nodes.values():
        if np_.classification == "consensus":
            assert np_.consensus_pick is not None
            for entry_id, rank in np_.consensus_pick:
                base[entry_id] = rank
        elif np_.classification == "contested":
            contested.append(np_)
        # minority_only: drop the node — meta doesn't support it

    # Most genuinely-contested first; ties broken by node_id for
    # determinism.
    contested.sort(key=lambda n: (-n.contest_strength, n.node_id))

    axes: list[list[PickBundle]] = []
    product_size = 1
    for np_ in contested:
        n_picks = len(np_.contested_picks)
        if n_picks > 1 and product_size * n_picks <= budget:
            axes.append(np_.contested_picks)
            product_size *= n_picks
        else:
            # No budget left for this axis — freeze at the majority pick
            # (contested_picks[0] is the most-supported bundle).
            for entry_id, rank in np_.contested_picks[0]:
                base[entry_id] = rank

    if not axes:
        return [dict(base)]

    variants: list[dict[int, int]] = []
    for combo in product(*axes):
        v = dict(base)
        for bundle in combo:
            for entry_id, rank in bundle:
                v[entry_id] = rank
        variants.append(v)
    return variants


def _cluster_variant_count(cluster: HeroTreeCluster) -> int:
    """Upper-bound variant count if every contested node were expanded
    (used by the CLI report — the real generator caps this)."""
    n = 1
    for np_ in cluster.nodes.values():
        if np_.classification == "contested":
            n *= len(np_.contested_picks)
    return n


# ---------------------------------------------------------------------------
# CLI demo: inspect cluster shape for synthetic input
# ---------------------------------------------------------------------------


def _print_report(cluster: ClusterResult) -> None:
    """Pretty-print a cluster result for human inspection."""
    print(
        f"\nTalent-Finder cluster report  "
        f"(spec={cluster.spec_id}, threshold={cluster.threshold:g})"
    )
    print(
        f"  loadouts: {cluster.n_loadouts_used}/{cluster.n_loadouts_input} usable"
    )
    print(f"  hero-tree distribution: {dict(cluster.hero_tree_distribution)}")
    if cluster.dropped_hero_trees:
        print(f"  dropped (below threshold): {cluster.dropped_hero_trees}")
    print(f"  total variants to sim: {cluster.total_variants()}")

    for hc in cluster.clusters:
        contested = [np_ for np_ in hc.nodes.values() if np_.classification == "contested"]
        consensus = sum(1 for np_ in hc.nodes.values() if np_.classification == "consensus")
        minority = sum(1 for np_ in hc.nodes.values() if np_.classification == "minority_only")
        print(
            f"\n  Hero tree '{hc.sub_tree_name}' (sub_tree={hc.sub_tree_id}, "
            f"loadouts={hc.n_loadouts()}):"
        )
        print(
            f"    consensus: {consensus}   contested: {len(contested)}   minority: {minority}   "
            f"variants: {_cluster_variant_count(hc)}"
        )
        for np_ in contested:
            picks = " | ".join(
                _format_bundle(b, hc.nodes[np_.node_id].counts[b])
                for b in np_.contested_picks
            )
            print(f"    - contested node {np_.node_id} ({np_.name}): {picks}")


def _format_bundle(bundle: PickBundle, count: int) -> str:
    if not bundle:
        return f"<skip>×{count}"
    parts = ",".join(f"{eid}/r{r}" for eid, r in sorted(bundle))
    return f"[{parts}]×{count}"


if __name__ == "__main__":  # pragma: no cover
    # Tiny smoke test against a couple of hard-coded loadout codes.
    # Replace with real WCL-extracted codes once the fetcher exists.
    import sys
    from pathlib import Path

    from app.services.talents.decoder import decode_loadout
    from app.services.talents.trait_data import load_trait_data

    if len(sys.argv) < 3:
        print(
            "usage: python -m app.services.talents.finder <spec_id> <loadout> [loadout ...]",
            file=sys.stderr,
        )
        sys.exit(2)

    spec = int(sys.argv[1])
    codes = sys.argv[2:]
    ds = load_trait_data(
        Path(__file__).parent / "trait_data.inc"
    )
    decoded = [decode_loadout(c, dataset=ds) for c in codes]

    # Try progressively stricter thresholds: real WCL clusters of 15
    # will pass at 0.30, but a tiny 3-4 loadout demo needs a strict
    # threshold to avoid combinatorial explosion.
    for thresh in (0.30, 0.51, 0.67, 0.80):
        result = cluster_loadouts(decoded, ds, spec_id=spec, threshold=thresh)
        _print_report(result)
        try:
            variants = generate_build_variants(result, ds, max_builds=64)
        except BuildExplosionError as exc:
            print(f"\n  (variant explosion at threshold={thresh:g}: {exc})")
            continue
        builds = materialize_variants(variants, ds, spec_id=spec)
        print(f"\nMaterialized {len(builds)} build(s) at threshold={thresh:g} "
              f"(first 2 shown):")
        for b in builds[:2]:
            print(f"\n  {b.label}  loadout={b.loadout_code[:80]}")
            for line in b.simc_block.splitlines():
                print(f"    | {line[:100]}")
        break
