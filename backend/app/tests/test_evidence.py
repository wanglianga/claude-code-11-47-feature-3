"""证据融合与冲突解释测试。

覆盖六类验收样例：平行道路误匹配、方向性封闭、晚传照片、相反上报、传感器离线、
修订证据；并验证证据分级、受影响范围（边+时间窗精准失效）与历史复现。
"""
from datetime import date, timedelta

from app.engine import solve
from app.seed import T0
from app.seed_evidence import C3, C_EV
from app.services.compute import manager
from app.services.evidence import (
    grid_evidence_drilldown,
    run_fusion,
    set_active_strategy,
)
from app.services.snapshot import get_or_create_network_version, load_snapshot

AS_OF = date(2026, 6, 1)


def _run_west(db, *, departure=T0 + timedelta(minutes=10), minutes=45, cutoff=C_EV,
              viewer="ev-test", name="证据情景"):
    return manager.get_or_create(
        db, name=name, population_as_of=AS_OF, departure_time=departure,
        max_trip_minutes=minutes, road_cutoff=cutoff, viewer_id=viewer,
        boundary_id="B_WEST",
    )


def _conclusions(db, run_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT edge_id, direction, level, weight_closure, weight_open, "
            "uncertainty, has_conflict "
            "FROM edge_evidence_conclusions WHERE run_id=%s",
            (run_id,),
        )
        return {r["edge_id"]: r for r in cur.fetchall()}


def _projection(db, evidence_id, edge_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM evidence_projections WHERE evidence_id=%s AND edge_id=%s "
            "ORDER BY evidence_revision DESC LIMIT 1",
            (evidence_id, edge_id),
        )
        return cur.fetchone()


# ------------------------------------------------ 1. 平行道路误匹配 --
def test_parallel_road_sensor_rejected_not_silently_assigned(fresh_db):
    db = fresh_db
    run = run_fusion(db, "multi_source", C_EV)
    # 模糊带内候选（WB/WP2/WP1）全部拒绝，且没有任何边因该传感器得到确定封路
    with db.cursor() as cur:
        cur.execute(
            "SELECT edge_id, accepted, reject_reason FROM evidence_projections "
            "WHERE evidence_id='EV-S1' AND edge_id IN ('WB','WP2','WP1')"
        )
        rows = cur.fetchall()
    assert rows and all(r["accepted"] is False for r in rows)
    assert all("parallel_road_ambiguity" in r["reject_reason"] for r in rows)
    levels = _conclusions(db, run["run_id"])
    assert "WB" not in levels or levels["WB"]["level"] != "confirmed_closure"
    assert "WP2" not in levels
    # 拒绝原因在规则轨迹中可解释
    p = _projection(db, "EV-S1", "WB")
    assert p["rule_trace"]["ambiguous_with"]  # 明确记录与哪些边歧义


# ------------------------------------------------ 2. 方向性封闭 --
def test_directional_closure_blocks_only_reverse_lane(fresh_db):
    db = fresh_db
    run = run_fusion(db, "multi_source", C_EV)
    levels = _conclusions(db, run["run_id"])
    # 西向证据规范化到对向有向边 WC-R 的正向并确认封闭
    assert levels["WC-R"]["level"] == "confirmed_closure"
    # 东向 WC 没有任何封路结论
    assert "WC" not in levels

    version = get_or_create_network_version(db, C_EV)
    snap = load_snapshot(db, version["version_id"])
    # 封路窗 08:20-08:50 内出发：
    # 东向 W7→W8（WC）自由流；西向 W8→W7（WC-R）被阻断，只能在 W8 等到 08:50 重开
    east = solve(snap, "W7", T0 + timedelta(minutes=25), T0 + timedelta(minutes=55))
    west = solve(snap, "W8", T0 + timedelta(minutes=25), T0 + timedelta(minutes=55))
    assert "W8" in east
    assert east["W8"].arrival_time < T0 + timedelta(minutes=27)  # 一分钟内直达，无等待
    assert west["W7"].arrival_time >= T0 + timedelta(minutes=50)  # 必须等过封路窗
    assert any(w.reason == "closure" for w in west["W7"].waits)
    # 引擎事件必须带来源（derived），不存在无来源的确定状态
    wcr = next(e for e in snap.edges if e.edge_id == "WC-R")
    assert all(ev.origin == "derived" for ev in wcr.events)


