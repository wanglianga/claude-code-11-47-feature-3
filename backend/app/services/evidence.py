"""道路通行证据融合与冲突解释服务。

口径（对应需求条文，规则版本 EV-FUSION-R1）：
1. 证据只追加：撤回/修正/补传都新增修订行，按上传时间决定截止点可见性，旧截止点
   分析永远看到当时的修订，可精确复现。
2. 投影规则把证据映射到（边, 方向, 有效时间区间）并全程留痕；平行道路歧义、方向
   不符、传感器离线、立场不明一律拒绝投影但保留行（accepted=FALSE + 原因）。
3. 融合权重 = 来源基础可靠性 × 空间误差折减 × 方向权重 × 晚传折减；严禁按来源
   数量多数表决。同一时间窗出现两侧有效证据即 conflict，不产生确定封路。
4. 仅 confirmed_closure 物化为派生封路；likely/conflict/unknown 只记录等级与不确定
   度，引擎不会得到无来源的确定状态。
5. 证据变更沿 task_evidence_dependencies（边 + 实际通过时间窗）精准失效任务，并
   登记受影响网格；既有发布的证据清单在计算时固化，不被后续修订改写。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Optional

from psycopg.types.json import Json

from .evidence_geo import (
    EdgeGeometry,
    classify_direction,
    point_to_edge,
    spatial_weight,
    time_weight,
)

# psycopg 的 Json 默认不支持 datetime，统一使用带 default=str 的 dumps
jsonb = partial(Json, dumps=partial(json.dumps, ensure_ascii=False, default=str))

RULE_VERSION = "EV-FUSION-R1"

# 来源基础可靠性（同时写入 evidence_source_types 目录表）
SOURCE_RELIABILITY: dict[str, tuple[str, float, bool]] = {
    "water_sensor": ("积水传感器", 0.90, True),
    "inspection_photo": ("巡查照片标注", 0.85, True),
    "crowd_report": ("群众上报", 0.55, False),
}

SPATIAL_ACCEPT_MIN = 0.30                  # 空间折减权重低于此值 → 投影不接受
MIN_EVIDENCE_WEIGHT = 0.15                 # 低于此权重的证据不能建立立场
LIKELY_CLOSURE_WEIGHT = 0.30               # 高倾向封路但不确定
CONFIRM_CLOSURE_WEIGHT = 0.55              # 确定封路所需封路侧权重
DEFAULT_SENSOR_SPAN_MIN = 30               # 传感器/照片无恢复时刻时的默认持续窗
DEFAULT_CROWD_SPAN_MIN = 20
OPEN_OBSERVATION_SPAN_MIN = 5

CLOSURE_KEYWORDS = ("积水", "无法通行", "封路", "封闭", "涉水", "淹没")
OPEN_KEYWORDS = ("退水", "恢复通行", "可正常通过", "已排空", "可通")


# ----------------------------------------------------------- 基础工具 --
def fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "evfp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _reliability(source_type: str) -> float:
    return SOURCE_RELIABILITY[source_type][1]


def _is_official(source_type: str) -> bool:
    return SOURCE_RELIABILITY[source_type][2]


def infer_stance(source_type: str, observed_value: Optional[float],
                 threshold: Optional[float], raw_text: str,
                 offline: bool) -> tuple[Optional[str], Optional[str]]:
    """返回 (stance, reject_reason)。stance: closure | open | None。"""
    text = raw_text or ""
    if offline:
        return None, "sensor_offline_no_reading"
    if source_type == "water_sensor":
        if observed_value is None or threshold is None:
            return None, "sensor_offline_no_reading"
        return ("closure" if observed_value >= threshold else "open", None)
    if source_type == "inspection_photo":
        # 巡查照片以报险为目的：封闭关键词优先（“东向仍可通行”不会翻转西向封闭结论）
        if any(k in text for k in CLOSURE_KEYWORDS):
            return "closure", None
        if any(k in text for k in OPEN_KEYWORDS):
            return "open", None
        return "closure", None
    # crowd_report：必须能从原文识别立场，只描述地名而无结论不得参与
    if any(k in text for k in CLOSURE_KEYWORDS):
        return "closure", None
    if any(k in text for k in OPEN_KEYWORDS):
        return "open", None
    return None, "stance_unclear_from_text"


def _default_span(source_type: str, stance: str) -> timedelta:
    if stance == "open":
        return timedelta(minutes=OPEN_OBSERVATION_SPAN_MIN)
    if source_type == "crowd_report":
        return timedelta(minutes=DEFAULT_CROWD_SPAN_MIN)
    return timedelta(minutes=DEFAULT_SENSOR_SPAN_MIN)


# ----------------------------------------------------------- 边几何加载 --
def load_edge_geometries(conn, at_time: datetime) -> list[EdgeGeometry]:
    """加载参考时刻有效的最新边修订几何（投影与方向解析共用）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (r.edge_id) r.edge_id, r.from_node, r.to_node,
                   r.length_m, n1.geom_x AS ax, n1.geom_y AS ay,
                   n2.geom_x AS bx, n2.geom_y AS by_
            FROM road_edge_revisions r
            JOIN road_nodes n1 ON n1.node_id = r.from_node
            JOIN road_nodes n2 ON n2.node_id = r.to_node
            WHERE r.valid_from <= %s AND (r.valid_to IS NULL OR r.valid_to > %s)
            ORDER BY r.edge_id, r.revision_no DESC
            """,
            (at_time, at_time),
        )
        rows = cur.fetchall()
    return [
        EdgeGeometry(
            edge_id=r["edge_id"], from_node=r["from_node"], to_node=r["to_node"],
            length_m=float(r["length_m"]), ax=float(r["ax"]), ay=float(r["ay"]),
            bx=float(r["bx"]), by=float(r["by_"]),
        )
        for r in rows
    ]


# ----------------------------------------------------------- 证据接入 --
def ingest_evidence(conn, payload: dict[str, Any], *, action: str = "ingest",
                    evidence_id: Optional[str] = None) -> dict[str, Any]:
    """接入一条证据（或为既有证据追加修订）。随后立即投影并登记影响。"""
    eid = evidence_id or payload["evidence_id"]
    offline = bool(payload.get("offline"))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(revision_no),0)+1 AS next_rev FROM road_evidence WHERE evidence_id=%s",
            (eid,),
        )
        next_rev = cur.fetchone()["next_rev"]
        if action == "ingest" and next_rev > 1:
            raise ValueError(f"证据 {eid} 已存在，修订必须使用 revise/backfill/withdraw")

        fp_payload = {k: payload.get(k) for k in (
            "source_type", "source_ref", "geom_x", "geom_y", "location_error_m",
            "observed_at", "uploaded_at", "bearing_deg", "threshold_value",
            "observed_value", "raw_text", "observed_recovery_at", "offline",
        )}
        ev_fp = fingerprint(fp_payload)

        # 同步登记来源文档（沿用 source_documents 目录，dataset_type 新增 road_evidence）
        cur.execute(
            """INSERT INTO source_documents
                   (dataset_type, title, source_org, unit_note, source_occurred_at,
                    received_at, cutoff_at, revision_no, raw_text)
               VALUES ('road_evidence', %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING doc_id""",
            (f"{payload['source_ref']}（{SOURCE_RELIABILITY[payload['source_type']][0]}）",
             payload.get("source_org"), payload.get("threshold_unit") or "积水深度：cm；坐标：米",
             payload["observed_at"], payload["uploaded_at"], payload["uploaded_at"],
             next_rev, payload.get("raw_text")),
        )
        doc_id = cur.fetchone()["doc_id"]

        status = "withdrawn" if action == "withdraw" else "active"
        recovery_at = payload.get("observed_recovery_at")
        cur.execute(
            """INSERT INTO road_evidence
                   (evidence_id, revision_no, source_type, source_ref, source_org,
                    geom_x, geom_y, location_error_m, location_text, observed_at, uploaded_at,
                    bearing_deg, threshold_value, observed_value, threshold_unit, raw_text,
                    evidence_fingerprint, status, supersedes_revision, change_note, source_doc_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       CASE WHEN %s>1 THEN %s-1 END, %s, %s)""",
            (eid, next_rev, payload["source_type"], payload["source_ref"],
             payload.get("source_org"), payload["geom_x"], payload["geom_y"],
             payload["location_error_m"], payload.get("location_text"),
             payload["observed_at"], payload["uploaded_at"], payload.get("bearing_deg"),
             payload.get("threshold_value"),
             (None if offline else payload.get("observed_value")),
             payload.get("threshold_unit"), payload.get("raw_text"), ev_fp, status,
             next_rev, next_rev, payload.get("change_note"), doc_id),
        )
        cur.execute(
            "INSERT INTO road_evidence_audits (evidence_id, revision_no, action, change_json) "
            "VALUES (%s,%s,%s,%s)",
            (eid, next_rev, action, jsonb({"fingerprint": ev_fp, "payload": fp_payload})),
        )
        conn.commit()

    impact: dict[str, Any] = {"affected_edges": [], "affected_tasks": [], "affected_grids": []}
    if status == "active":
        projections = project_evidence(conn, eid, next_rev)
        # 只有被接受的投影才可能改变路网状态；拒绝行仅供审计，不产生影响面
        effective = [p for p in projections if p["accepted"]]
        impact = register_evidence_impact(
            conn, eid, action,
            edges=[p["edge_id"] for p in effective],
            windows=[(p["valid_from"], p["valid_to"]) for p in effective],
        )
    else:
        # 撤回：沿用旧修订 *被接受* 投影过的边定位影响，再清空其投影影响面
        old_edges = _previous_accepted_edges(conn, eid, next_rev - 1)
        impact = register_evidence_impact(conn, eid, action, edges=old_edges, windows=[])
    return {
        "evidence_id": eid,
        "revision_no": next_rev,
        "action": action,
        "fingerprint": ev_fp,
        "status": status,
        "impact": impact,
    }


def _previous_accepted_edges(conn, evidence_id: str, revision_no: int) -> list[str]:
    if revision_no < 1:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT edge_id FROM evidence_projections "
            "WHERE evidence_id=%s AND evidence_revision=%s AND accepted=TRUE",
            (evidence_id, revision_no),
        )
        return [r["edge_id"] for r in cur.fetchall()]


# ----------------------------------------------------------- 投影 --
def project_evidence(conn, evidence_id: str, revision_no: int) -> list[dict[str, Any]]:
    """对一条证据修订执行空间匹配 + 方向判定 + 时间区间推导，落库投影行。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM road_evidence WHERE evidence_id=%s AND revision_no=%s",
            (evidence_id, revision_no),
        )
        ev = cur.fetchone()
        if ev is None:
            raise ValueError("证据修订不存在")
        cur.execute(
            "DELETE FROM evidence_projections WHERE evidence_id=%s AND evidence_revision=%s",
            (evidence_id, revision_no),
        )
    if ev["status"] != "active":
        return []

    stance, stance_reject = infer_stance(
        ev["source_type"], ev["observed_value"], ev["threshold_value"],
        ev["raw_text"], ev["offline"] if "offline" in ev else False,
    )
    geometries = load_edge_geometries(conn, ev["uploaded_at"])
    upload_delay = max((ev["uploaded_at"] - ev["observed_at"]).total_seconds(), 0.0)
    t_weight = time_weight(upload_delay)
    valid_from = ev["observed_at"]
    valid_to = _resolve_valid_to(ev, stance)

    # 先计算全部候选，平行道路歧义需要候选间相互比较
    candidates: list[dict[str, Any]] = []
    for geo in geometries:
        pg = point_to_edge(float(ev["geom_x"]), float(ev["geom_y"]), geo)
        s_weight = spatial_weight(pg.perpendicular_m, float(ev["location_error_m"]))
        direction, d_match, d_weight = classify_direction(ev["bearing_deg"], geo.bearing_deg)
        candidates.append({
            "geo": geo, "pg": pg, "spatial_weight": s_weight,
            "direction": direction, "direction_match": d_match, "direction_weight": d_weight,
        })

    # 歧义判定：定位误差包络内出现多条边，且没有显著更优的唯一候选。
    # 同一物理路面的对向有向边（端点互换）不算平行道路——方向规则会把证据
    # 规范到正确车道，不能因此把方向性封闭误判成歧义。
    pair_lookup = {(g.from_node, g.to_node): g.edge_id for g in geometries}
    endpoints = {g.edge_id: (g.from_node, g.to_node) for g in geometries}
    reverse_pairs: set[frozenset[str]] = set()
    for (a, b), eid in pair_lookup.items():
        paired = pair_lookup.get((b, a))
        if paired is not None:
            reverse_pairs.add(frozenset((eid, paired)))

    def _is_pair(x: str, y: str) -> bool:
        return frozenset((x, y)) in reverse_pairs

    in_envelope = [c for c in candidates
                   if c["pg"].perpendicular_m <= float(ev["location_error_m"])]
    ambiguous = len(in_envelope) > 1 and not _has_unique_best(
        candidates, float(ev["location_error_m"])
    )
    # 若包络内多条边全部互为对向车道，则不存在平行道路歧义
    if ambiguous and in_envelope and all(
        _is_pair(in_envelope[0]["geo"].edge_id, c["geo"].edge_id)
        for c in in_envelope[1:]
    ):
        ambiguous = False

    rows: list[dict[str, Any]] = []
    for c in candidates:
        geo, pg = c["geo"], c["pg"]
        edge_id = geo.edge_id
        direction = c["direction"]
        if direction == "reverse":
            a, b = endpoints[edge_id]
            paired = pair_lookup.get((b, a))
            if paired is not None:
                edge_id, direction = paired, "forward"
        accepted = True
        reasons: list[str] = []
        if not pg.inside_segment:
            # 垂足被夹到端点：来源点不在线段覆盖范围内，连接线类候选不得靠端点距离入选
            accepted = False
            reasons.append("projection_outside_segment")
        if stance is None:
            accepted = False
            reasons.append(stance_reject or "stance_unavailable")
        if c["direction_weight"] <= 0:
            accepted = False
            reasons.append("direction_mismatch")
        if c["spatial_weight"] < SPATIAL_ACCEPT_MIN:
            accepted = False
            reasons.append("spatial_weight_below_threshold")
        if ambiguous and c["pg"].perpendicular_m <= float(ev["location_error_m"]) * 1.5:
            # 平行道路歧义：模糊带（1.5 倍定位误差）内的候选全部拒绝，不静默选边；
            # 带外候选的空间权重本身已低于接受阈值，同样不会进入融合
            accepted = False
            reasons.append("parallel_road_ambiguity")
        if pg.perpendicular_m > float(ev["location_error_m"]) * 3:
            # 过远候选不入库，避免投影表被无关边淹没
            continue
        trace = {
            "rule_version": RULE_VERSION,
            "source_type": ev["source_type"],
            "base_reliability": _reliability(ev["source_type"]),
            "perpendicular_m": round(pg.perpendicular_m, 3),
            "along_m": round(pg.along_m, 3),
            "location_error_m": ev["location_error_m"],
            "spatial_weight": round(c["spatial_weight"], 4),
            "direction_match": c["direction_match"],
            "direction_weight": c["direction_weight"],
            "canonical_edge_id": edge_id,
            "canonical_direction": direction,
            "upload_delay_sec": round(upload_delay, 1),
            "time_weight": round(t_weight, 4),
            "stance": stance,
            "ambiguous_with": [x["geo"].edge_id for x in in_envelope
                               if x["geo"].edge_id != geo.edge_id] if ambiguous else [],
        }
        rows.append({
            "evidence_id": evidence_id,
            "evidence_revision": revision_no,
            "edge_id": edge_id,
            "direction": direction,
            "stance": stance or "none",
            "valid_from": valid_from,
            "valid_to": valid_to,
            "perpendicular_m": pg.perpendicular_m,
            "along_m": pg.along_m,
            "edge_length_m": pg.edge_length_m,
            "spatial_weight": c["spatial_weight"],
            "direction_match": c["direction_match"],
            "direction_weight": c["direction_weight"],
            "time_basis": ("late_upload_discounted" if upload_delay > 0 else "observed"),
            "time_weight": t_weight,
            "upload_delay_sec": upload_delay,
            "accepted": accepted,
            "reject_reason": ";".join(reasons) if reasons else None,
            "rule_trace": trace,
        })

    # 规范化到对向边后可能出现重复 (edge_id, direction)：保留垂距最小、解释最强的一条
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["edge_id"], row["direction"])
        prev = deduped.get(key)
        if prev is None or row["perpendicular_m"] < prev["perpendicular_m"]:
            deduped[key] = row
    rows = list(deduped.values())

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO evidence_projections
                   (evidence_id, evidence_revision, edge_id, direction, stance, valid_from,
                    valid_to, perpendicular_m, along_m, edge_length_m, spatial_weight,
                    direction_match, direction_weight, time_basis, time_weight, upload_delay_sec,
                    accepted, reject_reason, rule_trace)
               VALUES (%(evidence_id)s,%(evidence_revision)s,%(edge_id)s,%(direction)s,
                       %(stance)s,%(valid_from)s,%(valid_to)s,%(perpendicular_m)s,%(along_m)s,
                       %(edge_length_m)s,%(spatial_weight)s,%(direction_match)s,
                       %(direction_weight)s,%(time_basis)s,%(time_weight)s,%(upload_delay_sec)s,
                       %(accepted)s,%(reject_reason)s,%(rule_trace)s)""",
            [{**r, "rule_trace": jsonb(r["rule_trace"])} for r in rows],
        )
        conn.commit()
    return rows


def _has_unique_best(candidates: list[dict[str, Any]], location_error: float) -> bool:
    """误差包络内是否存在空间权重显著领先的唯一候选（差距 ≥0.25 视为可判定）。"""
    envelope = [c for c in candidates
                if c["pg"].perpendicular_m <= location_error]
    if not envelope:
        return False
    ranked = sorted(envelope, key=lambda c: c["spatial_weight"], reverse=True)
    if len(ranked) == 1:
        return True
    return ranked[0]["spatial_weight"] - ranked[1]["spatial_weight"] >= 0.25


def _resolve_valid_to(ev: dict, stance: Optional[str]) -> Optional[datetime]:
    recovery = ev.get("observed_recovery_at") if isinstance(ev, dict) else None
    if recovery is not None:
        return recovery
    if stance is None:
        return None
    return ev["observed_at"] + _default_span(ev["source_type"], stance)


# ----------------------------------------------------------- 融合策略配置 --
def get_active_strategy(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT strategy FROM evidence_fusion_config LIMIT 1")
        row = cur.fetchone()
    return row["strategy"] if row else "multi_source"


def set_active_strategy(conn, strategy: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO evidence_fusion_config (strategy) VALUES (%s)
               ON CONFLICT ((single_row)) DO UPDATE SET strategy=EXCLUDED.strategy, updated_at=now()
               RETURNING strategy, updated_at""",
            (strategy,),
        )
        row = cur.fetchone()
        # 策略切换改变派生事件集合：全部已完成任务失效，等待按新策略重算
        cur.execute("UPDATE compute_tasks SET stale=TRUE WHERE status='done'")
        cur.execute("UPDATE coverage_cache_index SET stale=TRUE")
        conn.commit()
    return {"active_strategy": row["strategy"], "updated_at": row["updated_at"]}


