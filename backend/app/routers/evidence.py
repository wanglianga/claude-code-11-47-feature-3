"""道路通行证据融合路由：接入、投影查询、融合、冲突解释与审计下钻。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..schemas_api import EvidenceIngest, FusionRunRequest, FusionStrategyRequest
from ..services import evidence as ev_service
from .deps import get_db, jsonable

router = APIRouter(prefix="/evidence", tags=["证据融合"])


@router.post("")
def ingest(payload: EvidenceIngest, conn=Depends(get_db)):
    """接入一条候选道路事件证据并立即执行投影（含空间/方向/时间留痕）。"""
    try:
        out = ev_service.ingest_evidence(conn, payload.model_dump(), action="ingest")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return jsonable(out)


def _revise(conn, evidence_id: str, payload: EvidenceIngest, action: str, note: str | None):
    if payload.evidence_id != evidence_id:
        raise HTTPException(400, "路径 evidence_id 与正文不一致")
    data = payload.model_dump()
    if note:
        data["change_note"] = note
    try:
        out = ev_service.ingest_evidence(
            conn, data, action=action, evidence_id=evidence_id
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return jsonable(out)


@router.post("/{evidence_id}/revise")
def revise(evidence_id: str, payload: EvidenceIngest, conn=Depends(get_db)):
    """修正证据：追加修订行并重新投影，旧修订保留可复现。"""
    return _revise(conn, evidence_id, payload, "revise", payload.change_note)


@router.post("/{evidence_id}/backfill")
def backfill(evidence_id: str, payload: EvidenceIngest, conn=Depends(get_db)):
    """补传证据：观测时间早、上传时间晚，按晚传折减进入时间区间推导。"""
    return _revise(conn, evidence_id, payload, "backfill", payload.change_note)


@router.post("/{evidence_id}/withdraw")
def withdraw(evidence_id: str, conn=Depends(get_db), change_note: str | None = None):
    """临时撤回：最新修订标记 withdrawn，只重算引用该边与时间窗的情景。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM road_evidence WHERE evidence_id=%s "
            "ORDER BY revision_no DESC LIMIT 1",
            (evidence_id,),
        )
        latest = cur.fetchone()
    if latest is None:
        raise HTTPException(404, "证据不存在")
    now = datetime.now(timezone.utc)
    payload = EvidenceIngest(
        evidence_id=evidence_id,
        source_type=latest["source_type"],
        source_ref=latest["source_ref"],
        source_org=latest["source_org"],
        geom_x=float(latest["geom_x"]),
        geom_y=float(latest["geom_y"]),
        location_error_m=float(latest["location_error_m"]),
        location_text=latest["location_text"],
        observed_at=latest["observed_at"],
        uploaded_at=now,
        bearing_deg=latest["bearing_deg"],
        threshold_value=latest["threshold_value"],
        observed_value=latest["observed_value"],
        observed_recovery_at=latest["observed_recovery_at"],
        offline=bool(latest["offline"]),
        threshold_unit=latest["threshold_unit"],
        raw_text=latest["raw_text"] or "",
        change_note=change_note or "证据临时撤回",
    )
    return _revise(conn, evidence_id, payload, "withdraw", change_note)


@router.get("")
def list_evidence(source_type: str | None = None, status: str | None = None,
                  conn=Depends(get_db)):
    """每条证据的最新修订（含指纹与生命周期状态）。"""
    clauses, params = [], []
    if source_type:
        clauses.append("source_type=%s")
        params.append(source_type)
    if status:
        clauses.append("status=%s")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (evidence_id) *
            FROM road_evidence
            {where}
            ORDER BY evidence_id, revision_no DESC
            """,
            params,
        )
        rows = cur.fetchall()
    return jsonable({"evidence": rows})


@router.get("/strategy")
def get_strategy(conn=Depends(get_db)):
    return jsonable({"active_strategy": ev_service.get_active_strategy(conn)})


@router.post("/strategy")
def set_strategy(payload: FusionStrategyRequest, conn=Depends(get_db)):
    if payload.strategy not in ("official_only", "multi_source"):
        raise HTTPException(400, "策略仅支持 official_only / multi_source")
    return jsonable(ev_service.set_active_strategy(conn, payload.strategy))


@router.get("/{evidence_id}")
def evidence_detail(evidence_id: str, conn=Depends(get_db)):
    """单条证据的全部修订、投影与审计留痕。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM road_evidence WHERE evidence_id=%s ORDER BY revision_no",
            (evidence_id,),
        )
        revisions = cur.fetchall()
        if not revisions:
            raise HTTPException(404, "证据不存在")
        cur.execute(
            "SELECT * FROM evidence_projections WHERE evidence_id=%s "
            "ORDER BY evidence_revision, edge_id",
            (evidence_id,),
        )
        projections = cur.fetchall()
        cur.execute(
            "SELECT * FROM road_evidence_audits WHERE evidence_id=%s ORDER BY revision_no",
            (evidence_id,),
        )
        audits = cur.fetchall()
    return jsonable({"evidence_id": evidence_id, "revisions": revisions,
                     "projections": projections, "audits": audits})


