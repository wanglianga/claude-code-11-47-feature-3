export interface ScenarioCreatePayload {
  name: string;
  population_as_of: string;
  departure_time: string;
  max_trip_minutes: number;
  road_cutoff: string;
  boundary_id?: string | null;
  viewer_id: string;
  note?: string;
}

export interface ScenarioResult {
  scenario: Record<string, unknown>;
  task_id: string;
  status: string;
  cache_hit: boolean;
  evidence_cutoff_at: string;
  message: string;
}

export interface Summary {
  total_population: number;
  located_population: number;
  unknown_population: number;
  covered_population: number;
  uncovered_population: number;
  covered_grid_count: number;
  unknown_grid_count: number;
  computed_at?: string;
}

export type ShareStatus = "covered" | "uncovered" | "unknown";

export interface ShareRow {
  task_id: string;
  grid_id: string;
  grid_name?: string;
  geom_x?: number;
  geom_y?: number;
  boundary_id?: string | null;
  connector_node: string;
  share_index: number;
  share_population: number;
  reachable: boolean;
  shelter_id: string | null;
  departure_time: string | null;
  arrival_time: string | null;
  total_wait_sec: number;
  witness_edge_path: string[] | null;
  status: ShareStatus;
  reason: string | null;
  evidence_cutoff_at: string;
}

export interface ArrivalRow {
  task_id: string;
  node_id: string;
  arrival_time: string;
  wait_cost_sec: number;
  witness_node_path: string[];
  witness_edge_path: string[];
  geom_x: number;
  geom_y: number;
  allow_wait: boolean;
}

export interface SourceDoc {
  doc_id: number;
  dataset_type: string;
  title: string;
  source_org: string | null;
  unit_note: string | null;
  source_occurred_at: string | null;
  received_at: string;
  cutoff_at: string;
  revision_no: number;
  supersedes_doc_id: number | null;
  raw_text: string | null;
}

export interface RoadEventRow {
  event_id: string;
  event_type: string;
  edge_id: string;
  effective_from: string;
  effective_to: string | null;
  speed_limit_kmh: number | null;
  revision_no: number;
  status: string;
  source_title?: string;
  source_raw?: string;
}

export interface EdgeRevision {
  edge_id: string;
  revision_no: number;
  from_node: string;
  to_node: string;
  length_m: number;
  base_speed_kmh: number;
  valid_from: string;
  valid_to: string | null;
}

export interface NodeRow {
  node_id: string;
  name: string | null;
  geom_x: number;
  geom_y: number;
  allow_wait: boolean;
}

export interface ShelterRow {
  shelter_id: string;
  name: string;
  node_id: string;
  geom_x: number;
  geom_y: number;
  active: boolean;
}

export interface OpenWindow {
  shelter_id: string;
  open_from: string;
  open_to: string | null;
}

export interface GridRow {
  grid_id: string;
  name: string | null;
  geom_x: number;
  geom_y: number;
  boundary_id: string | null;
  population: number;
  pop_revision: number;
  pop_as_of: string;
}

export interface ConnectorRow {
  grid_id: string;
  node_id: string;
  allocation_ratio: number;
  revision_no: number;
  source_title?: string;
}

export interface CacheRow {
  signature: string;
  task_id: string;
  graph_version_id: number;
  departure_time: string;
  max_trip_minutes: number;
  evidence_cutoff_at: string;
  stale: boolean;
  computed_at: string;
  status?: string;
  kind?: string;
  task_stale?: boolean;
  scenario_name?: string;
}

export interface TaskRow {
  task_id: string;
  scenario_id: string;
  signature: string;
  graph_version_id: number;
  kind: string;
  status: string;
  ref_count: number;
  stale: boolean;
  evidence_cutoff_at: string;
  error: string | null;
  created_at: string;
  scenario_name?: string;
}

export interface ScenarioListRow {
  scenario_id: string;
  name: string;
  population_as_of: string;
  departure_time: string;
  max_trip_minutes: number;
  road_cutoff: string;
  boundary_id: string | null;
  signature: string;
  evidence_cutoff_at: string;
  task_id: string | null;
  task_status: string | null;
  task_stale: boolean | null;
  task_kind: string | null;
  ref_count: number | null;
}

export interface ComparisonGrid {
  grid_id: string;
  boundary_id: string | null;
  population_a: number;
  population_b: number;
  covered_pop_a: number;
  covered_pop_b: number;
  unknown_pop_a: number;
  unknown_pop_b: number;
  shelters_a: string[];
  shelters_b: string[];
  reasons_a: string[];
  reasons_b: string[];
  classification: string;
}

