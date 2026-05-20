"""Unit tests for the Talent-Finder cluster analyzer (finder.py).

Test corpus reuses the real saved-loadout codes from
``test_talent_decoder.py``: four Unholy-DK builds (one Sanlayn hero
tree, three Rider) and three Windwalker builds (all one hero tree).
That gives us a hero-tree split to exercise and a clean
single-cluster case.
"""
from __future__ import annotations

import pytest

from app.services.talents import decode_loadout, get_dataset
from app.services.talents.finder import (
    cluster_loadouts,
    generate_build_variants,
    materialize_variants,
)

# Unholy DK (spec 252): 1× Sanlayn (sub_tree 31), 3× Rider (sub_tree 32).
DEADFOX_CODES = [
    "CwPAkXBWxkyfx9CbGaHonEAhLBwMMjZGDz2MzMjZzMjZmxAAAAAAAAwMjZMAYZYmZzMzMzMGYgZsxYZBw2gNMgZAYMzMMYmBzYMA",  # Sanlayn
    "CwPAkXBWxkyfx9CbGaHonEAhLBwMjZMzYY2mZmZMbmZMmxAAAAAAAAwMDjBALzYmZbmZMmBsZWMMwAzGDGLYAzAwYmZMDwMzMGD",  # Rider Vanguard
    "CwPAkXBWxkyfx9CbGaHonEAhLBwMjZMDDz2MzMjZzMzMmxAAAAAAAAwMDzMAYbGzMbzMjxMgNzihBGY2YwYBAzAwYmZMDwMzMGD",  # Rider ST
    "CwPAkXBWxkyfx9CbGaHonEAhLBYmZMPgxYY2GzMjZbmZMzMGAAAAAAAAmZMMAYZGzMbmZMzMgFzihBGY2YwYBDYGAGzMjZAmZGzYA",  # Rider AOE
]
DEADFOX_SPEC = 252

# Windwalker Monk (spec 269): all three on hero sub_tree 65.
KARATEPUMMEL_CODES = [
    "C0QAi6cZM+HWADeySjzG9Lwx8PzYMYMYbmZ2mxAAAAAAAAAAAALDDYGGGwMGmZmZYWGmhZZGAALmZbMMmZGAAbAwsMLmZmZBYYgZGAGLjBMgB",
    "C0QAi6cZM+HWADeySjzG9Lwx8PzYMYMYbmZ2mxAAAAAAAAAAAALDzAzwwAGmxMzMDzGmhZZGAALmZbMMmZGAAbAwsMLmZmZBYMDMzAwYZAMgB",
    "C0QAi6cZM+HWADeySjzG9Lwx8PzYw2MGsNzMbzAAAAAAAAAAAAsMMgZsNMgZMMzMzwsMMDzyMAA2Mz2YYmZmBAwGAMLziZmZWAwAzMAwyYADYA",
]
KARATEPUMMEL_SPEC = 269


@pytest.fixture(scope="module")
def dataset():
    return get_dataset()


@pytest.fixture(scope="module")
def deadfox_loadouts(dataset):
    return [decode_loadout(c, dataset=dataset) for c in DEADFOX_CODES]


@pytest.fixture(scope="module")
def karatepummel_loadouts(dataset):
    return [decode_loadout(c, dataset=dataset) for c in KARATEPUMMEL_CODES]


# ---------------------------------------------------------------------------
# Hero-tree clustering
# ---------------------------------------------------------------------------


def test_all_hero_trees_become_clusters(dataset, deadfox_loadouts):
    """Every hero tree the meta uses becomes a cluster — even a 1-of-4
    minority tree. A one-button optimum can live on a minority tree, so
    we never drop one by popularity."""
    result = cluster_loadouts(
        deadfox_loadouts, dataset, spec_id=DEADFOX_SPEC, threshold=0.30
    )
    assert result.hero_tree_distribution == {31: 1, 32: 3}
    kept = {c.sub_tree_id for c in result.clusters}
    assert kept == {31, 32}, "both Sanlayn and Rider must be clustered"
    assert result.dropped_hero_trees == {}


def test_max_per_hero_tree_caps_cluster_size(dataset, deadfox_loadouts):
    """``max_per_hero_tree`` keeps only the top-N loadouts per tree."""
    result = cluster_loadouts(
        deadfox_loadouts, dataset, spec_id=DEADFOX_SPEC,
        threshold=0.30, max_per_hero_tree=2,
    )
    for c in result.clusters:
        assert c.n_loadouts() <= 2


def test_single_hero_tree_cluster(dataset, karatepummel_loadouts):
    """All three Windwalker builds share one hero tree → one cluster."""
    result = cluster_loadouts(
        karatepummel_loadouts, dataset, spec_id=KARATEPUMMEL_SPEC, threshold=0.30
    )
    assert len(result.clusters) == 1
    assert result.clusters[0].sub_tree_id == 65
    assert result.clusters[0].n_loadouts() == 3


def test_wrong_spec_loadouts_filtered_out(dataset, deadfox_loadouts):
    """Asking for a spec none of the loadouts match yields no clusters."""
    result = cluster_loadouts(
        deadfox_loadouts, dataset, spec_id=269, threshold=0.30
    )
    assert result.n_loadouts_used == 0
    assert result.clusters == []