# ----------------------------------------------------------- 融合 --
@dataclass
class _FusionItem:
    edge_id: str
    direction: str
    valid_from: datetime
    valid_to: Optional[datetime]
    stance: str
    weight: float
    evidence_id: str
    revision_no: int
    projection_id: int
    official: bool
    reliability: float
    fingerprint: str


def _load_visible_evidence(conn, cutoff: datetime) -> dict[tuple[str, int], dict]:
    """截止点可见的每条证据最新修订（received 口径用 uploaded_at）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (evidence_id) *
            FROM road_evidence
            WHERE uploaded_at <= %s
            ORDER BY evidence_id, revision_no DESC
            """,
            (cutoff,),
        )
        return {(r["evidence_id"], r["revision_no"]): r for r in cur.fetchall()}


def run_fusion(conn, strategy: str, cutoff_at: datetime) -> dict[str, Any]:
    """执行一次融合：投影 → 同窗分组 → 加权定级 → 物化 confirmed 派生封路。"""
    active_strategy = get_active_strategy(conn)
    evidence = _load_visible_evidence(conn, cutoff_at)
    active_ev = {k: v for k, v in evidence.items() if v["status"] == "active"}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.*, e.source_type, e.evidence_fingerprint
            FROM evidence_projections p
            JOIN road_evidence e
              ON e.evidence_id = p.evidence_id
             AND e.revision_no = p.evidence_revision
            WHERE p.accepted = TRUE AND e.uploaded_at <= %s
            ORDER BY p.edge_id, p.valid_from
            """,
            (cutoff_at,),
        )
        projection_rows = cur.fetchall()

    items: list[_FusionItem] = []
    input_material: list[dict] = []
    for p in projection_rows:
        key = (p["evidence_id"], p["evidence_revision"])
        ev = active_ev.get(key)
        if ev is None:
            continue  # 截止点最新修订已撤回：旧修订不参与
        official = _is_official(ev["source_type"])
        if strategy == "official_only" and not official:
            continue
        reliability = _reliability(ev["source_type"])
        weight = reliability * float(p["spatial_weight"]) * float(p["direction_weight"]) \
            * float(p["time_weight"])
        items.append(_FusionItem(
            edge_id=p["edge_id"], direction=p["direction"],
            valid_from=p["valid_from"], valid_to=p["valid_to"],
            stance=p["stance"], weight=weight,
            evidence_id=p["evidence_id"], revision_no=p["evidence_revision"],
            projection_id=p["projection_id"], official=official,
            reliability=reliability, fingerprint=ev["evidence_fingerprint"],
        ))
        input_material.append({
            "evidence_id": p["evidence_id"], "revision": p["evidence_revision"],
            "edge_id": p["edge_id"], "direction": p["direction"],
            "fingerprint": ev["evidence_fingerprint"], "weight": round(weight, 4),
            "official": official, "stance": p["stance"],
        })

    groups = _group_windows(items)
    conclusions: list[dict[str, Any]] = []
    for (edge_id, direction), windows in sorted(groups.items()):
        for window in windows:
            conclusions.append(
                _build_conclusion(edge_id, direction, window, strategy, cutoff_at)
            )

    input_fp = fingerprint({"cutoff": cutoff_at, "items": input_material,
                            "strategy": strategy, "rule": RULE_VERSION})
    conclusion_fp = fingerprint({"conclusions": [
        {k: c[k] for k in ("edge_id", "direction", "level", "valid_from", "valid_to",
                           "weight_closure", "weight_open")}
        for c in conclusions
    ]})

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO evidence_fusion_runs
                   (strategy, cutoff_at, input_fingerprint, conclusion_fingerprint, rule_version)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (strategy, cutoff_at, input_fingerprint)
               DO UPDATE SET status='completed'
               RETURNING run_id, (xmax = 0) AS inserted""",
            (strategy, cutoff_at, input_fp, conclusion_fp, RULE_VERSION),
        )
        run_row = cur.fetchone()
        run_id = run_row["run_id"]
        # 幂等：同一输入指纹重复执行时先清旧结论（先解派生封路外键，refs 随 CASCADE 清）
        cur.execute("DELETE FROM derived_road_closures WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM edge_evidence_conclusions WHERE run_id=%s", (run_id,))
        cur.executemany(
            """INSERT INTO edge_evidence_conclusions
                   (run_id, strategy, edge_id, direction, valid_from, valid_to, level,
                    weight_closure, weight_open, uncertainty, has_conflict, evidence_count,
                    evidence_refs, rule_trace, cutoff_at)
               VALUES (%(run_id)s,%(strategy)s,%(edge_id)s,%(direction)s,%(valid_from)s,
                       %(valid_to)s,%(level)s,%(weight_closure)s,%(weight_open)s,
                       %(uncertainty)s,%(has_conflict)s,%(evidence_count)s,
                       %(evidence_refs)s,%(rule_trace)s,%(cutoff_at)s)""",
            [{**c, "run_id": run_id, "cutoff_at": cutoff_at,
              "evidence_refs": jsonb(c["evidence_refs"]), "rule_trace": jsonb(c["rule_trace"])}
             for c in conclusions],
        )
        cur.execute("SELECT conclusion_id FROM edge_evidence_conclusions WHERE run_id=%s",
                    (run_id,))
        concl_ids = [r["conclusion_id"] for r in cur.fetchall()]
        if concl_ids:
            cur.executemany(
                """INSERT INTO conclusion_evidence_refs
                       (conclusion_id, evidence_id, evidence_revision, projection_id,
                        stance, weight)
                   SELECT c.conclusion_id, ref->>'evidence_id',
                          (ref->>'revision')::int, (ref->>'projection_id')::bigint,
                          ref->>'stance', (ref->>'weight')::float
                   FROM edge_evidence_conclusions c,
                        jsonb_array_elements(c.evidence_refs) AS ref
                   WHERE c.conclusion_id = ANY(%s)
                   ON CONFLICT DO NOTHING""",
                [(concl_ids,)],
            )
        conn.commit()

    derived = _materialize_derived_closures(conn, run_id, strategy, cutoff_at, conclusion_fp)
    invalidation = _invalidate_for_fusion_diff(conn, strategy, run_id, conclusions)
    return {
        "run_id": run_id,
        "strategy": strategy,
        "active_strategy": active_strategy,
        "cutoff_at": cutoff_at,
        "input_fingerprint": input_fp,
        "conclusion_fingerprint": conclusion_fp,
        "conclusion_count": len(conclusions),
        "level_breakdown": _level_breakdown(conclusions),
        "derived_closures": derived,
        "invalidation": invalidation,
        "rule_version": RULE_VERSION,
    }


