"""Port of simc's ``parse_traits_hash`` to Python.

Reads Blizzards talent-loadout codes (the same base64 strings the
in-game ``/simc`` command and the Battle.net Profile API produce) and
returns the list of ``(entry_id, rank)`` pairs simc consumes via its
``class_talents=`` / ``spec_talents=`` / ``hero_talents=`` options.

Reference: ``engine/player/player.cpp`` in simulationcraft/simc
(``parse_traits_hash`` at the time of writing the canonical source).

The bit-stream is laid out as follows (little-endian within each base64
sextet, ``LOADOUT_SERIALIZATION_VERSION = 2``):

* 8 bits   serialization version (must be 2)
* 16 bits  specialization id
* 128 bits tree hash (Blizzards C_Traits.GetTreeHash() — simc zeros it,
            so do we; serves as a tamper check only)
* For each node in the spec's class+spec+hero+selection tree (sorted by
  ``node_id`` ascending):
    * 1 bit  is_node_selected
    * if selected:
        * 1 bit  is_purchased (vs. baseline / granted at rank 1)
        * if purchased:
            * 1 bit  is_partially_ranked
            * if partial: 6 bits = partial rank
            * 1 bit  is_choice_node
            * if choice: 2 bits = choice index

Missing-anchor case for stale saved loadouts:

Saved loadouts in WoW persist the *user-picked* talents but **not** the
hero-tree selection anchor (an entry on the ``TREE_SELECTION`` node).
Activating the loadout in-game silently adds the anchor; the saved-state
exported via ``/simc`` and via Battle.nets API does not have it. Simc's
spell-init then segfaults because the hero tree never got assigned.

We re-add the anchor here: if the decoded selections include any
hero-tree entries (``tree_index == TREE_HERO``) and no
``TREE_SELECTION`` entry pointing at that ``sub_tree_id``, we synthesise
the matching selection entry from the dataset. This is exactly what the
game does on activation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.talents.trait_data import (
    NODE_SELECTION,
    NODE_TIERED,
    TREE_HERO,
    TREE_SELECTION,
    TraitDataset,
    TraitEntry,
)

logger = logging.getLogger(__name__)


# Format constants — keep in lock-step with simc's parse_traits_hash.
_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CHAR_TO_VAL = {c: i for i, c in enumerate(_BASE64_ALPHABET)}
_VERSION_BITS = 8
_SPEC_BITS = 16
_TREE_BITS = 128
_RANK_BITS = 6
_CHOICE_BITS = 2
_BYTE_SIZE = 6  # base64 sextet
_LOADOUT_SERIALIZATION_VERSION = 2


class TalentDecodeError(ValueError):
    """Raised for unparseable / mismatched loadout codes."""


@dataclass(frozen=True)
class SelectedEntry:
    """One selected talent in a decoded loadout."""

    entry_id: int
    rank: int
    tree_index: int      # CLASS / SPECIALIZATION / HERO / SELECTION
    node_id: int
    sub_tree_id: int
    name: str
    is_granted: bool = False
    """``True`` for entries that the game grants for free (rank 1) — included
    so callers can decide whether to emit them in the simc input; in
    practice simc adds them automatically and our generator skips them."""


@dataclass
class DecodedLoadout:
    """The full result of decoding a single loadout code."""

    version: int
    spec_id: int
    selections: list[SelectedEntry] = field(default_factory=list)
    anchor_added: bool = False
    """``True`` when we synthesised the missing hero-tree selection anchor."""


class _BitReader:
    """LSB-first bit reader over a base64 string. Mirrors simcs
    ``get_bit`` closure in parse_traits_hash, including its quirk of
    capping the per-call shift at 31 bits."""

    __slots__ = ("_chars", "_head", "_byte")

    def __init__(self, code: str) -> None:
        # Strip whitespace; reject foreign characters early.
        clean = "".join(c for c in code if not c.isspace())
        for c in clean:
            if c not in _CHAR_TO_VAL:
                raise TalentDecodeError(f"invalid character {c!r} in loadout code")
        self._chars = clean
        self._head = 0
        self._byte = _CHAR_TO_VAL[clean[0]] if clean else 0

    def read(self, bits: int) -> int:
        val = 0
        for i in range(bits):
            bit_in_byte = self._head % _BYTE_SIZE
            self._head += 1
            val |= ((self._byte >> bit_in_byte) & 1) << min(i, 31)
            if bit_in_byte == _BYTE_SIZE - 1:
                idx = self._head // _BYTE_SIZE
                if idx >= len(self._chars):
                    self._byte = 0
                else:
                    self._byte = _CHAR_TO_VAL[self._chars[idx]]
        return val


# ---------------------------------------------------------------------------
# Tree-node iteration (mirrors simcs generate_tree_nodes)
# ---------------------------------------------------------------------------


def _spec_class_id(spec_id: int, dataset: TraitDataset) -> int | None:
    """Best-effort ``spec_id -> class_id`` lookup using the dataset.

    Walks every trait until it finds one whose ``id_spec`` list contains
    ``spec_id``. That entrys ``class_id`` is the answer. trait_data.inc
    has one such row per *class* talent so this lookup is O(N) per call
    in the worst case, but the dataset is fixed at ~3500 rows.
    """
    for e in dataset.entries:
        if spec_id in e.id_spec:
            return e.class_id
    return None


def _ordered_class_nodes(class_id: int, dataset: TraitDataset) -> list[int]:
    """Node IDs for one class, sorted ascending — the exact order simcs
    ``parse_traits_hash`` reads bits in. Mirrors ``generate_tree_nodes``
    which iterates all trees (CLASS / SPEC / HERO / SELECTION) for the
    class and collects them into a ``std::map`` keyed on node_id (which
    sorts ascending automatically in C++)."""
    nodes: set[int] = set()
    for e in dataset.entries_for_class(class_id):
        # Skip non-loadout trees (EXPANSION etc. — tree_index >= MAX = 5)
        if e.tree_index < 1 or e.tree_index > TREE_SELECTION:
            continue
        nodes.add(e.node_id)
    return sorted(nodes)


# ---------------------------------------------------------------------------
# Public decode entry point
# ---------------------------------------------------------------------------


def decode_loadout(
    code: str,
    *,
    dataset: TraitDataset,
    auto_add_anchor: bool = True,
) -> DecodedLoadout:
    """Decode a single loadout code against the given trait dataset.

    Parameters
    ----------
    code
        Base64 loadout code, as emitted by the ``/simc`` command or
        returned by Blizzards Profile API.
    dataset
        Parsed trait dataset (typically from :func:`get_dataset()`).
    auto_add_anchor
        When ``True`` (default), missing hero-tree selection anchors are
        synthesised so simc accepts saved-loadout exports without
        segfaulting.

    Returns
    -------
    DecodedLoadout
        ``selections`` is in tree-node-id order, matching simcs internal
        layout.
    """
    if not code:
        raise TalentDecodeError("empty loadout code")
    reader = _BitReader(code)

    version = reader.read(_VERSION_BITS)
    if version != _LOADOUT_SERIALIZATION_VERSION:
        raise TalentDecodeError(
            f"unsupported serialization version {version} "
            f"(expected {_LOADOUT_SERIALIZATION_VERSION})"
        )

    spec_id = reader.read(_SPEC_BITS)
    class_id = _spec_class_id(spec_id, dataset)
    if class_id is None:
        raise TalentDecodeError(
            f"spec id {spec_id} not present in trait dataset (build mismatch?)"
        )

    # Tree hash is 128 bits, simc fills it with zeros on its own exports.
    # We read past it without validation — bad hashes show up as wrong
    # node selections downstream anyway.
    reader.read(_TREE_BITS)

    selections: list[SelectedEntry] = []
    node_ids = _ordered_class_nodes(class_id, dataset)

    for node_id in node_ids:
        is_selected = reader.read(1)
        if not is_selected:
            continue
        is_purchased = reader.read(1)
        # Layout per simcs encoder (generate_traits_hash) — when not
        # purchased we stop reading for this node, the rank defaults to
        # 1 (granted talents).
        if not is_purchased:
            entry = dataset.entry_at(node_id, choice_index=0)
            if entry is not None:
                selections.append(
                    SelectedEntry(
                        entry_id=entry.entry_id,
                        rank=1,
                        tree_index=entry.tree_index,
                        node_id=entry.node_id,
                        sub_tree_id=entry.sub_tree_id,
                        name=entry.name,
                        is_granted=True,
                    )
                )
            continue
        # Purchased node — read partial-rank + choice flags.
        is_partial = reader.read(1)
        rank = 0
        if is_partial:
            rank = reader.read(_RANK_BITS)
        is_choice = reader.read(1)
        choice_idx = reader.read(_CHOICE_BITS) if is_choice else 0

        entries_here = dataset.entries_at_node(node_id)
        if not entries_here:
            # Loadout references a node our dataset doesnt know — most
            # likely a build mismatch. Skip silently rather than abort
            # so a fresh-but-not-yet-imported build still produces
            # partial output for diagnostics.
            logger.debug("decode_loadout: unknown node %d, skipping", node_id)
            continue
        if is_choice and choice_idx < len(entries_here):
            entry = entries_here[choice_idx]
        else:
            entry = entries_here[0]

        if entry.node_type == NODE_TIERED:
            # TIERED nodes split a single rank value across multiple
            # entries on the node, allocating max_ranks to each in
            # source order until the rank budget runs out. Mirrors
            # simcs decoder; without this the verbose-form simc input
            # caps every TIERED talent at the first entrys max_ranks.
            total = rank if is_partial else sum(e.max_ranks for e in entries_here)
            for tiered_entry in entries_here:
                if total <= 0:
                    break
                allocated = min(total, tiered_entry.max_ranks)
                if allocated > 0:
                    selections.append(
                        SelectedEntry(
                            entry_id=tiered_entry.entry_id,
                            rank=allocated,
                            tree_index=tiered_entry.tree_index,
                            node_id=tiered_entry.node_id,
                            sub_tree_id=tiered_entry.sub_tree_id,
                            name=tiered_entry.name,
                        )
                    )
                total -= allocated
            continue

        # Effective rank: max_ranks for fully-allocated non-tiered nodes
        # (is_partial == 0), explicit rank otherwise.
        effective_rank = rank if is_partial else entry.max_ranks

        selections.append(
            SelectedEntry(
                entry_id=entry.entry_id,
                rank=effective_rank,
                tree_index=entry.tree_index,
                node_id=entry.node_id,
                sub_tree_id=entry.sub_tree_id,
                name=entry.name,
            )
        )

    result = DecodedLoadout(version=version, spec_id=spec_id, selections=selections)

    if auto_add_anchor:
        result.anchor_added = _add_missing_hero_anchor(result, dataset)

    return result


def decoded_from_talent_tree(
    talent_tree: list[dict],
    *,
    spec_id: int,
    dataset: TraitDataset,
) -> DecodedLoadout:
    """Build a :class:`DecodedLoadout` from WCL's structured talent shapes.

    WCL serves talents in three forms — :func:`decode_loadout` handles
    the base64 ``talentLoadout`` code; this function handles the two
    structured (list-of-dicts) shapes, which differ only in key names:

    * ``combatantInfo.talentTree`` → ``{"id", "rank", "nodeID"}``
    * ``characterRankings`` entries → ``{"talentID", "points"}``

    In both, the entry-id field (``id`` / ``talentID``) is the
    TraitNodeEntry.ID; the rest of the metadata (tree, node, sub-tree,
    name) is resolved from the trait dataset. ``nodeID``, when present,
    is ignored — the dataset is authoritative. ``spec_id`` must be
    supplied by the caller — the structured forms don't carry it.

    Unknown entry-ids (dataset/build skew) are skipped with an INFO log
    rather than raising, so one stale row doesn't sink a whole cluster.
    """
    selections: list[SelectedEntry] = []
    for item in talent_tree:
        if not isinstance(item, dict):
            continue
        # Accept both key conventions: id/rank (combatantInfo) and
        # talentID/points (characterRankings).
        raw_id = item.get("id", item.get("talentID"))
        raw_rank = item.get("rank", item.get("points", 1))
        try:
            entry_id = int(raw_id)
            rank = int(raw_rank)
        except (TypeError, ValueError):
            continue
        if rank <= 0:
            continue
        meta = dataset._by_entry_id.get(entry_id)
        if meta is None:
            logger.info(
                "talentTree: entry_id %s not in dataset (build skew?) — skipping",
                entry_id,
            )
            continue
        selections.append(
            SelectedEntry(
                entry_id=entry_id,
                rank=rank,
                tree_index=meta.tree_index,
                node_id=meta.node_id,
                sub_tree_id=meta.sub_tree_id,
                name=meta.name,
                is_granted=False,
            )
        )
    return DecodedLoadout(
        version=_LOADOUT_SERIALIZATION_VERSION,
        spec_id=spec_id,
        selections=selections,
    )


def _full_rank_for_node(
    node_id: int, entries: list[TraitEntry], chosen: TraitEntry
) -> int:
    """Max-rank fill for ``is_partial == 0`` nodes. Tiered nodes sum the
    ranks of every entry on the node; everything else uses the chosen
    entrys own ``max_ranks``."""
    if chosen.node_type == NODE_TIERED:
        return sum(e.max_ranks for e in entries)
    return chosen.max_ranks


def _add_missing_hero_anchor(
    decoded: DecodedLoadout, dataset: TraitDataset
) -> bool:
    """Insert the gateway HERO talent for every hero sub-tree that the
    decoded loadout has entries for but doesnt include the gateway of.

    Saved-loadout exports persist user-picked hero talents but omit the
    tree's gateway node (e.g. "Rider's Champion" for the Rider tree on
    Unholy DK). The game auto-adds it on activation; simc segfaults
    without it. This function detects the missing gateway by sub_tree
    and synthesises a HERO entry for it.

    Returns ``True`` when at least one gateway was added. Idempotent:
    if the loadout already includes the gateway entry, nothing changes.
    """
    hero_entries_by_sub_tree: dict[int, set[int]] = {}
    for s in decoded.selections:
        if s.tree_index == TREE_HERO and s.sub_tree_id > 0:
            hero_entries_by_sub_tree.setdefault(s.sub_tree_id, set()).add(s.entry_id)
    if not hero_entries_by_sub_tree:
        return False

    added = False
    for sub_tree_id, present_entries in hero_entries_by_sub_tree.items():
        gateway = dataset.find_anchor_for_hero_tree(sub_tree_id, spec_id=decoded.spec_id)
        if gateway is None:
            logger.warning(
                "decode_loadout: no gateway entry found for hero tree %d", sub_tree_id
            )
            continue
        if gateway.entry_id in present_entries:
            # Already in the loadout — no anchor needed (active state).
            continue
        decoded.selections.append(
            SelectedEntry(
                entry_id=gateway.entry_id,
                rank=1,
                tree_index=TREE_HERO,
                node_id=gateway.node_id,
                sub_tree_id=gateway.sub_tree_id,
                name=gateway.name or f"hero-tree-{sub_tree_id}-gateway",
            )
        )
        added = True
    return added
