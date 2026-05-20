"""SQLAlchemy models — importing this module registers all tables on Base.metadata."""
from app.models.base import Base
from app.models.user import User, UserRole
from app.models.invite import Invite
from app.models.settings import AppSetting
from app.models.class_spec import GameClass, GameSpec, Role
from app.models.report import Report, ReportFight, ReportPlayer, ReportPlayerCast, ReportPlayerGear
from app.models.top_log import TopLog
from app.models.top_logs_seed_job import TopLogsSeedJob
from app.models.talent_ranking_snapshot import TalentRankingSnapshot
from app.models.analysis import Analysis, AnalysisStatus
from app.models.simulation import (
    Simulation,
    SimulationRun,
    SimulationRunStatus,
    SimulationStatus,
)
from app.models.user_ai_config import UserAiConfig
from app.models.wcl_connection import UserWclConnection
from app.models.wow_data import WowDataImport, WowImportStatus, WowLocalization

__all__ = [
    "Analysis",
    "AnalysisStatus",
    "AppSetting",
    "Base",
    "GameClass",
    "GameSpec",
    "Invite",
    "Report",
    "ReportFight",
    "ReportPlayer",
    "ReportPlayerCast",
    "ReportPlayerGear",
    "Role",
    "Simulation",
    "SimulationRun",
    "SimulationRunStatus",
    "SimulationStatus",
    "TalentRankingSnapshot",
    "TopLog",
    "TopLogsSeedJob",
    "User",
    "UserRole",
    "UserAiConfig",
    "UserWclConnection",
    "WowDataImport",
    "WowImportStatus",
    "WowLocalization",
]