def _tasks_affected_by_edge_windows(
    conn, edge_windows: dict[str, list[tuple[datetime, Optional[datetime]]]]
) -> set[str]:
    """定位引用受影响边且时间窗相交的任务（含被封死、未实际通过该边的任务）。

    两条通道：
      a) task_evidence_dependencies：实际通行时间窗相交；
      b) 任务证据清单中的结论边（被阻断而没走成的边）与任务 [出发, 时限] 相交。
    边的窗口列表为空表示证据整体撤回（无时间约束）：引用该边的任务全部命中。
    """
    edge_ids = sorted(edge_windows)
    if not edge_ids:
        return set()
    affected: set[str] = set()

    def _hit(edge_id: str, t_from: datetime, t_to: datetime) -> bool:
        windows = edge_windows.get(edge_id, [])
        return not windows or _intersects_any(t_from, t_to, windows)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT task_id, edge_id, traversed_from, traversed_to "
            "FROM task_evidence_dependencies WHERE edge_id = ANY(%s)",
            (edge_ids,),
        )
        for r in cur.fetchall():
            if _hit(r["edge_id"], r["traversed_from"], r["traversed_to"]):
                affected.add(str(r["task_id"]))
        # 通道 b：清单记录了该边结论、且该边是任务所评估网格连接节点出边的任务，
        # 按情景整体时间窗判定（避免把不相关任务的同批次清单误判为受影响）
        cur.execute(
            """
            SELECT DISTINCT t.task_id, c.edge_id, s.departure_time,
                   s.departure_time + (s.max_trip_minutes || ' minutes')::interval AS deadline
            FROM task_evidence_manifests m
            JOIN compute_tasks t ON t.task_id = m.task_id
            JOIN scenarios s ON s.scenario_id = t.scenario_id
            JOIN edge_evidence_conclusions c ON c.run_id = m.run_id
            JOIN road_edge_revisions r ON r.edge_id = c.edge_id
            JOIN grid_connectors gc ON gc.node_id = r.from_node
            JOIN grid_share_results g ON g.task_id = t.task_id AND g.grid_id = gc.grid_id
            WHERE c.edge_id = ANY(%s)
            """,
            (edge_ids,),
        )
        for r in cur.fetchall():
            if _hit(r["edge_id"], r["departure_time"], r["deadline"]):
                affected.add(str(r["task_id"]))
    return affected