export interface Comparison {
  scenario_a: Record<string, unknown>;
  scenario_b: Record<string, unknown>;
  common_denominator: {
    grid_count: number;
    population_a: number;
    population_b: number;
    covered_a: number;
    covered_b: number;
    unknown_a: number;
    unknown_b: number;
    coverage_rate_a: number;
    coverage_rate_b: number;
    rate_delta: number;
  };
  attribution: {
    road_or_open_window_effect_population: number;
    newly_covered_grids: ComparisonGrid[];
    lost_coverage_grids: ComparisonGrid[];
    population_revision_effect: Array<{
      grid_id: string;
      population_delta: number;
      covered_a: number;
      covered_b: number;
    }>;
    boundary_added_grids: string[];
    boundary_removed_grids: string[];
    unknown_changed_grids: ComparisonGrid[];
    stable_covered_count: number;
    stable_uncovered_count: number;
  };
  per_grid: ComparisonGrid[];
  warnings: string[];
  basis_checks: Record<string, boolean>;
}

// ------------------------------------------------------------- 证据融合 --
export type EvidenceSourceType = "water_sensor" | "inspection_photo" | "crowd_report";
export type EvidenceLevel =
  | "confirmed_closure"
  | "likely_closure"
  | "conflict"
  | "unknown";

export interface EvidenceRow {
  evidence_id: string;
  revision_no: number;
  source_type: EvidenceSourceType;
  source_ref: string;
  source_org: string | null;
  geom_x: number;
  geom_y: number;
  location_error_m: number;
  location_text: string | null;
  observed_at: string;
  uploaded_at: string;
  bearing_deg: number | null;
  threshold_value: number | null;
  observed_value: number | null;
  observed_recovery_at: string | null;
  offline: boolean;
  raw_text: string | null;
  evidence_fingerprint: string;
  status: "active" | "withdrawn";
  change_note: string | null;
}

export interface ProjectionRow {
  projection_id: number;
  evidence_id: string;
  evidence_revision: number;
  edge_id: string;
  direction: "forward" | "reverse" | "both";
  stance: "closure" | "open" | "none";
  valid_from: string;
  valid_to: string | null;
  perpendicular_m: number;
  along_m: number;
  spatial_weight: number;
  direction_match: string;
  direction_weight: number;
  time_basis: string;
  time_weight: number;
  upload_delay_sec: number;
  accepted: boolean;
  reject_reason: string | null;
  rule_trace: Record<string, unknown>;
  source_type?: EvidenceSourceType;
  source_ref?: string;
}

export interface ConclusionRow {
  conclusion_id: number;
  run_id: number;
  strategy: "official_only" | "multi_source";
  edge_id: string;
  direction: string;
  valid_from: string;
  valid_to: string | null;
  level: EvidenceLevel;
  weight_closure: number;
  weight_open: number;
  uncertainty: number;
  has_conflict: boolean;
  evidence_count: number;
  evidence_refs: Array<Record<string, unknown>>;
  rule_trace: Record<string, unknown>;
  cutoff_at: string;
}

export interface FusionRunRow {
  run_id: number;
  strategy: string;
  cutoff_at: string;
  input_fingerprint: string;
  conclusion_fingerprint: string;
  rule_version: string;
  status: string;
  conclusion_count: number;
  created_at: string;
}

export interface FusionRunResult {
  run_id: number;
  strategy: string;
  cutoff_at: string;
  input_fingerprint: string;
  conclusion_fingerprint: string;
  conclusion_count: number;
  level_breakdown: Record<EvidenceLevel, number>;
  derived_closures: string[];
  invalidation: {
    changed_edges?: string[];
    affected_task_ids?: string[];
    affected_grid_ids?: string[];
    staled_total: number;
  };
  rule_version: string;
}

export interface ImpactLogRow {
  impact_id: number;
  evidence_id: string;
  action: string;
  changed_at: string;
  affected_edge_ids: string[];
  affected_task_ids: string[];
  affected_grid_ids: string[];
  detail_json: Record<string, unknown>;
}

export interface DerivedClosureRow {
  derived_event_id: string;
  conclusion_id: number;
  run_id: number;
  edge_id: string;
  direction: string;
  effective_from: string;
  effective_to: string | null;
  cutoff_at: string;
  status: "active" | "retracted";
}

export interface EvidenceDrilldown {
  grid_id: string;
  found: boolean;
  cutoff_at: string;
  witness_edges: string[];
  incident_edges: string[];
  share_reasons: string[];
  conclusions: ConclusionRow[];
  projections: ProjectionRow[];
  manifest_snapshot: Record<string, unknown> | null;
  impact_logs: ImpactLogRow[];
}