# ------------------------------------------------ 3. 晚传照片 --
def test_late_upload_photo_discounted_and_invisible_before_upload(fresh_db):
    db = fresh_db
    early = run_fusion(db, "multi_source", C_EV)
    early_levels = _conclusions(db, early["run_id"])
    # 08:45 截止点：11:15 才上传的照片尚不可见，WD 无结论
    assert "WD" not in early_levels
    p = _projection(db, "EV-S3", "WD")
    assert p["time_weight"] < 0.5  # 晚传 185 分钟，权重大幅折减
    late = run_fusion(db, "multi_source", C3)
    late_levels = _conclusions(db, late["run_id"])
    # 晚传折减后只能到 likely，绝不能升级为确定封路
    assert late_levels["WD"]["level"] == "likely_closure"


# ------------------------------------------------ 4. 相反上报（禁止多数表决）--
def test_conflicting_reports_weighted_not_majority_voted(fresh_db):
    db = fresh_db
    run = run_fusion(db, "multi_source", C_EV)
    wf = _conclusions(db, run["run_id"])["WF"]
    assert wf["level"] == "conflict" and wf["has_conflict"] is True
    assert wf["weight_closure"] > 0 and wf["weight_open"] > 0
    # 两条群众 vs 一条官方：按可靠性×误差加权，官方开放侧仍有可观权重 → 冲突而非封路
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM derived_road_closures d "
                    "JOIN edge_evidence_conclusions c ON c.conclusion_id=d.conclusion_id "
                    "WHERE c.edge_id='WF'")
        assert cur.fetchone()["n"] == 0


# ------------------------------------------------ 5. 传感器离线 --
def test_offline_sensor_has_no_stance_edge_stays_unknown(fresh_db):
    db = fresh_db
    p = _projection(db, "EV-S5", "WE")
    assert p["accepted"] is False
    assert "sensor_offline_no_reading" in p["reject_reason"]
    run = run_fusion(db, "multi_source", C_EV)
    levels = _conclusions(db, run["run_id"])
    assert "WE" not in levels  # 无证据建立立场 → 保持未知，不生成确定状态


# ------------------------------------------------ 6. 修订证据 + 定向失效 + 复现 --
def test_revision_retracts_closure_and_only_related_scenarios_stale(fresh_db):
    db = fresh_db
    run_fusion(db, "multi_source", C_EV)
    target = _run_west(db, viewer="rev-a", name="修订目标情景")
    # 无关情景：限定主城区边界，不评估城西网格，不引用城西任何边
    unrelated = manager.get_or_create(
        db, name="无关情景", population_as_of=AS_OF, departure_time=T0,
        max_trip_minutes=30, road_cutoff=C_EV, viewer_id="rev-b",
        boundary_id="B_MAIN",
    )
    with db.cursor() as cur:
        cur.execute("SELECT stale FROM compute_tasks WHERE task_id=%s", (target["task_id"],))
        assert cur.fetchone()["stale"] is False

    run2 = run_fusion(db, "multi_source", C3)  # EV-S6 r2 修正 + 晚传可见
    levels = _conclusions(db, run2["run_id"])
    assert levels["WA"]["level"] == "unknown"  # 封路结论被撤回
    with db.cursor() as cur:
        cur.execute("SELECT stale FROM compute_tasks WHERE task_id=%s", (target["task_id"],))
        assert cur.fetchone()["stale"] is True
        cur.execute("SELECT stale FROM compute_tasks WHERE task_id=%s", (unrelated["task_id"],))
        assert cur.fetchone()["stale"] is False  # 不引用 WA 的任务不被波及

    # 旧截止点快照仍复现 r1 的 WA 封路；新截止点已无 WA 派生事件
    old_snap = load_snapshot(db, get_or_create_network_version(db, C_EV)["version_id"])
    new_snap = load_snapshot(db, get_or_create_network_version(db, C3)["version_id"])
    assert any(ev.origin == "derived" for ev in next(
        e for e in old_snap.edges if e.edge_id == "WA").events)
    assert next(e for e in new_snap.edges if e.edge_id == "WA").events == ()

    # 既有任务的证据清单保留当时的 confirmed 结论，不被后续修订改写
    with db.cursor() as cur:
        cur.execute(
            "SELECT manifest_json FROM task_evidence_manifests WHERE task_id=%s",
            (target["task_id"],),
        )
        manifest = cur.fetchone()["manifest_json"]
    assert any(c["edge_id"] == "WA" and c["level"] == "confirmed_closure"
               for c in manifest["conclusions"])