def _grids_affected_by_edges(conn, edge_ids: list[str], task_ids: list[str]) -> list[str]:
    grids: set[str] = set()
    if task_ids:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT grid_id FROM grid_share_results
                   WHERE task_id = ANY(%s)
                     AND (witness_edge_path ?| %s OR reason IS NOT NULL)""",
                (task_ids, edge_ids),
            )
            grids.update(r["grid_id"] for r in cur.fetchall())
    # 连接节点的出边命中受影响边：即使任务没走成也登记网格影响
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT gc.grid_id FROM grid_connectors gc
               JOIN road_edge_revisions r ON r.from_node = gc.node_id
               WHERE r.edge_id = ANY(%s)""",
            (edge_ids,),
        )
        grids.update(r["grid_id"] for r in cur.fetchall())
    return sorted(grids)


def _invalidate_for_fusion_diff(conn, strategy: str, new_run_id: int,
                                new_conclusions: list[dict[str, Any]]) -> dict[str, Any]:
    """新融合批次相对同策略上一批次的 confirmed 封路差异，定向失效受影响任务。

    新增与撤销的封路都要失效：撤销可能让此前不可达区域恢复，不能只看新结论。
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run_id FROM evidence_fusion_runs
               WHERE strategy=%s AND run_id < %s AND status='completed'
               ORDER BY run_id DESC LIMIT 1""",
            (strategy, new_run_id),
        )
        prev_row = cur.fetchone()
        cur.execute(
            "SELECT edge_id, valid_from, valid_to FROM edge_evidence_conclusions "
            "WHERE run_id=%s AND level='confirmed_closure'",
            (new_run_id,),
        )
        new_rows = cur.fetchall()
    prev_rows = []
    if prev_row:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT edge_id, valid_from, valid_to FROM edge_evidence_conclusions "
                "WHERE run_id=%s AND level='confirmed_closure'",
                (prev_row["run_id"],),
            )
            prev_rows = cur.fetchall()

    def _key(rows):
        out: dict[str, list[tuple[datetime, Optional[datetime]]]] = {}
        for r in rows:
            out.setdefault(r["edge_id"], []).append((r["valid_from"], r["valid_to"]))
        return out

    prev_map, new_map = _key(prev_rows), _key(new_rows)
    changed_edges = sorted(set(prev_map) | set(new_map))
    changed_edges = [e for e in changed_edges if prev_map.get(e) != new_map.get(e)]
    merged: dict[str, list[tuple[datetime, Optional[datetime]]]] = {}
    for e in changed_edges:
        merged[e] = prev_map.get(e, []) + new_map.get(e, [])

    affected = _tasks_affected_by_edge_windows(conn, merged)
    affected_grids: list[str] = []
    if affected:
        with conn.cursor() as cur:
            cur.execute("UPDATE compute_tasks SET stale=TRUE WHERE task_id = ANY(%s)",
                        (list(affected),))
            cur.execute("UPDATE coverage_cache_index SET stale=TRUE WHERE task_id = ANY(%s)",
                        (list(affected),))
            conn.commit()
    if changed_edges:
        affected_grids = _grids_affected_by_edges(conn, changed_edges, list(affected))
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO evidence_impact_logs
                       (evidence_id, action, affected_edge_ids, affected_task_ids,
                        affected_grid_ids, detail_json)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (f"fusion-run-{new_run_id}", "fusion_diff", jsonb(changed_edges),
                 jsonb(sorted(affected)), jsonb(affected_grids),
                 jsonb({"strategy": strategy,
                        "previous_confirmed": {k: [str(w) for w in v] for k, v in prev_map.items()},
                        "new_confirmed": {k: [str(w) for w in v] for k, v in new_map.items()}})),
            )
            conn.commit()
    return {
        "changed_edges": changed_edges,
        "affected_task_ids": sorted(affected),
        "affected_grid_ids": affected_grids,
        "staled_total": len(affected),
    }