@router.get("/projections/list")
def list_projections(edge_id: str | None = None, accepted: bool | None = None,
                     conn=Depends(get_db)):
    clauses, params = [], []
    if edge_id:
        clauses.append("p.edge_id=%s")
        params.append(edge_id)
    if accepted is not None:
        clauses.append("p.accepted=%s")
        params.append(accepted)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT p.*, e.source_type, e.source_ref, e.evidence_fingerprint, e.status AS evidence_status
            FROM evidence_projections p
            JOIN road_evidence e
              ON e.evidence_id=p.evidence_id AND e.revision_no=p.evidence_revision
            {where}
            ORDER BY p.edge_id, p.accepted, p.evidence_id
            """,
            params,
        )
        rows = cur.fetchall()
    return jsonable({"projections": rows})


@router.get("/conclusions/list")
def list_conclusions(edge_id: str | None = None, level: str | None = None,
                     run_id: int | None = None, conn=Depends(get_db)):
    clauses, params = [], []
    if edge_id:
        clauses.append("c.edge_id=%s")
        params.append(edge_id)
    if level:
        clauses.append("c.level=%s")
        params.append(level)
    if run_id:
        clauses.append("c.run_id=%s")
        params.append(run_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT c.* FROM edge_evidence_conclusions c {where} "
            "ORDER BY c.run_id DESC, c.edge_id, c.valid_from",
            params,
        )
        rows = cur.fetchall()
    return jsonable({"conclusions": rows})


@router.get("/runs/list")
def list_runs(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,
                      (SELECT count(*) FROM edge_evidence_conclusions c WHERE c.run_id=r.run_id) AS conclusion_count
               FROM evidence_fusion_runs r ORDER BY r.run_id DESC"""
        )
        rows = cur.fetchall()
    return jsonable({"runs": rows})


@router.get("/derived-closures/list")
def list_derived(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM derived_road_closures ORDER BY created_at DESC")
        rows = cur.fetchall()
    return jsonable({"derived_closures": rows})


@router.get("/impact-logs/list")
def list_impacts(evidence_id: str | None = None, conn=Depends(get_db)):
    with conn.cursor() as cur:
        if evidence_id:
            cur.execute(
                "SELECT * FROM evidence_impact_logs WHERE evidence_id=%s ORDER BY impact_id DESC",
                (evidence_id,),
            )
        else:
            cur.execute("SELECT * FROM evidence_impact_logs ORDER BY impact_id DESC")
        rows = cur.fetchall()
    return jsonable({"impact_logs": rows})


@router.post("/fusion/run")
def run_fusion(payload: FusionRunRequest, conn=Depends(get_db)):
    """按策略与截止点执行融合；confirmed 结论物化为派生封路并定向失效受影响任务。"""
    strategy = payload.strategy or ev_service.get_active_strategy(conn)
    if strategy not in ("official_only", "multi_source"):
        raise HTTPException(400, "策略仅支持 official_only / multi_source")
    return jsonable(ev_service.run_fusion(conn, strategy, payload.cutoff_at))


@router.get("/tasks/{task_id}/manifest")
def task_manifest(task_id: str, conn=Depends(get_db)):
    """任务计算时固化的证据清单（后续证据撤回不改写此清单）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM task_evidence_manifests WHERE task_id=%s", (task_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "该任务尚无证据清单（可能在证据功能启用前计算）")
    return jsonable(row)


@router.get("/tasks/{task_id}/grids/{grid_id}/drilldown")
def grid_drilldown(task_id: str, grid_id: str, conn=Depends(get_db)):
    """从被排除路径下钻：见证边上的结论等级、投影拒绝原因与原始证据。"""
    out = ev_service.grid_evidence_drilldown(conn, task_id, grid_id)
    if not out.get("found"):
        raise HTTPException(404, "该任务下没有此网格的结果")
    return jsonable(out)