# ---------------------------------------------------------------------------
# Consensus / contested classification
# ---------------------------------------------------------------------------


def test_strict_threshold_collapses_to_consensus(dataset, deadfox_loadouts):
    """At threshold 0.95 (near-unanimous) the 3 Rider builds have no
    contested slot → exactly one variant comes out."""
    rider = deadfox_loadouts[1:]  # the three Rider builds
    result = cluster_loadouts(rider, dataset, spec_id=DEADFOX_SPEC, threshold=0.95)
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    contested = [n for n in cluster.nodes.values() if n.classification == "contested"]
    assert contested == [], "near-unanimous threshold should leave nothing contested"
    assert result.total_variants() == 1


def test_loose_threshold_finds_contested_slots(dataset, deadfox_loadouts):
    """At 0.30 the 3 Rider builds (varied for ST/AOE/Vanguard) disagree
    on several slots → more than one variant."""
    rider = deadfox_loadouts[1:]
    result = cluster_loadouts(rider, dataset, spec_id=DEADFOX_SPEC, threshold=0.30)
    assert result.total_variants() > 1


# ---------------------------------------------------------------------------
# Variant generation
# ---------------------------------------------------------------------------


def test_generated_variant_count_matches_cluster(dataset, karatepummel_loadouts):
    result = cluster_loadouts(
        karatepummel_loadouts, dataset, spec_id=KARATEPUMMEL_SPEC, threshold=0.95
    )
    variants = generate_build_variants(result, dataset, max_builds=4096)
    assert len(variants) == result.total_variants()


def test_greedy_fit_respects_max_builds(dataset, deadfox_loadouts):
    """A loose threshold on the divergent Rider trio would Cartesian-
    explode — generate_build_variants must greedily fit within the cap,
    never exceed it."""
    rider = deadfox_loadouts[1:]
    result = cluster_loadouts(rider, dataset, spec_id=DEADFOX_SPEC, threshold=0.30)
    if result.total_variants() <= 4:
        pytest.skip("corpus didn't diverge enough to stress the cap")
    for cap in (1, 4, 16, 64):
        variants = generate_build_variants(result, dataset, max_builds=cap)
        assert 0 < len(variants) <= cap, f"cap={cap}: got {len(variants)}"


def test_consensus_pick_present_in_every_variant(dataset, karatepummel_loadouts):
    """Every consensus (entry, rank) must appear in all generated variants."""
    result = cluster_loadouts(
        karatepummel_loadouts, dataset, spec_id=KARATEPUMMEL_SPEC, threshold=0.95
    )
    variants = generate_build_variants(result, dataset, max_builds=4096)
    consensus: dict[int, int] = {}
    for cluster in result.clusters:
        for node in cluster.nodes.values():
            if node.classification == "consensus" and node.consensus_pick:
                for entry_id, rank in node.consensus_pick:
                    consensus[entry_id] = rank
    assert consensus, "expected at least some consensus picks in the corpus"
    for variant in variants:
        for entry_id, rank in consensus.items():
            assert variant.get(entry_id) == rank


# ---------------------------------------------------------------------------
# Materialization round-trip
# ---------------------------------------------------------------------------


def test_materialize_produces_valid_loadout_codes(dataset, karatepummel_loadouts):
    """Each materialized build round-trips: the emitted Blizzard code
    decodes back to the same spec and yields a complete simc block."""
    result = cluster_loadouts(
        karatepummel_loadouts, dataset, spec_id=KARATEPUMMEL_SPEC, threshold=0.95
    )
    variants = generate_build_variants(result, dataset, max_builds=64)
    builds = materialize_variants(variants, dataset, spec_id=KARATEPUMMEL_SPEC)
    assert builds
    for b in builds:
        assert b.loadout_code, "empty loadout code"
        re_decoded = decode_loadout(b.loadout_code, dataset=dataset)
        assert re_decoded.spec_id == KARATEPUMMEL_SPEC
        for line in ("class_talents=", "spec_talents=", "hero_talents="):
            assert line in b.simc_block, f"{b.label}: missing {line}"


def test_materialize_labels_are_unique(dataset, karatepummel_loadouts):
    result = cluster_loadouts(
        karatepummel_loadouts, dataset, spec_id=KARATEPUMMEL_SPEC, threshold=0.95
    )
    variants = generate_build_variants(result, dataset, max_builds=64)
    builds = materialize_variants(variants, dataset, spec_id=KARATEPUMMEL_SPEC)
    labels = [b.label for b in builds]
    assert len(labels) == len(set(labels))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input_yields_empty_result(dataset):
    result = cluster_loadouts([], dataset, spec_id=DEADFOX_SPEC, threshold=0.30)
    assert result.clusters == []
    assert result.n_loadouts_used == 0


def test_invalid_threshold_rejected(dataset, deadfox_loadouts):
    for bad in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            cluster_loadouts(
                deadfox_loadouts, dataset, spec_id=DEADFOX_SPEC, threshold=bad
            )