def _group_windows(items: list[_FusionItem]) -> dict[tuple[str, str], list[list[_FusionItem]]]:
    """按 (边, 方向) 聚合，方向 both 的证据同时进入 forward/reverse 两组；
    组内再按时间区间重叠切窗，每个重叠窗产出一条结论。"""
    per_edge: dict[str, list[_FusionItem]] = {}
    for it in items:
        per_edge.setdefault(it.edge_id, []).append(it)

    result: dict[tuple[str, str], list[list[_FusionItem]]] = {}
    for edge_id, edge_items in sorted(per_edge.items()):
        directional = {d: [it for it in edge_items if it.direction == d]
                       for d in ("forward", "reverse", "both")}
        labels = []
        if directional["forward"] or directional["reverse"]:
            if directional["forward"]:
                labels.append(("forward", directional["forward"] + directional["both"]))
            if directional["reverse"]:
                labels.append(("reverse", directional["reverse"] + directional["both"]))
        else:
            labels.append(("both", directional["both"]))
        for direction, group in labels:
            result[(edge_id, direction)] = _merge_interval_windows(group)
    return result


def _merge_interval_windows(items: list[_FusionItem]) -> list[list[_FusionItem]]:
    """把时间区间相交的证据归入同一窗（开放区间 valid_to=None 吸收后续区间）。"""
    ordered = sorted(items, key=lambda it: it.valid_from)
    windows: list[list[_FusionItem]] = []
    cur: list[_FusionItem] = []
    cur_end: Optional[datetime] = None
    for it in ordered:
        if not cur:
            cur, cur_end = [it], it.valid_to
            continue
        if cur_end is None or it.valid_from <= cur_end:
            cur.append(it)
            if it.valid_to is not None and (cur_end is None or it.valid_to > cur_end):
                cur_end = it.valid_to
        else:
            windows.append(cur)
            cur, cur_end = [it], it.valid_to
    if cur:
        windows.append(cur)
    return windows


