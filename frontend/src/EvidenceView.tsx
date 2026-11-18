import { useEffect, useState } from "react";
import { api } from "./api";
import type {
  ConclusionRow,
  DerivedClosureRow,
  EvidenceDrilldown,
  EvidenceRow,
  FusionRunResult,
  FusionRunRow,
  ImpactLogRow,
  ProjectionRow,
} from "./types";

const TABS = [
  "overview",
  "evidence",
  "projections",
  "conclusions",
  "impact",
  "drilldown",
] as const;

const LEVEL_LABEL: Record<string, { text: string; cls: string }> = {
  confirmed_closure: { text: "确定封闭", cls: "uncovered" },
  likely_closure: { text: "高倾向·不定态", cls: "unknown" },
  conflict: { text: "来源冲突", cls: "stale" },
  unknown: { text: "未知", cls: "done" },
};

const SOURCE_LABEL: Record<string, string> = {
  water_sensor: "积水传感器",
  inspection_photo: "巡查照片",
  crowd_report: "群众上报",
};

const fmt = (s: string | null) => (s ? s.replace("T", " ").slice(0, 16) : "—");

export default function EvidenceView({
  pushToast,
  initialPrefill,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
  initialPrefill?: { task_id: string; grid_id: string } | null;
}) {
  const [tab, setTab] = useState<(typeof TABS)[number]>("overview");
  const [strategy, setStrategy] = useState("multi_source");
  const [runs, setRuns] = useState<FusionRunRow[]>([]);
  const [evidence, setEvidence] = useState<EvidenceRow[]>([]);
  const [projections, setProjections] = useState<ProjectionRow[]>([]);
  const [conclusions, setConclusions] = useState<ConclusionRow[]>([]);
  const [impacts, setImpacts] = useState<ImpactLogRow[]>([]);
  const [derived, setDerived] = useState<DerivedClosureRow[]>([]);
  const [cutoff, setCutoff] = useState("2026-07-15T08:45:00+00:00");
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [drillInput, setDrillInput] = useState({ task_id: "", grid_id: "" });
  const [drill, setDrill] = useState<EvidenceDrilldown | null>(null);

  // 从情景页跨模块跳入：自动切到下钻页并预填任务/网格
  useEffect(() => {
    if (initialPrefill?.task_id && initialPrefill?.grid_id) {
      setDrillInput(initialPrefill);
      setTab("drilldown");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPrefill?.task_id, initialPrefill?.grid_id]);

  const refreshAll = () => {
    api.getStrategy().then((r) => setStrategy(r.active_strategy)).catch(() => undefined);
    api.listFusionRuns().then((r) => setRuns(r.runs)).catch(() => undefined);
    api.listEvidence().then((r) => setEvidence(r.evidence)).catch(() => undefined);
    api.listProjections().then((r) => setProjections(r.projections)).catch(() => undefined);
    api.listConclusions().then((r) => setConclusions(r.conclusions)).catch(() => undefined);
    api.listImpactLogs().then((r) => setImpacts(r.impact_logs)).catch(() => undefined);
    api.listDerivedClosures().then((r) => setDerived(r.derived_closures)).catch(() => undefined);
  };

  useEffect(refreshAll, []);

  const runFusion = async (useStrategy?: string) => {
    try {
      const r: FusionRunResult = await api.runFusion(cutoff, useStrategy);
      pushToast(
        `融合完成：确定封闭 ${r.level_breakdown.confirmed_closure}、冲突 ${r.level_breakdown.conflict}、` +
          `失效任务 ${r.invalidation.staled_total}`
      );
      refreshAll();
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const switchStrategy = async (next: string) => {
    try {
      await api.setStrategy(next);
      setStrategy(next);
      pushToast(`融合策略已切换为 ${next === "official_only" ? "仅官方来源" : "多源加权"}，全部缓存已失效`);
      refreshAll();
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const openEvidence = async (id: string) => {
    try {
      setDetail((await api.evidenceDetail(id)) as unknown as Record<string, unknown>);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const doDrilldown = async () => {
    try {
      setDrill(await api.gridDrilldown(drillInput.task_id, drillInput.grid_id));
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  return (
    <div className="full-page">
      <div className="card">
        <h3>道路通行证据融合与冲突解释（规则 EV-FUSION-R1）</h3>
        <div className="small muted">
          每条证据保留来源坐标、观测/上传时间、方向、阈值、定位误差与原始指纹；投影为
          （边, 方向, 有效时间区间）后按可靠性×空间/方向/晚传折减加权，<b>禁止按来源数量多数表决</b>。
          冲突或证据不足只给 conflict/unknown，绝不生成无来源的确定封路。
        </div>
        <nav className="tabs" style={{ borderBottom: "none", padding: 0 }}>
          {TABS.map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {
                {
                  overview: "策略与融合",
                  evidence: "证据清单",
                  projections: "投影解释",
                  conclusions: "结论与冲突",
                  impact: "影响审计",
                  drilldown: "网格下钻",
                }[t]
              }
            </button>
          ))}
        </nav>
      </div>

      {tab === "overview" && (
        <>
          <div className="card">
            <h3>融合策略（两种口径可切换，切换即失效全部缓存）</h3>
            <div className="row">
              <button
                className={`btn ${strategy === "multi_source" ? "" : "secondary"}`}
                onClick={() => switchStrategy("multi_source")}
              >
                多源加权融合（覆盖广，显式冲突与不确定度）
              </button>
              <button
                className={`btn ${strategy === "official_only" ? "" : "secondary"}`}
                onClick={() => switchStrategy("official_only")}
              >
                仅官方传感器/巡查（误报低，偏远道路长期未知）
              </button>
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>
              当前活动策略：<b className="mono">{strategy}</b>
            </div>
          </div>
          <div className="card">
            <h3>执行融合（显式触发，不自动改写路网）</h3>
            <div className="row">
              <label className="field">
                <span>融合截止点（UTC）</span>
                <input value={cutoff} onChange={(e) => setCutoff(e.target.value)} />
              </label>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
                <button className="btn" onClick={() => runFusion()}>按当前策略融合</button>
                <button className="btn secondary" onClick={() => runFusion("multi_source")}>多源融合</button>
                <button className="btn secondary" onClick={() => runFusion("official_only")}>官方融合</button>
              </div>
            </div>
            <div className="small muted">
              建议截止点：08:45（晚传照片上传前）、11:30（晚传与修订可见后）。
            </div>
          </div>
          <div className="card scroll-x">
            <h3>融合批次（指纹可复现校验）</h3>
            <table>
              <thead>
                <tr><th>批次</th><th>策略</th><th>截止点</th><th>结论数</th><th>输入指纹</th><th>结论指纹</th><th>规则</th></tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id}>
                    <td className="mono">#{r.run_id}</td>
                    <td>{r.strategy}</td>
                    <td className="small">{fmt(r.cutoff_at)}</td>
                    <td>{r.conclusion_count}</td>
                    <td className="mono small">{r.input_fingerprint.slice(0, 14)}…</td>
                    <td className="mono small">{r.conclusion_fingerprint.slice(0, 14)}…</td>
                    <td className="small">{r.rule_version}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card scroll-x">
            <h3>派生封路（仅 confirmed_closure 物化，均带来源）</h3>
            <table>
              <thead><tr><th>派生事件</th><th>边</th><th>方向</th><th>生效起</th><th>生效止</th><th>批次截止</th><th>状态</th></tr></thead>
              <tbody>
                {derived.map((d) => (
                  <tr key={d.derived_event_id}>
                    <td className="mono small">{d.derived_event_id}</td>
                    <td className="mono">{d.edge_id}</td>
                    <td>{d.direction}</td>
                    <td className="small">{fmt(d.effective_from)}</td>
                    <td className="small">{fmt(d.effective_to)}</td>
                    <td className="small">{fmt(d.cutoff_at)}</td>
                    <td><span className={`pill ${d.status === "active" ? "covered" : "stale"}`}>{d.status}</span></td>
                  </tr>
                ))}
                {derived.length === 0 && <tr><td colSpan={7} className="muted">尚无派生封路。</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "evidence" && (
        <div className="card scroll-x">
          <h3>证据清单（追加式修订；撤回只置状态，不删历史）</h3>
          <table>
            <thead>
              <tr>
                <th>证据</th><th>来源</th><th>坐标/误差</th><th>观测→上传</th><th>方向</th>
                <th>阈值/读数</th><th>状态</th><th>指纹</th><th>原文</th><th></th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((e) => (
                <tr key={`${e.evidence_id}-r${e.revision_no}`}>
                  <td className="mono">
                    {e.evidence_id}
                    <div className="small muted">r{e.revision_no} {e.source_ref}</div>
                  </td>
                  <td>{SOURCE_LABEL[e.source_type] ?? e.source_type}</td>
                  <td className="small">
                    ({e.geom_x}, {e.geom_y})
                    <div className="muted">±{e.location_error_m}m{e.location_text ? `｜${e.location_text}` : ""}</div>
                  </td>
                  <td className="small">
                    观 {fmt(e.observed_at)}
                    <br />传 {fmt(e.uploaded_at)}
                  </td>
                  <td>{e.bearing_deg == null ? "未给方向" : `${e.bearing_deg}°`}</td>
                  <td className="small">
                    {e.offline ? <span className="warn">离线无读数</span>
                      : `${e.threshold_value ?? "—"} / ${e.observed_value ?? "—"}`}
                  </td>
                  <td><span className={`pill ${e.status === "active" ? "covered" : "stale"}`}>{e.status}</span></td>
                  <td className="mono small">{e.evidence_fingerprint.slice(0, 12)}…</td>
                  <td className="small muted" style={{ maxWidth: 240 }}>{e.raw_text}</td>
                  <td><button className="btn secondary" onClick={() => openEvidence(e.evidence_id)}>修订/投影</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          {detail && (
            <div className="card" style={{ marginTop: 12 }}>
              <h3>{String((detail as { evidence_id?: string }).evidence_id)} 修订与投影留痕</h3>
              <pre className="small" style={{ whiteSpace: "pre-wrap", maxHeight: 420, overflow: "auto" }}>
                {JSON.stringify(detail, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}

      {tab === "projections" && (
        <div className="card scroll-x">
          <h3>投影解释：证据 → 边/方向/有效时间区间（拒绝行保留“为何不够”）</h3>
          <table>
            <thead>
              <tr>
                <th>证据</th><th>边</th><th>方向</th><th>立场</th><th>垂距(m)</th>
                <th>空间权重</th><th>方向匹配</th><th>晚传(分)/时间权重</th>
                <th>有效区间</th><th>判定</th><th>拒绝原因/规则轨迹</th>
              </tr>
            </thead>
            <tbody>
              {projections.map((p) => (
                <tr key={p.projection_id} className={p.accepted ? "" : "clickable"}>
                  <td className="mono small">{p.evidence_id} r{p.evidence_revision}</td>
                  <td className="mono">{p.edge_id}</td>
                  <td>{p.direction}</td>
                  <td>{p.stance}</td>
                  <td>{p.perpendicular_m.toFixed(1)}</td>
                  <td>{p.spatial_weight.toFixed(3)}</td>
                  <td className="small">{p.direction_match} ({p.direction_weight})</td>
                  <td className="small">{(p.upload_delay_sec / 60).toFixed(0)} / {p.time_weight.toFixed(3)}</td>
                  <td className="small">{fmt(p.valid_from)} → {fmt(p.valid_to)}</td>
                  <td>
                    <span className={`pill ${p.accepted ? "covered" : "stale"}`}>
                      {p.accepted ? "接受" : "拒绝"}
                    </span>
                  </td>
                  <td className="small">
                    {p.reject_reason && <div className="warn">{p.reject_reason}</div>}
                    <details>
                      <summary className="muted">规则轨迹</summary>
                      <pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>
                        {JSON.stringify(p.rule_trace, null, 1)}
                      </pre>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === "conclusions" && (
        <div className="card scroll-x">
          <h3>融合结论：等级、双侧权重与不确定度（无多数表决）</h3>
          <table>
            <thead>
              <tr>
                <th>批次</th><th>边</th><th>方向</th><th>等级</th><th>封路侧权重</th>
                <th>通行侧权重</th><th>不确定度</th><th>冲突</th><th>证据数</th><th>时间窗</th><th>来源权重明细</th>
              </tr>
            </thead>
            <tbody>
              {conclusions.map((c) => {
                const meta = LEVEL_LABEL[c.level] ?? { text: c.level, cls: "done" };
                return (
                  <tr key={c.conclusion_id}>
                    <td className="mono small">#{c.run_id}</td>
                    <td className="mono">{c.edge_id}</td>
                    <td>{c.direction}</td>
                    <td><span className={`pill ${meta.cls}`}>{meta.text}</span></td>
                    <td>{c.weight_closure.toFixed(3)}</td>
                    <td>{c.weight_open.toFixed(3)}</td>
                    <td>{c.uncertainty.toFixed(3)}</td>
                    <td>{c.has_conflict ? <span className="warn">是</span> : "否"}</td>
                    <td>{c.evidence_count}</td>
                    <td className="small">{fmt(c.valid_from)} → {fmt(c.valid_to)}</td>
                    <td className="small">
                      <details>
                        <summary className="muted">{c.evidence_refs.length} 条来源</summary>
                        <pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>
                          {JSON.stringify(c.evidence_refs, null, 1)}
                        </pre>
                      </details>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {tab === "impact" && (
        <div className="card scroll-x">
          <h3>证据变更影响审计：撤回/修正/补传只重算引用该边与时间窗的情景</h3>
          <table>
            <thead>
              <tr><th>#</th><th>证据/批次</th><th>动作</th><th>时间</th><th>受影响边</th><th>受影响任务</th><th>受影响网格</th></tr>
            </thead>
            <tbody>
              {impacts.map((l) => (
                <tr key={l.impact_id}>
                  <td>{l.impact_id}</td>
                  <td className="mono small">{l.evidence_id}</td>
                  <td><span className="pill done">{l.action}</span></td>
                  <td className="small">{fmt(l.changed_at)}</td>
                  <td className="small">{l.affected_edge_ids.map((e) => <span key={e} className="path-chip">{e}</span>)}</td>
                  <td className="small">{l.affected_task_ids.length} 个</td>
                  <td className="small">{l.affected_grid_ids.map((g) => <span key={g} className="path-chip hl">{g}</span>)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === "drilldown" && (
        <>
          <div className="card">
            <h3>从一条被排除的路径下钻：何种证据、为何不够、改变了哪个网格</h3>
            <div className="row">
              <label className="field">
                <span>任务 ID</span>
                <input value={drillInput.task_id} onChange={(e) => setDrillInput({ ...drillInput, task_id: e.target.value })} />
              </label>
              <label className="field">
                <span>网格 ID（如 GA / GF）</span>
                <input value={drillInput.grid_id} onChange={(e) => setDrillInput({ ...drillInput, grid_id: e.target.value })} />
              </label>
              <div style={{ display: "flex", alignItems: "flex-end" }}>
                <button className="btn" onClick={doDrilldown}>下钻</button>
              </div>
            </div>
          </div>
          {drill && (
            <>
              <div className="card">
                <h3>{drill.grid_id}：连接节点关联边与排除原因</h3>
                <div className="small">
                  见证边：{drill.witness_edges.join("、") || "（无 —— 网格在连接节点即被阻断）"}
                  <br />关联边：{drill.incident_edges.map((e) => <span key={e} className="path-chip hl">{e}</span>)}
                </div>
                <ul className="tight small warn">
                  {drill.share_reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </div>
              <div className="card scroll-x">
                <h3>该边上的融合结论（截止 {fmt(drill.cutoff_at)}）</h3>
                <table>
                  <thead><tr><th>边</th><th>等级</th><th>封路/通行权重</th><th>不确定度</th><th>时间窗</th></tr></thead>
                  <tbody>
                    {drill.conclusions.map((c) => {
                      const meta = LEVEL_LABEL[c.level] ?? { text: c.level, cls: "done" };
                      return (
                        <tr key={c.conclusion_id}>
                          <td className="mono">{c.edge_id}</td>
                          <td><span className={`pill ${meta.cls}`}>{meta.text}</span></td>
                          <td className="small">{c.weight_closure} / {c.weight_open}</td>
                          <td>{c.uncertainty}</td>
                          <td className="small">{fmt(c.valid_from)} → {fmt(c.valid_to)}</td>
                        </tr>
                      );
                    })}
                    {drill.conclusions.length === 0 && <tr><td colSpan={5} className="muted">该时间窗无融合结论（保持未知，不臆造状态）。</td></tr>}
                  </tbody>
                </table>
              </div>
              <div className="card scroll-x">
                <h3>投影与原始证据（含被拒绝原因）</h3>
                <table>
                  <thead><tr><th>证据</th><th>来源</th><th>边</th><th>判定</th><th>拒绝原因</th><th>原文/地名</th></tr></thead>
                  <tbody>
                    {drill.projections.map((p) => (
                      <tr key={p.projection_id}>
                        <td className="mono small">{p.evidence_id} r{p.evidence_revision}</td>
                        <td className="small">{SOURCE_LABEL[p.source_type ?? ""] ?? p.source_type}</td>
                        <td className="mono">{p.edge_id}</td>
                        <td><span className={`pill ${p.accepted ? "covered" : "stale"}`}>{p.accepted ? "接受" : "拒绝"}</span></td>
                        <td className="small warn">{p.reject_reason ?? "—"}</td>
                        <td className="small muted">{p.source_ref}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
