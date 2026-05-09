// Mirrors the FastAPI Pydantic schemas (backend/app/schemas/*).
export type Role = "user" | "admin";
export type GameRole = "dps" | "healer" | "tank";
export type Severity = "critical" | "high" | "medium" | "low" | "info";

export interface UserOut {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  is_active: boolean;
  locale: string;
  created_at: string;
  last_login_at: string | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface PublicConfig {
  app_name: string;
  supported_locales: string[];
  default_locale: string;
  allow_registration: boolean;
  ai_enabled: boolean;
  captcha_enabled: boolean;
  /** Cloudflare site key. Empty string ⇒ widget disabled. */
  turnstile_site_key: string;
}

export type AiProviderType = "anthropic" | "openai" | "openai_compatible";
export type ReasoningEffort = "minimal" | "low" | "medium" | "high";

export interface UserAiConfig {
  provider_type: AiProviderType;
  base_url: string | null;
  model: string;
  label: string;
  api_key_masked: string;
  reasoning_effort: ReasoningEffort | null;
}

export interface UserAiConfigInput {
  provider_type: AiProviderType;
  base_url?: string;
  model: string;
  api_key: string;
  label?: string;
  reasoning_effort?: ReasoningEffort | null;
}

export interface UserAiConfigTestResult {
  ok: boolean;
  detail: string;
  latency_ms: number | null;
}

export interface ContainerStatus {
  name: string;
  service: string;
  image: string;
  status: string;
  health: string | null;
  started_at: string | null;
  finished_at: string | null;
  is_local_ai: boolean;
}

export interface SystemStatus {
  enabled: boolean;
  project: string;
  containers: ContainerStatus[];
}

export interface LocalAiModelConfig {
  hf_repo: string;
  hf_file: string;
  alias: string;
  ctx_size: number;
  enable_thinking: boolean;
}

export interface LocalAiDownloadProgress {
  filename: string;
  bytes_done: number;
  bytes_total: number | null;
  percent: number | null;
  started_at: number;
  finished_at: number | null;
  error: string | null;
}

export interface LocalAiStatus {
  reachable: boolean;
  config: LocalAiModelConfig | null;
  desired_running: boolean;
  child_running: boolean;
  child_healthy: boolean;
  current_model_filename: string | null;
  download: LocalAiDownloadProgress | null;
  last_error: string | null;
}

export interface LocalAiModelFile {
  filename: string;
  size_bytes: number;
  is_loaded: boolean;
}

export interface GameSpec {
  slug: string;
  name_en: string;
  name_de: string;
  role: GameRole;
  wcl_spec_id: number;
}

export interface GameClass {
  slug: string;
  name_en: string;
  name_de: string;
  color_hex: string;
  specs: GameSpec[];
}

export interface ReportPlayerCast {
  ability_id: number;
  ability_name: string;
  casts: number;
  hits: number;
  total: number;
  icon: string | null;
}

export interface ReportPlayerGear {
  slot: number;
  item_id: number;
  item_level: number | null;
  item_quality: number | null;
  name: string;
  icon: string | null;
  enchant_id: number | null;
  gem_ids: number[];
  bonus_ids: number[];
}

export interface ReportPlayer {
  id: string;
  name: string;
  server: string;
  class_slug: string;
  spec_slug: string;
  role: GameRole;
  item_level: number | null;
  dps: number | null;
  hps: number | null;
  damage_done: number;
  healing_done: number;
  deaths: number;
  talents_loadout: string | null;
  casts: ReportPlayerCast[];
  gear: ReportPlayerGear[];
  // WCL parse percentiles populated at import time. Both 0-100 (higher=
  // better) — ``parse_percent`` is the all-logs value, ``ilvl_percent`` the
  // gear-bracketed one. Either can be null when WCL has no ranking data
  // (very fresh boss / non-public log / private realm).
  parse_percent: number | null;
  ilvl_percent: number | null;
}

export interface ReportFight {
  id: string;
  fight_id: number;
  encounter_id: number | null;
  name: string;
  name_localized: string | null;
  difficulty: number | null;
  keystone_level: number | null;
  is_kill: boolean;
  boss_percentage: number | null;
  duration_ms: number;
  start_time: string;
  players: ReportPlayer[];
}

export type ReportImportStatus = "importing" | "ready" | "failed";

export interface Report {
  id: string;
  wcl_code: string;
  title: string;
  zone_id: number | null;
  zone_name: string;
  region: string;
  game_version: string;
  start_time: string | null;
  end_time: string | null;
  import_status: ReportImportStatus;
  import_error: string | null;
  fights: ReportFight[];
}

export interface ReportSummary {
  id: string;
  wcl_code: string;
  title: string;
  zone_name: string;
  start_time: string | null;
  end_time: string | null;
  import_status: ReportImportStatus;
}

export interface PaginatedReports {
  items: ReportSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface AnalysisFinding {
  severity: Severity;
  title: string;
  detail: string;
  estimated_loss_pct: number | null;
  category:
    | "rotation"
    | "cooldowns"
    | "stats"
    | "talents"
    | "gear"
    | "trinkets"
    | "consumables"
    | "mechanics"
    | "other";
  related_spell_ids: number[];
  related_item_ids: number[];
}

export interface AnalysisStructured {
  headline: string;
  overall_score: number;
  role_focus: GameRole;
  strengths: string[];
  findings: AnalysisFinding[];
  rotation_summary: string;
  cooldown_usage_summary: string;
  stat_recommendations: string;
  talent_recommendations: string;
  gear_and_trinket_notes: string;
  comparison_to_top_logs: string;
  // Underscore-prefixed fields are NOT produced by the AI — they are
  // server-side metadata stitched into the structured output before save.
  _localized_names?: Record<string, string>;
  _parse_metrics?: {
    parse_percent: number | null;
    ilvl_percent: number | null;
    rank: number | null;
    out_of: number | null;
  };
}

export interface Analysis {
  id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  locale: string;
  provider: string;
  model: string;
  summary: string;
  structured: AnalysisStructured | Record<string, never>;
  error: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  created_at: string;
  updated_at: string;
}

export interface PaginatedAnalyses {
  items: AnalysisListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface AnalysisListItem {
  id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  locale: string;
  provider: string;
  model: string;
  created_at: string;
  headline: string;
  overall_score: number | null;
  role_focus: GameRole | null;
  report_id: string;
  report_code: string;
  fight_id: string;
  fight_name: string;
  fight_name_localized: string | null;
  encounter_id: number | null;
  player_id: string;
  player_name: string;
  player_class: string;
  player_spec: string;
}

export interface TopLog {
  id: string;
  spec_slug: string;
  encounter_id: number;
  encounter_name: string;
  encounter_name_localized: string | null;
  difficulty: number | null;
  metric: "dps" | "hps";
  rank: number;
  amount: number;
  item_level: number | null;
  duration_ms: number | null;
  character_name: string;
  server: string;
  region: string;
  wcl_report_code: string;
  wcl_fight_id: number;
  recorded_at: string;
}

export interface ApiError {
  error: { code: string; message: string; details?: unknown };
}

export interface Invite {
  id: string;
  email: string;
  expires_at: string;
  accepted_at: string | null;
  revoked: boolean;
  created_at: string;
}

export interface AdminSettings {
  allow_registration: boolean;
  ai_provider: string;
  ai_model: string;
}

export interface WclConnectionStatus {
  connected: boolean;
  wcl_user_id?: number | null;
  wcl_user_name?: string;
  expires_at?: string | null;
  scope?: string;
}

export interface WclAuthorizationStart {
  authorization_url: string;
}

export interface WowDataImport {
  id: string;
  build: string;
  status: "in_progress" | "success" | "failed";
  started_at: string;
  finished_at: string | null;
  rows_imported: number;
  source: string;
  notes: string;
  phase: string;
}

export interface WowDataStatus {
  last_import: WowDataImport | null;
  counts: Record<string, Record<string, number>>;
  latest_known_build: string | null;
}

export interface TopLogsEncounterRow {
  encounter_id: number;
  encounter_name: string;
  encounter_name_localized: string | null;
  metrics: string[];
  rows: number;
  latest_recorded_at: string | null;
}

export interface TopLogsSeedJob {
  id: string;
  encounter_id: number;
  encounter_name: string;
  is_raid: boolean;
  metric_filter: "dps" | "hps" | null;
  total_specs: number;
  completed_specs: number;
  current_spec_slug: string | null;
  status: "queued" | "running" | "succeeded" | "failed";
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  created_at: string;
}

export interface TopLogsCurrentTierResponse {
  queued: number;
  skipped_already_running: number;
  encounters: Array<{
    encounter_id: number;
    encounter_name: string;
    zone_id: number;
    zone_name: string;
    expansion_id: number;
    expansion_name: string;
  }>;
}