def _build_conclusion(edge_id: str, direction: str, window: list[_FusionItem],
                      strategy: str, cutoff: datetime) -> dict[str, Any]:
    closure = [it for it in window if it.stance == "closure"]
    opened = [it for it in window if it.stance == "open"]
    wc = round(sum(it.weight for it in closure), 4)
    wo = round(sum(it.weight for it in opened), 4)
    has_official_closure = any(it.official for it in closure)
    conflict = wc >= MIN_EVIDENCE_WEIGHT and wo >= MIN_EVIDENCE_WEIGHT

    if conflict:
        level = "conflict"
        uncertainty = min(1.0, 0.5 + 0.5 * (1 - abs(wc - wo) / max(wc, wo)))
    elif wc >= CONFIRM_CLOSURE_WEIGHT and (strategy == "multi_source" or has_official_closure):
        level = "confirmed_closure"
        uncertainty = max(0.0, 1.0 - min(1.0, wc / 0.8))
    elif wc >= LIKELY_CLOSURE_WEIGHT:
        level = "likely_closure"
        uncertainty = max(0.25, 1.0 - min(1.0, wc / 0.8))
    else:
        level = "unknown"
        uncertainty = 1.0 - min(1.0, (wc + wo) / 0.8)

    refs = [
        {
            "evidence_id": it.evidence_id,
            "revision": it.revision_no,
            "projection_id": it.projection_id,
            "stance": it.stance,
            "weight": round(it.weight, 4),
            "official": it.official,
            "reliability": it.reliability,
            "fingerprint": it.fingerprint,
        }
        for it in sorted(window, key=lambda x: (x.stance, -x.weight))
    ]
    starts = min(it.valid_from for it in window)
    open_ends = [it.valid_to for it in window if it.valid_to is not None]
    ends = max(open_ends) if open_ends else None
    trace = {
        "rule_version": RULE_VERSION,
        "strategy": strategy,
        "weight_formula": "reliability * spatial_weight * direction_weight * time_weight",
        "no_majority_vote": True,
        "thresholds": {
            "min_evidence_weight": MIN_EVIDENCE_WEIGHT,
            "likely_closure_weight": LIKELY_CLOSURE_WEIGHT,
            "confirm_closure_weight": CONFIRM_CLOSURE_WEIGHT,
        },
        "ignored_non_official": strategy == "official_only",
    }
    return {
        "strategy": strategy,
        "edge_id": edge_id,
        "direction": direction,
        "valid_from": starts,
        "valid_to": ends,
        "level": level,
        "weight_closure": wc,
        "weight_open": wo,
        "uncertainty": round(uncertainty, 4),
        "has_conflict": conflict,
        "evidence_count": len(window),
        "evidence_refs": refs,
        "rule_trace": trace,
    }


def _level_breakdown(conclusions: list[dict[str, Any]]) -> dict[str, int]:
    out = {"confirmed_closure": 0, "likely_closure": 0, "conflict": 0, "unknown": 0}
    for c in conclusions:
        out[c["level"]] += 1
    return out


