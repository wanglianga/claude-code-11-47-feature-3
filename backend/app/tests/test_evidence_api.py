"""证据融合 HTTP 端到端：接入、投影、策略切换、融合、下钻、撤回与清单。"""
from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.seed import T0
from app.seed_evidence import C3, C_EV

client = TestClient(app)


def _iso(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def test_evidence_catalog_strategy_and_fusion_flow(fresh_db):
    # 来源类型目录与默认策略
    types = client.get("/evidence").json()
    assert len(types["evidence"]) >= 8
    strategy = client.get("/evidence/strategy").json()
    assert strategy["active_strategy"] == "multi_source"

    # 投影列表：平行歧义证据被拒绝且原因可查
    projs = client.get("/evidence/projections/list", params={"accepted": False}).json()
    rejected = [p for p in projs["projections"] if p["evidence_id"] == "EV-S1"]
    assert rejected and all(
        "parallel_road_ambiguity" in (p["reject_reason"] or "")
        or "spatial_weight_below_threshold" in (p["reject_reason"] or "")
        for p in rejected
    )

    # 执行融合并核对分级
    run = client.post("/evidence/fusion/run", json={
        "cutoff_at": C_EV.isoformat(),
    }).json()
    assert run["level_breakdown"]["confirmed_closure"] >= 2
    assert run["level_breakdown"]["conflict"] >= 1
    runs = client.get("/evidence/runs/list").json()["runs"]
    assert any(r["run_id"] == run["run_id"] for r in runs)
    derived = client.get("/evidence/derived-closures/list").json()["derived_closures"]
    assert any(d["status"] == "active" for d in derived)


def test_evidence_ingest_revise_withdraw_and_impact(fresh_db):
    payload = {
        "evidence_id": "EV-HTTP-1",
        "source_type": "water_sensor",
        "source_ref": "WS-HTTP",
        "geom_x": -5500, "geom_y": 0, "location_error_m": 50,
        "observed_at": _iso(5), "uploaded_at": _iso(9),
        "threshold_value": 27, "observed_value": 40,
        "raw_text": "HTTP 接入：传感器 40cm 超阈值",
    }
    out = client.post("/evidence", json=payload).json()
    assert out["revision_no"] == 1 and out["fingerprint"].startswith("evfp_")
    detail = client.get("/evidence/EV-HTTP-1").json()
    assert any(p["accepted"] for p in detail["projections"])

    # 重复接入同 ID 必须被拒绝（修订只能走 revise）
    dup = client.post("/evidence", json=payload)
    assert dup.status_code == 409

    # 修正为未超阈值并给出恢复时刻
    revised = {**payload, "uploaded_at": _iso(60), "observed_value": 8,
               "observed_recovery_at": _iso(20),
               "raw_text": "HTTP 复核：实测 8cm，已退水", "change_note": "撤销封路"}
    rev = client.post("/evidence/EV-HTTP-1/revise", json=revised).json()
    assert rev["revision_no"] == 2 and rev["action"] == "revise"

    # 撤回接口
    wd = client.post("/evidence/EV-HTTP-1/withdraw", params={"change_note": "临时撤回"}).json()
    assert wd["status"] == "withdrawn"
    audits = client.get("/evidence/EV-HTTP-1").json()["audits"]
    assert {a["action"] for a in audits} >= {"ingest", "revise", "withdraw"}

    # 影响日志可查
    logs = client.get("/evidence/impact-logs/list",
                      params={"evidence_id": "EV-HTTP-1"}).json()["impact_logs"]
    assert logs and all("affected_edge_ids" in x for x in logs)


def test_strategy_switch_and_grid_drilldown(fresh_db):
    client.post("/evidence/fusion/run", json={"cutoff_at": C_EV.isoformat()})
    # 建一个城西片情景（08:10 出发，45 分钟时限）
    scenario = client.post("/scenarios", json={
        "name": "HTTP 证据情景",
        "population_as_of": "2026-06-01",
        "departure_time": _iso(10),
        "max_trip_minutes": 45,
        "road_cutoff": C_EV.isoformat(),
        "boundary_id": "B_WEST",
        "viewer_id": "http-ev",
    }).json()
    task_id = scenario["task_id"]

    # 任务证据清单已固化
    manifest = client.get(f"/evidence/tasks/{task_id}/manifest").json()
    assert manifest["manifest_json"]["strategy"] == "multi_source"

    # GA 下钻：看到 WA 的 confirmed 结论与拒绝投影解释
    dd = client.get(f"/evidence/tasks/{task_id}/grids/GA/drilldown").json()
    assert dd["found"] and "WA" in dd["incident_edges"]
    assert any(c["level"] == "confirmed_closure" for c in dd["conclusions"])

    # 切到官方-only：缓存全部失效，且融合不再产生群众驱动的冲突
    switched = client.post("/evidence/strategy", json={"strategy": "official_only"}).json()
    assert switched["active_strategy"] == "official_only"
    run = client.post("/evidence/fusion/run", json={
        "strategy": "official_only", "cutoff_at": C3.isoformat(),
    }).json()
    wf = [c for c in client.get("/evidence/conclusions/list",
                                params={"edge_id": "WF"}).json()["conclusions"]
          if c["run_id"] == run["run_id"]]
    assert all(c["level"] != "conflict" for c in wf)
    client.post("/evidence/strategy", json={"strategy": "multi_source"})


def test_conclusions_and_projections_filtering(fresh_db):
    client.post("/evidence/fusion/run", json={"cutoff_at": C_EV.isoformat()})
    conflicts = client.get("/evidence/conclusions/list",
                           params={"level": "conflict"}).json()["conclusions"]
    assert conflicts and all(c["level"] == "conflict" for c in conflicts)
    accepted = client.get("/evidence/projections/list",
                          params={"accepted": True}).json()["projections"]
    assert accepted and all(p["accepted"] for p in accepted)
