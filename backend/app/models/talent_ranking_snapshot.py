"""Cached WCL ``characterRankings`` talent data for the Talent-Finder.

One row per (spec, encounter). The Talent-Finder needs the talent
loadouts of the top ~100 logged players to build its variant search;
fetching that from WCL on every run would hammer their API. We snapshot
it here instead and refetch only when the snapshot ages past the TTL
(see ``talent_finder_service``), the same lazy-refresh idea the rest of
the WCL caching uses.

Deliberately talents-only: gear / casts / buffs are NOT stored — the
AI analyzer's ``TopLog.detail_payload`` already covers heavy detail and
the Talent-Finder doesn't need it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models._types import JSONType
from app.models.base import Base, TimestampMixin


class TalentRankingSnapshot(Base, TimestampMixin):
    __tablename__ = "talent_ranking_snapshots"

    spec_slug: Mapped[str] = mapped_column(String(48), primary_key=True)
    encounter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # When the WCL fetch behind this snapshot ran. The Talent-Finder
    # treats a snapshot older than its TTL as stale and refetches.
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Rank-ordered list (best DPS first) of
    # ``{"rank": int, "amount": float, "talents": [{talentID, points}, ...]}``.
    # ``talents`` is WCL's structured characterRankings shape, fed
    # straight into ``decoded_from_talent_tree``.
    rankings: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