# ----------------------------------------------------------- 派生封路 --
def _materialize_derived_closures(conn, run_id: int, strategy: str,
                                  cutoff_at: datetime, conclusion_fp: str) -> list[dict]:
    """每个融合批次的 confirmed 结论独立物化，历史批次行全部保留。

    不做整组撤回：旧截止点的快照只载入“截止点不晚于它的最近批次”，因此后续
    撤回/修正产生的新批次不会改写旧截止点结论，历史分析可精确复现。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM edge_evidence_conclusions WHERE run_id=%s AND level='confirmed_closure'",
            (run_id,),
        )
        confirmed = cur.fetchall()
        created = []
        for c in confirmed:
            derived_id = f"DC-R{run_id}-C{c['conclusion_id']}"
            cur.execute(
                """INSERT INTO derived_road_closures
                       (derived_event_id, conclusion_id, run_id, edge_id, direction,
                        effective_from, effective_to, cutoff_at, evidence_fingerprint, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
                   ON CONFLICT (derived_event_id) DO NOTHING
                   RETURNING derived_event_id""",
                (derived_id, c["conclusion_id"], run_id, c["edge_id"], c["direction"],
                 c["valid_from"], c["valid_to"], cutoff_at, conclusion_fp),
            )
            row = cur.fetchone()
            if row:
                created.append(row["derived_event_id"])
        conn.commit()
    return created


def load_active_derived_closures(conn, cutoff_at: datetime, strategy: str) -> list[dict]:
    """快照载入用：取截止点可见的 *最近融合批次* 的活动派生封路。

    最近批次未再确认某条边，即表示该结论在更新证据下已撤回——旧批次行仍保留，
    但不会进入当前截止点的路网；查询旧截止点时仍会命中旧批次，保证可复现。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.run_id FROM evidence_fusion_runs r
            WHERE r.strategy=%s AND r.cutoff_at <= %s AND r.status='completed'
            ORDER BY r.cutoff_at DESC, r.run_id DESC
            LIMIT 1
            """,
            (strategy, cutoff_at),
        )
        latest = cur.fetchone()
        if latest is None:
            return []
        cur.execute(
            """
            SELECT d.* FROM derived_road_closures d
            WHERE d.status='active' AND d.run_id=%s
            ORDER BY d.edge_id, d.effective_from
            """,
            (latest["run_id"],),
        )
        return cur.fetchall()


# ----------------------------------------------------------- 证据变更影响 --
def register_evidence_impact(conn, evidence_id: str, action: str, *,
                             edges: list[str], windows: list[tuple[datetime, Optional[datetime]]]
                             ) -> dict[str, Any]:
    """沿“边 + 时间窗相交”定位受影响任务；只失效这些情景，不波及其余任务。

    双通道定位见 _tasks_affected_by_edge_windows：实际通行窗与清单结论窗都覆盖，
    被阻断而没走成的任务也能被撤回/修正精准命中。
    """
    edge_set = sorted(set(edges))
    edge_windows: dict[str, list[tuple[datetime, Optional[datetime]]]] = {}
    for e in edge_set:
        edge_windows[e] = windows
    affected_tasks = _tasks_affected_by_edge_windows(conn, edge_windows)
    affected_grids: list[str] = []
    if edge_set:
        if affected_tasks:
            task_list = list(affected_tasks)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE compute_tasks SET stale=TRUE WHERE task_id = ANY(%s)",
                    (task_list,),
                )
                cur.execute(
                    "UPDATE coverage_cache_index SET stale=TRUE WHERE task_id = ANY(%s)",
                    (task_list,),
                )
                conn.commit()
        affected_grids = _grids_affected_by_edges(conn, edge_set, list(affected_tasks))

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO evidence_impact_logs
                   (evidence_id, action, affected_edge_ids, affected_task_ids,
                    affected_grid_ids, detail_json)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING impact_id""",
            (evidence_id, action, jsonb(edge_set), jsonb(sorted(affected_tasks)),
             jsonb(affected_grids),
             jsonb({"windows": [(str(a), str(b) if b else None) for a, b in windows]})),
        )
        impact_id = cur.fetchone()["impact_id"]
        conn.commit()
    return {
        "impact_id": impact_id,
        "affected_edges": edge_set,
        "affected_tasks": sorted(affected_tasks),
        "affected_grids": affected_grids,
    }


def _intersects_any(t_from: datetime, t_to: datetime,
                    windows: list[tuple[datetime, Optional[datetime]]]) -> bool:
    for w_from, w_to in windows:
        if w_to is None:
            if t_to > w_from:
                return True
        elif t_from < w_to and t_to > w_from:
            return True
    return False


