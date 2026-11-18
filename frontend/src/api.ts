import type {
  ArrivalRow,
  CacheRow,
  Comparison,
  ConnectorRow,
  EdgeRevision,
  GridRow,
  NodeRow,
  OpenWindow,
  RoadEventRow,
  ScenarioCreatePayload,
  ScenarioListRow,
  ScenarioResult,
  ShareRow,
  ShelterRow,
  SourceDoc,
  Summary,
  TaskRow,
} from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  createScenario: (payload: ScenarioCreatePayload) =>
    request<ScenarioResult>("/scenarios", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listScenarios: () =>
    request<{ scenarios: ScenarioListRow[] }>("/scenarios"),

  taskSummary: (taskId: string) =>
    request<{ task: TaskRow; summary: Summary | null }>(
      `/tasks/${taskId}/summary`
    ),

  taskGrids: (taskId: string) =>
    request<{ shares: ShareRow[] }>(`/tasks/${taskId}/grids`),

  taskArrivals: (taskId: string) =>
    request<{ arrivals: ArrivalRow[] }>(`/tasks/${taskId}/arrivals`),

  taskWitness: (taskId: string, gridId: string) =>
    request<{
      grid_id: string;
      shares: ShareRow[];
      connector_arrivals: Array<ArrivalRow | null>;
      task_edge_dependencies: Array<{ task_id: string; edge_id: string }>;
    }>(`/tasks/${taskId}/witness?grid_id=${encodeURIComponent(gridId)}`),

  releaseViewer: (taskId: string, viewerId: string) =>
    request<{
      task_id: string;
      viewer_id: string;
      remaining_viewers: number;
      cancelled: boolean;
      note: string;
    }>(`/tasks/${taskId}/release`, {
      method: "POST",
      body: JSON.stringify({ viewer_id: viewerId }),
    }),

  listTasks: () =>
    request<{ tasks: TaskRow[]; cache_index: CacheRow[] }>("/tasks"),

  documents: () =>
    request<{ documents: SourceDoc[] }>("/sources/documents"),

  boundaries: () =>
    request<{
      boundaries: Array<{
        boundary_id: string;
        name: string;
        geometry: number[][];
      }>;
    }>("/sources/boundaries"),

  nodes: () => request<{ nodes: NodeRow[] }>("/sources/nodes"),

  edges: () => request<{ edge_revisions: EdgeRevision[] }>("/sources/edges"),

  events: (includeAudit = false) =>
    request<{
      events: RoadEventRow[];
      audits: Array<Record<string, unknown>>;
    }>(`/sources/events?include_audit=${includeAudit}`),

  shelters: () =>
    request<{ shelters: ShelterRow[]; open_windows: OpenWindow[] }>(
      "/sources/shelters"
    ),

  grids: (asOf: string) =>
    request<{ grids: GridRow[]; connectors: ConnectorRow[] }>(
      `/sources/grids?as_of=${encodeURIComponent(asOf)}`
    ),

  compare: (taskA: string, taskB: string) =>
    request<Comparison>("/comparisons", {
      method: "POST",
      body: JSON.stringify({ task_a_id: taskA, task_b_id: taskB }),
    }),

  publish: (taskA: string, taskB: string, title: string) =>
    request<{
      publication: { publication_id: string; [k: string]: unknown };
      comparison: Comparison;
      input_manifest: Record<string, unknown>;
    }>("/publications", {
      method: "POST",
      body: JSON.stringify({
        task_a_id: taskA,
        task_b_id: taskB,
        title,
        created_by: "analyst-ui",
      }),
    }),

  publications: () =>
    request<{ publications: Record<string, unknown>[] }>(
      "/publications"
    ),

  createEvent: (payload: Record<string, unknown>) =>
    request<{
      event_id: string;
      invalidation: {
        edge_id: string;
        reason: string;
        affected_task_ids: string[];
        recovery_candidate_task_ids: string[];
        staled_total: number;
      };
    }>("/events", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  invalidate: (edgeId: string, reason: string) =>
    request<Record<string, unknown>>("/cache/invalidate", {
      method: "POST",
      body: JSON.stringify({ edge_id: edgeId, reason }),
    }),

  precompute: (payload: Record<string, unknown>) =>
    request<{ precomputed: ScenarioResult[] }>("/cache/precompute", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  rebuild: (newCutoff: string) =>
    request<Record<string, unknown>>("/cache/rebuild", {
      method: "POST",
      body: JSON.stringify({ new_cutoff: newCutoff }),
    }),

  cacheIndex: () =>
    request<{ cache_index: CacheRow[] }>("/cache/index"),

  networkVersions: () =>
    request<{
      versions: Array<{
        version_id: number;
        cutoff_at: string;
        edge_fingerprint: string;
        event_fingerprint: string;
        note: string | null;
      }>;
    }>("/network-versions"),

  reseed: () => request("/admin/reseed", { method: "POST" }),

  applyC2: () => request("/admin/apply-c2-revision", { method: "POST" }),

  // --------------------------------------------------------- 证据融合 --
  listEvidence: (sourceType?: string, status?: string) =>
    request<{ evidence: import("./types").EvidenceRow[] }>(
      `/evidence?${new URLSearchParams({
        ...(sourceType ? { source_type: sourceType } : {}),
        ...(status ? { status } : {}),
      })}`
    ),

  evidenceDetail: (evidenceId: string) =>
    request<{
      evidence_id: string;
      revisions: import("./types").EvidenceRow[];
      projections: import("./types").ProjectionRow[];
      audits: Array<Record<string, unknown>>;
    }>(`/evidence/${encodeURIComponent(evidenceId)}`),

  ingestEvidence: (payload: Record<string, unknown>) =>
    request<Record<string, unknown>>("/evidence", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  reviseEvidence: (evidenceId: string, payload: Record<string, unknown>, action: "revise" | "backfill") =>
    request<Record<string, unknown>>(
      `/evidence/${encodeURIComponent(evidenceId)}/${action}`,
      { method: "POST", body: JSON.stringify(payload) }
    ),

  withdrawEvidence: (evidenceId: string, changeNote?: string) =>
    request<Record<string, unknown>>(
      `/evidence/${encodeURIComponent(evidenceId)}/withdraw${
        changeNote ? `?change_note=${encodeURIComponent(changeNote)}` : ""
      }`,
      { method: "POST" }
    ),

  listProjections: (accepted?: boolean, edgeId?: string) =>
    request<{ projections: import("./types").ProjectionRow[] }>(
      `/evidence/projections/list?${new URLSearchParams({
        ...(accepted === undefined ? {} : { accepted: String(accepted) }),
        ...(edgeId ? { edge_id: edgeId } : {}),
      })}`
    ),

  listConclusions: (params?: Record<string, string>) =>
    request<{ conclusions: import("./types").ConclusionRow[] }>(
      `/evidence/conclusions/list?${new URLSearchParams(params ?? {})}`
    ),

  listFusionRuns: () =>
    request<{ runs: import("./types").FusionRunRow[] }>("/evidence/runs/list"),

  listDerivedClosures: () =>
    request<{ derived_closures: import("./types").DerivedClosureRow[] }>(
      "/evidence/derived-closures/list"
    ),

  listImpactLogs: (evidenceId?: string) =>
    request<{ impact_logs: import("./types").ImpactLogRow[] }>(
      `/evidence/impact-logs/list${
        evidenceId ? `?evidence_id=${encodeURIComponent(evidenceId)}` : ""
      }`
    ),

  getStrategy: () =>
    request<{ active_strategy: "official_only" | "multi_source" }>(
      "/evidence/strategy"
    ),

  setStrategy: (strategy: string) =>
    request<Record<string, unknown>>("/evidence/strategy", {
      method: "POST",
      body: JSON.stringify({ strategy }),
    }),

  runFusion: (cutoffAt: string, strategy?: string) =>
    request<import("./types").FusionRunResult>("/evidence/fusion/run", {
      method: "POST",
      body: JSON.stringify({ cutoff_at: cutoffAt, ...(strategy ? { strategy } : {}) }),
    }),

  taskManifest: (taskId: string) =>
    request<Record<string, unknown>>(`/evidence/tasks/${taskId}/manifest`),

  gridDrilldown: (taskId: string, gridId: string) =>
    request<import("./types").EvidenceDrilldown>(
      `/evidence/tasks/${taskId}/grids/${encodeURIComponent(gridId)}/drilldown`
    ),
};