# ------------------------------------------------ 7. 官方-only 策略 --
def test_official_only_strategy_excludes_crowd_and_covers_less(fresh_db):
    db = fresh_db
    set_active_strategy(db, "official_only")
    run = run_fusion(db, "official_only", C3)
    levels = _conclusions(db, run["run_id"])
    # 群众上报被排除后，WF 不再有冲突结论（只剩官方开放观测或证据不足）
    assert levels.get("WF", {}).get("level") in (None, "unknown")
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM edge_evidence_conclusions c "
            "JOIN jsonb_array_elements(c.evidence_refs) ref ON TRUE "
            "WHERE c.run_id=%s AND ref->>'official'='false'",
            (run["run_id"],),
        )
        assert cur.fetchone()["n"] == 0
    set_active_strategy(db, "multi_source")  # 还原，避免影响其他用例


# ------------------------------------------------ 8. 下钻：被排除路径看到证据 --
def test_grid_drilldown_explains_why_path_excluded(fresh_db):
    db = fresh_db
    run_fusion(db, "multi_source", C_EV)
    task = _run_west(db, viewer="drill", name="下钻情景")
    ga = grid_evidence_drilldown(db, task["task_id"], "GA")
    assert ga["found"] and ga["incident_edges"] == ["WA"]
    assert any(c["level"] == "confirmed_closure" for c in ga["conclusions"])
    # 改变的是哪个网格、何种证据、为何不够，都能追溯
    with db.cursor() as cur:
        cur.execute("SELECT * FROM evidence_impact_logs WHERE affected_grid_ids ? 'GA' "
                    "ORDER BY impact_id DESC LIMIT 1")
        assert cur.fetchone() is not None


# ------------------------------------------------ 8b. 临时撤回（无时间窗也命中）--
def test_withdraw_invalidates_tasks_even_without_time_window(fresh_db):
    from app.services.evidence import ingest_evidence

    db = fresh_db
    run_fusion(db, "multi_source", C_EV)
    task = _run_west(db, viewer="wd-a", name="撤回目标")
    with db.cursor() as cur:
        cur.execute("SELECT stale FROM compute_tasks WHERE task_id=%s", (task["task_id"],))
        assert cur.fetchone()["stale"] is False
    # 对已被确认封闭的 WA 来源 EV-S6 做临时撤回（不带时间窗）
    out = ingest_evidence(
        db,
        {
            "evidence_id": "EV-S6", "source_type": "water_sensor", "source_ref": "WS-A",
            "geom_x": -5500, "geom_y": 0, "location_error_m": 50,
            "observed_at": T0 + timedelta(minutes=5),
            "uploaded_at": T0 + timedelta(minutes=80),
            "threshold_value": 27, "observed_value": 12,
            "observed_recovery_at": T0 + timedelta(minutes=25),
            "raw_text": "临时撤回复核", "change_note": "撤回",
        },
        action="withdraw", evidence_id="EV-S6",
    )
    assert out["status"] == "withdrawn"
    # 撤回无时间窗约束：引用 WA 的城西任务必须被失效，且影响网格登记为 GA
    assert str(task["task_id"]) in out["impact"]["affected_tasks"]
    assert "GA" in out["impact"]["affected_grids"]
    with db.cursor() as cur:
        cur.execute("SELECT stale FROM compute_tasks WHERE task_id=%s", (task["task_id"],))
        assert cur.fetchone()["stale"] is True


# ------------------------------------------------ 9. 指纹与追加式修订 --
def test_evidence_fingerprints_and_append_only_revisions(fresh_db):
    db = fresh_db
    with db.cursor() as cur:
        cur.execute(
            "SELECT revision_no, status, evidence_fingerprint FROM road_evidence "
            "WHERE evidence_id='EV-S6' ORDER BY revision_no"
        )
        rows = cur.fetchall()
    assert [r["revision_no"] for r in rows] == [1, 2]
    assert len({r["evidence_fingerprint"] for r in rows}) == 2
    # r1 行原样保留，可被旧截止点查询读取
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM road_evidence WHERE evidence_id='EV-S6' "
            "AND revision_no=1"
        )
        assert cur.fetchone()["n"] == 1