# ----------------------------------------------------------- 任务证据清单 --
def build_task_manifest(conn, task_id: str, cutoff_at: datetime,
                        dep_windows: list[dict[str, Any]]) -> dict[str, Any]:
    """计算完成时固化：任务时间窗内的全部结论与相关来源证据明细。

    注意：被证据封死的边不会出现在通行窗依赖里（根本没走成），因此结论收集按
    任务整体时间窗 [最早进入, 最晚离开] 而不是只看已通过的边，确保阻断性证据
    同样进入清单。
    """
    strategy = get_active_strategy(conn)
    horizon_from = min((d["traversed_from"] for d in dep_windows), default=cutoff_at)
    horizon_to = max((d["traversed_to"] for d in dep_windows), default=cutoff_at)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT run_id FROM evidence_fusion_runs
               WHERE strategy=%s AND cutoff_at <= %s
               ORDER BY cutoff_at DESC, run_id DESC LIMIT 1""",
            (strategy, cutoff_at),
        )
        run_row = cur.fetchone()
        manifest: dict[str, Any] = {
            "strategy": strategy,
            "cutoff_at": cutoff_at.isoformat(),
            "rule_version": RULE_VERSION,
            "run_id": run_row["run_id"] if run_row else None,
            "horizon": [horizon_from.isoformat(), horizon_to.isoformat()],
            "conclusions": [],
            "rejected_projections": [],
        }
        if run_row is None:
            cur.execute(
                "INSERT INTO task_evidence_manifests (task_id, run_id, strategy, cutoff_at, "
                "conclusion_fingerprint, manifest_json) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (task_id) DO UPDATE SET manifest_json=EXCLUDED.manifest_json, "
                "run_id=EXCLUDED.run_id, cutoff_at=EXCLUDED.cutoff_at",
                (task_id, manifest["run_id"], strategy, cutoff_at, None, jsonb(manifest)),
            )
            conn.commit()
            return manifest

        cur.execute(
            "SELECT * FROM edge_evidence_conclusions WHERE run_id=%s "
            "ORDER BY edge_id, valid_from",
            (run_row["run_id"],),
        )
        all_conclusions = cur.fetchall()
        kept = [
            c for c in all_conclusions
            if _intersects_any(horizon_from, horizon_to, [(c["valid_from"], c["valid_to"])])
        ]
        manifest["conclusions"] = [
            {
                "conclusion_id": c["conclusion_id"],
                "edge_id": c["edge_id"],
                "direction": c["direction"],
                "level": c["level"],
                "valid_from": c["valid_from"].isoformat(),
                "valid_to": c["valid_to"].isoformat() if c["valid_to"] else None,
                "weight_closure": c["weight_closure"],
                "weight_open": c["weight_open"],
                "uncertainty": c["uncertainty"],
                "has_conflict": c["has_conflict"],
                "evidence_refs": c["evidence_refs"],
            }
            for c in kept
        ]
        related_edges = sorted({*(d["edge_id"] for d in dep_windows),
                                *(c["edge_id"] for c in kept)})
        # 被拒绝投影：分析员可看到“何种证据、为何不够”
        cur.execute(
            """
            SELECT p.edge_id, p.evidence_id, p.evidence_revision, p.direction, p.accepted,
                   p.reject_reason, p.rule_trace, e.source_type, e.source_ref
            FROM evidence_projections p
            JOIN road_evidence e
              ON e.evidence_id=p.evidence_id AND e.revision_no=p.evidence_revision
            WHERE p.edge_id = ANY(%s) AND e.uploaded_at <= %s
            ORDER BY p.edge_id, p.evidence_id
            """,
            (related_edges, cutoff_at),
        )
        manifest["rejected_projections"] = [
            {k: r[k] for k in ("edge_id", "evidence_id", "evidence_revision", "direction",
                               "accepted", "reject_reason", "source_type", "source_ref")}
            for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT conclusion_fingerprint FROM evidence_fusion_runs WHERE run_id=%s",
            (run_row["run_id"],),
        )
        fp_row = cur.fetchone()
        cur.execute(
            "INSERT INTO task_evidence_manifests (task_id, run_id, strategy, cutoff_at, "
            "conclusion_fingerprint, manifest_json) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (task_id) DO UPDATE SET manifest_json=EXCLUDED.manifest_json, "
            "run_id=EXCLUDED.run_id, cutoff_at=EXCLUDED.cutoff_at, "
            "conclusion_fingerprint=EXCLUDED.conclusion_fingerprint",
            (task_id, manifest["run_id"], strategy, cutoff_at,
             fp_row["conclusion_fingerprint"] if fp_row else None, jsonb(manifest)),
        )
        conn.commit()
    return manifest


def grid_evidence_drilldown(conn, task_id: str, grid_id: str) -> dict[str, Any]:
    """从被排除路径下钻：该网格见证边在任务截止点上的结论、投影与原始证据。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT witness_edge_path, reason FROM grid_share_results "
            "WHERE task_id=%s AND grid_id=%s ORDER BY share_index",
            (task_id, grid_id),
        )
        shares = cur.fetchall()
        if not shares:
            return {"grid_id": grid_id, "found": False}
        cur.execute("SELECT road_cutoff FROM scenarios s "
                    "JOIN compute_tasks t ON t.scenario_id=s.scenario_id WHERE t.task_id=%s",
                    (task_id,))
        cutoff = cur.fetchone()["road_cutoff"]
        witness_edges = sorted({e for s in shares for e in (s["witness_edge_path"] or [])})
        # 被阻断网格没有见证边：取其连接节点的出边，才能解释“为何走不出去”
        cur.execute(
            "SELECT DISTINCT r.edge_id FROM grid_connectors gc "
            "JOIN road_edge_revisions r ON r.from_node = gc.node_id "
            "WHERE gc.grid_id=%s AND r.valid_from <= %s "
            "AND (r.valid_to IS NULL OR r.valid_to > %s)",
            (grid_id, cutoff, cutoff),
        )
        incident_edges = [r["edge_id"] for r in cur.fetchall()]
        edge_ids = sorted({*witness_edges, *incident_edges})
        cur.execute(
            """
            SELECT c.* FROM edge_evidence_conclusions c
            JOIN evidence_fusion_runs r ON r.run_id=c.run_id
            WHERE c.edge_id = ANY(%s) AND r.cutoff_at <= %s
            ORDER BY c.edge_id, c.valid_from, r.run_id DESC
            """,
            (edge_ids, cutoff),
        )
        conclusions = cur.fetchall()
        cur.execute(
            """
            SELECT p.*, e.source_type, e.source_ref, e.source_org, e.observed_at,
                   e.uploaded_at, e.location_text, e.raw_text, e.status AS evidence_status
            FROM evidence_projections p
            JOIN road_evidence e
              ON e.evidence_id=p.evidence_id AND e.revision_no=p.evidence_revision
            WHERE p.edge_id = ANY(%s) AND e.uploaded_at <= %s
            ORDER BY p.edge_id, p.accepted, p.evidence_id
            """,
            (edge_ids, cutoff),
        )
        projections = cur.fetchall()
        cur.execute(
            "SELECT * FROM task_evidence_manifests WHERE task_id=%s", (task_id,)
        )
        manifest = cur.fetchone()
        cur.execute(
            "SELECT * FROM evidence_impact_logs WHERE affected_grid_ids ? %s "
            "ORDER BY changed_at DESC",
            (grid_id,),
        )
        impacts = cur.fetchall()
    return {
        "grid_id": grid_id,
        "found": True,
        "cutoff_at": cutoff,
        "witness_edges": witness_edges,
        "incident_edges": incident_edges,
        "share_reasons": [s["reason"] for s in shares if s["reason"]],
        "conclusions": conclusions,
        "projections": projections,
        "manifest_snapshot": manifest,
        "impact_logs": impacts,
    }
