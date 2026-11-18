"""样例数据种子。

小型样例（small）可手工复算：所有边长 1000m、基础速度 60km/h，即每条边 1 分钟。
基准时刻 T0 = 2026-07-15 08:00 UTC。

路网拓扑（含单向边与绕行）：
    N1 → N2 → N3 → N4 → N7        E3(N3→N4) 08:05-08:20 临时封闭，N3 不可等待
          ↓        ↑               E7(N4→N7) 08:00-08:40 封闭，08:40 重开（恢复验证）
          N5 → N6 ─┘               E1(N1→N2) 08:10-08:30 分时限速 20km/h（3 分钟）
    （N1→N2 为单向边，不存在反向边）
安置点：S1@N3（07:00-18:00）、S2@N6（07:00-18:00）、S3@N7（仅 09:00-12:00 开放）
N4 只有 E7 一个出口，故 G8@N4 能否覆盖完全取决于 E7 重开与 S3 开放：
C1 截止点 EV3 为“尚未恢复”的封路 → G8 不可覆盖；C2 修订重开 → G8 恢复覆盖。

情形覆盖：
  - 单向边 E1、绕行 N2→N5→N6→N4、分时限速 EV2、中途封闭 EV1/EV3；
  - G1 单份额可同时到 S2/S3 之外的两个点（多点重叠，只计一次）；
  - G2 双出口互斥份额 0.6/0.4；
  - G3 无连接、G4 比例缺口、G5 比例>1 → 未知口径；
  - G6 两次人口修订 → 共同分母对比中的人口资料变化；
  - G7 属于另一边界 → 分析边界变化；
  - 截止点 C2 录入 E7 重开与新封路 → 缓存失效与恢复重算。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Json

UTC = timezone.utc
T0 = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
C1 = datetime(2026, 7, 15, 7, 30, tzinfo=UTC)   # 第一道路资料截止点
C2 = datetime(2026, 7, 15, 8, 45, tzinfo=UTC)   # 第二截止点（含临时事件修订）


def _dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _add_doc(cur, dataset_type, title, unit_note, occurred, cutoff, revision=1,
             supersedes=None, raw="", org="市防汛办"):
    cur.execute(
        """INSERT INTO source_documents
               (dataset_type, title, source_org, unit_note, source_occurred_at,
                received_at, cutoff_at, revision_no, supersedes_doc_id, raw_text)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING doc_id""",
        (dataset_type, title, org, unit_note, occurred, cutoff - timedelta(minutes=5),
         cutoff, revision, supersedes, raw),
    )
    return cur.fetchone()["doc_id"]


def reseed(conn) -> None:
    """清空并重建全部样例（幂等）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
                grid_share_results, node_arrivals, coverage_summaries,
                coverage_cache_index, compute_task_viewers, task_edge_dependencies,
                task_evidence_dependencies, task_evidence_manifests,
                compute_tasks, publications, scenarios, grid_connectors,
                pop_grid_revisions, pop_grids, shelter_open_windows, shelters,
                road_event_revisions, road_events, road_event_registry,
                road_edge_revisions, road_nodes,
                conclusion_evidence_refs, derived_road_closures,
                edge_evidence_conclusions, evidence_fusion_runs, evidence_impact_logs,
                evidence_projections, road_evidence_audits, road_evidence,
                evidence_fusion_config, evidence_source_types,
                region_boundaries, road_network_versions, source_documents
            RESTART IDENTITY CASCADE
            """
        )
    _seed_boundaries(conn)
    _seed_documents_and_network(conn)
    _seed_shelters(conn)
    _seed_population(conn)
    from .seed_evidence import seed_evidence

    seed_evidence(conn)
    conn.commit()


def _seed_boundaries(conn) -> None:
    doc = None
    with conn.cursor() as cur:
        doc = _add_doc(cur, "boundary", "行政区划边界（2026 版）", "坐标单位：米（地方平面坐标系）",
                       datetime(2026, 1, 1, tzinfo=UTC), C1,
                       raw="POLYGON 边界原文摘录：主城区 B_MAIN 与扩展区 B_EXT")
        cur.execute(
            "INSERT INTO region_boundaries (boundary_id, name, geometry, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,1,%s)",
            ("B_MAIN", "主城区", Json([[-1500, -1200], [4500, -1200], [4500, 2000], [-1500, 2000]]), doc),
        )
        cur.execute(
            "INSERT INTO region_boundaries (boundary_id, name, geometry, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,1,%s)",
            ("B_EXT", "扩展区", Json([[4500, -1200], [6500, -1200], [6500, 2000], [4500, 2000]]), doc),
        )


def _seed_documents_and_network(conn) -> None:
    with conn.cursor() as cur:
        node_doc = _add_doc(cur, "road_node", "路网节点台账", "坐标单位：米",
                            datetime(2026, 6, 1, tzinfo=UTC), C1,
                            raw="节点 N1..N8 原文清单，含是否允许路边等待标记")
        edge_doc = _add_doc(cur, "road_edge", "路网有向边台账", "长度：米；速度：km/h",
                            datetime(2026, 6, 1, tzinfo=UTC), C1,
                            raw="有向边原文：单向通行路段需标注方向，限速为常规晴好天气限速")

        nodes = [
            ("N1", "城北路口", 0, 0, True),
            ("N2", "中心市场", 1000, 0, True),
            ("N3", "河东桥头", 2000, 0, False),
            ("N4", "体育中心", 3000, 0, True),
            ("N5", "西环绕道", 1000, 1000, False),
            ("N6", "南门广场", 2000, 1000, True),
            ("N7", "火车东站", 4000, 0, True),
            ("N8", "孤屯", -1000, 0, False),
        ]
        cur.executemany(
            "INSERT INTO road_nodes (node_id, name, geom_x, geom_y, allow_wait, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(nid, name, x, y, wait, node_doc) for nid, name, x, y, wait in nodes],
        )

        # (edge_id, from, to)；全部 1000m / 60kmh → 1 分钟，边方向即通行方向
        edges = [
            ("E1", "N1", "N2"),   # 单向边：无 E1 反向
            ("E2", "N2", "N3"),
            ("E3", "N3", "N4"),
            ("E4", "N2", "N5"),
            ("E5", "N5", "N6"),
            ("E6", "N6", "N4"),
            ("E7", "N4", "N7"),
            ("E8", "N6", "N3"),
        ]
        cur.executemany(
            """INSERT INTO road_edge_revisions
                   (edge_id, revision_no, from_node, to_node, length_m, base_speed_kmh,
                    valid_from, source_doc_id)
               VALUES (%s,1,%s,%s,1000,60,%s,%s)""",
            [(eid, f, t, datetime(2026, 6, 1, tzinfo=UTC), edge_doc)
             for eid, f, t in edges],
        )

        ev_doc1 = _add_doc(
            cur, "road_event", "暴雨交通管制通告（第 1 号）", "时间：本地时刻(UTC)；速度：km/h",
            T0, C1,
            raw="E3 河东桥 08:05-08:20 封闭；E7 东站引道 08:00 起封闭、恢复时间待定；E1 分时限速 20km/h 08:10-08:30",
        )
        events = [
            # event_id, type, edge, from, to, speed
            ("EV1", "closure", "E3", _dt(5), _dt(20), None),
            ("EV2", "speed_limit", "E1", _dt(10), _dt(30), 20.0),
            # C1 时 E7 封路尚未恢复（effective_to 为空）；C2 以 *新修订行* 记录 08:40 重开
            ("EV3", "closure", "E7", _dt(0), None, None),
        ]
        cur.executemany(
            "INSERT INTO road_event_registry (event_id, edge_id, event_type) VALUES (%s,%s,%s)",
            [(eid, edge, typ) for eid, typ, edge, ef, et, spd in events],
        )
        cur.executemany(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, supersedes_event_id, status, source_doc_id, received_at)
               VALUES (%s,1,%s,%s,%s,%s,%s,NULL,'active',%s,%s)""",
            [(eid, typ, edge, ef, et, spd, ev_doc1, C1 - timedelta(minutes=10))
             for eid, typ, edge, ef, et, spd in events],
        )
        cur.executemany(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES (%s,1,%s,%s,%s)""",
            [
                ("EV1", "首发：河东桥临时封闭", Json({"effective_from": "08:05", "effective_to": "08:20"}), ev_doc1),
                ("EV2", "首发：分时限速 20km/h", Json({"speed_limit_kmh": 20}), ev_doc1),
                ("EV3", "首发：东站引道封闭（恢复时间待定）", Json({"effective_from": "08:00", "effective_to": None}), ev_doc1),
            ],
        )


def _seed_shelters(conn) -> None:
    with conn.cursor() as cur:
        doc = _add_doc(cur, "shelter", "既有应急安置点名录与开放安排", "容量单位：人（本平台不做分配）",
                       datetime(2026, 7, 1, tzinfo=UTC), C1,
                       raw="S1 河东桥旁社区馆（N3）、S2 南门学校（N6）、S3 东站展厅（N7，仅上午开放）")
        shelters = [
            ("S1", "河东桥社区安置点", "N3", 2000, 0),
            ("S2", "南门学校安置点", "N6", 2000, 1000),
            ("S3", "东站展厅安置点", "N7", 4000, 0),
        ]
        cur.executemany(
            "INSERT INTO shelters (shelter_id, name, node_id, geom_x, geom_y, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,1,%s)",
            [(sid, name, node, x, y, doc) for sid, name, node, x, y in shelters],
        )
        windows = [
            ("S1", datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 7, 15, 18, tzinfo=UTC)),
            ("S2", datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 7, 15, 18, tzinfo=UTC)),
            ("S3", datetime(2026, 7, 15, 9, tzinfo=UTC), datetime(2026, 7, 15, 12, tzinfo=UTC)),
        ]
        cur.executemany(
            "INSERT INTO shelter_open_windows (shelter_id, open_from, open_to, revision_no) "
            "VALUES (%s,%s,%s,1)",
            windows,
        )


def _seed_population(conn) -> None:
    with conn.cursor() as cur:
        doc_v1 = _add_doc(
            cur, "population", "规则网格常住人口（6 月基准）", "单位：人；网格 500m×500m",
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 30, tzinfo=UTC), revision=1,
            raw="规则网格人口原文表 v1：G1..G7",
        )
        doc_v2 = _add_doc(
            cur, "population", "规则网格常住人口（7 月修订）", "单位：人；网格 500m×500m",
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 14, tzinfo=UTC), revision=2, supersedes=doc_v1,
            raw="修订 v2：G6 新交付小区入住，人口 1000 → 1200",
        )
        conn_doc = _add_doc(
            cur, "population", "网格—路网连接与人口分配比例调查", "比例：无量纲，互斥子份额",
            datetime(2026, 6, 20, tzinfo=UTC), C1,
            raw="多出口网格按实测出口选择比例切分，份额互斥；比例之和不足 1 的缺口记为分配依据缺失",
        )

        # grid_id, name, x, y, boundary, pop_v1, pop_v2(None=不修订)
        grids = [
            ("G1", "网格 G1（南门重叠片）", 2000, 800, "B_MAIN", 1000, None),
            ("G2", "网格 G2（双出口片）", 1000, 500, "B_MAIN", 500, None),
            ("G3", "网格 G3（无连接片）", -1000, 800, "B_MAIN", 300, None),
            ("G4", "网格 G4（分配缺口片）", 200, 200, "B_MAIN", 800, None),
            ("G5", "网格 G5（比例矛盾片）", 300, -300, "B_MAIN", 400, None),
            ("G6", "网格 G6（人口修订片）", 200, -100, "B_MAIN", 1000, 1200),
            ("G7", "网格 G7（扩展区片）", 5200, 500, "B_EXT", 600, None),
            ("G8", "网格 G8（东站片，待恢复）", 3200, 200, "B_MAIN", 700, None),
        ]
        cur.executemany(
            "INSERT INTO pop_grids (grid_id, name, geom_x, geom_y, boundary_id, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(gid, name, x, y, b, doc_v1) for gid, name, x, y, b, _, _ in grids],
        )
        rev_rows = []
        for gid, _, _, _, _, p1, p2 in grids:
            rev_rows.append((gid, 1, p1, date(2026, 6, 1), doc_v1))
            if p2 is not None:
                rev_rows.append((gid, 2, p2, date(2026, 7, 1), doc_v2))
        cur.executemany(
            """INSERT INTO pop_grid_revisions (grid_id, revision_no, population, as_of_date, source_doc_id)
               VALUES (%s,%s,%s,%s,%s)""",
            rev_rows,
        )

        # 连接与互斥分配比例
        connectors = [
            ("G1", "N6", 1.0),                       # 单出口；N6 即 S2，且可到 S1 → 多点重叠
            ("G2", "N2", 0.6), ("G2", "N5", 0.4),    # 双出口互斥份额 300/200
            # G3 无连接 → unknown_no_connector
            ("G4", "N1", 0.7),                       # 缺口 0.3 → 240 人 unknown_allocation_gap
            ("G5", "N1", 0.6), ("G5", "N2", 0.6),    # 和 1.2 > 1 → 整格 unknown_ratio_over_one
            ("G6", "N1", 1.0),
            ("G7", "N5", 1.0),
            ("G8", "N4", 1.0),                       # 仅 E7 一条出口，恢复前不可覆盖
        ]
        cur.executemany(
            """INSERT INTO grid_connectors (grid_id, node_id, allocation_ratio, revision_no, source_doc_id)
               VALUES (%s,%s,%s,1,%s)""",
            [(g, n, r, conn_doc) for g, n, r in connectors],
        )


# ----------------------------------------------------------- 较大样例 --
def apply_c2_revision(conn) -> dict:
    """第二道路资料截止点 C2 的修订：E7 恢复重开 + E2 新增临时封闭。

    用于验证：
      - 此前不可达的 G8 因重开恢复覆盖（恢复候选机制）；
      - 新封路立即参与即时计算，并沿依赖边定位受影响任务；
      - 同截止点迟到修订产生新路网版本，旧任务不会覆盖新版本索引。
    """
    with conn.cursor() as cur:
        doc = _add_doc(
            cur, "road_event", "暴雨交通管制通告（第 2 号·修订）",
            "时间：本地时刻(UTC)；速度：km/h",
            C2, C2, revision=2,
            raw="E7 东站引道 08:40 恢复通行；E2 中心市场至河东桥 08:15-08:35 临时封闭",
        )
        # EV3 追加第 2 修订行；第 1 行（未恢复封路）保留，C1 截止点分析仍可精确复现
        cur.execute(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, supersedes_event_id, status, source_doc_id, received_at)
               VALUES ('EV3',2,'closure','E7',%s,%s,NULL,'EV3','active',%s,%s)""",
            (_dt(0), _dt(40), doc, C2 - timedelta(minutes=2)),
        )
        cur.execute(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES ('EV3',2,%s,%s,%s)""",
            ("恢复修订：明确 08:40 重开",
             Json({"effective_to": "08:40", "previous_effective_to": None}), doc),
        )
        cur.execute(
            "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
            "VALUES ('EV4','E2','closure')"
        )
        cur.execute(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, status, source_doc_id, received_at)
               VALUES ('EV4',1,'closure','E2',%s,%s,NULL,'active',%s,%s)""",
            (_dt(15), _dt(35), doc, C2 - timedelta(minutes=2)),
        )
        cur.execute(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES ('EV4',1,%s,%s,%s)""",
            ("首发：E2 临时封闭 08:15-08:35",
             Json({"edge_id": "E2", "effective_from": "08:15", "effective_to": "08:35"}), doc),
        )
        conn.commit()

    # 修订涉及 E7（重开）与 E2（新封路）：沿依赖边失效，并筛查可能因重开恢复的区域
    from .services.compute import manager

    inv_e7 = manager.invalidate_for_edge(conn, "E7", "C2 修订：E7 恢复重开")
    inv_e2 = manager.invalidate_for_edge(conn, "E2", "C2 修订：E2 新增临时封闭")
    return {
        "cutoff_at": C2,
        "changes": ["EV3 重开 08:40", "EV4 新封路 E2 08:15-08:35"],
        "invalidation": {"E7": inv_e7, "E2": inv_e2},
    }


def seed_large(conn) -> None:
    """在小型样例之外追加 8×8 规则路网与 40 个网格（固定模式，无随机数）。"""
    with conn.cursor() as cur:
        doc = _add_doc(cur, "road_node", "区级骨干路网节点台账（大区样例）", "坐标单位：米",
                       datetime(2026, 5, 1, tzinfo=UTC), C1,
                       raw="8×8 规则网格路网，节点 Lxy；奇数行设为单向交通组织")
        for i in range(8):
            for j in range(8):
                nid = f"L{i}{j}"
                cur.execute(
                    "INSERT INTO road_nodes (node_id, name, geom_x, geom_y, allow_wait, source_doc_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (node_id) DO NOTHING",
                    (nid, f"大区节点 {i},{j}", 10000 + j * 800, 10000 + i * 800,
                     (i + j) % 2 == 0, doc),
                )
        edge_rows = []
        eid = 0
        for i in range(8):
            for j in range(8):
                # 横向：奇数行只允许向东（单向边演示）；偶数行双向
                if j < 7:
                    edge_rows.append((f"LE{eid}", f"L{i}{j}", f"L{i}{j+1}")); eid += 1
                    if i % 2 == 0:
                        edge_rows.append((f"LE{eid}", f"L{i}{j+1}", f"L{i}{j}")); eid += 1
                # 纵向双向
                if i < 7:
                    edge_rows.append((f"LE{eid}", f"L{i}{j}", f"L{i+1}{j}")); eid += 1
                    edge_rows.append((f"LE{eid}", f"L{i+1}{j}", f"L{i}{j}")); eid += 1
        cur.executemany(
            """INSERT INTO road_edge_revisions
                   (edge_id, revision_no, from_node, to_node, length_m, base_speed_kmh,
                    valid_from, source_doc_id)
               VALUES (%s,1,%s,%s,800,40,%s,%s)
               ON CONFLICT (edge_id, revision_no) DO NOTHING""",
            [(e, f, t, datetime(2026, 5, 1, tzinfo=UTC), doc) for e, f, t in edge_rows],
        )

        # 固定模式事件：每两行一条分时限速、一条中段封闭
        for i in range(0, 8, 2):
            edge = next(e for e, f, t in edge_rows if f == f"L{i}3" and t == f"L{i}4")
            eid = f"LEV-S{i}"
            cur.execute(
                "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
                "VALUES (%s,%s,'speed_limit') ON CONFLICT (event_id) DO NOTHING",
                (eid, edge),
            )
            cur.execute(
                """INSERT INTO road_events
                       (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                        speed_limit_kmh, status, source_doc_id, received_at)
                   VALUES (%s,1,'speed_limit',%s,%s,%s,15,'active',%s,%s)
                   ON CONFLICT (event_id, revision_no) DO NOTHING""",
                (eid, edge, _dt(0), _dt(90), doc, C1 - timedelta(minutes=10)),
            )
        for i in (1, 5):
            edge = next(e for e, f, t in edge_rows if f == f"L{i}5" and t == f"L{i}6")
            eid = f"LEV-C{i}"
            cur.execute(
                "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
                "VALUES (%s,%s,'closure') ON CONFLICT (event_id) DO NOTHING",
                (eid, edge),
            )
            cur.execute(
                """INSERT INTO road_events
                       (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                        speed_limit_kmh, status, source_doc_id, received_at)
                   VALUES (%s,1,'closure',%s,%s,%s,NULL,'active',%s,%s)
                   ON CONFLICT (event_id, revision_no) DO NOTHING""",
                (eid, edge, _dt(10), _dt(70), doc, C1 - timedelta(minutes=10)),
            )

        # 四个角安置点
        for k, (i, j) in enumerate(((0, 0), (0, 7), (7, 0), (7, 7))):
            sid = f"SL{k}"
            cur.execute(
                "INSERT INTO shelters (shelter_id, name, node_id, geom_x, geom_y, source_doc_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (shelter_id) DO NOTHING",
                (sid, f"大区安置点 {k}", f"L{i}{j}", 10000 + j * 800, 10000 + i * 800, doc),
            )
            cur.execute(
                "INSERT INTO shelter_open_windows (shelter_id, open_from, open_to) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (sid, datetime(2026, 7, 15, 6, tzinfo=UTC), datetime(2026, 7, 15, 20, tzinfo=UTC)),
            )

        # 40 个网格，连接到确定性的节点对；第 40 个刻意无连接 → 未知
        for k in range(40):
            gid = f"GL{k:02d}"
            i, j = divmod(k, 8)
            cur.execute(
                "INSERT INTO pop_grids (grid_id, name, geom_x, geom_y, boundary_id, source_doc_id) "
                "VALUES (%s,%s,%s,%s,'B_MAIN',%s) ON CONFLICT (grid_id) DO NOTHING",
                (gid, f"大区网格 {k}", 10000 + j * 800, 9700 + i * 800, doc),
            )
            cur.execute(
                "INSERT INTO pop_grid_revisions (grid_id, revision_no, population, as_of_date, source_doc_id) "
                "VALUES (%s,1,%s,%s,%s) ON CONFLICT DO NOTHING",
                (gid, 500 + (k * 37) % 900, date(2026, 6, 1), doc),
            )
            if k != 39:
                cur.execute(
                    "INSERT INTO grid_connectors (grid_id, node_id, allocation_ratio, source_doc_id) "
                    "VALUES (%s,%s,1.0,%s) ON CONFLICT DO NOTHING",
                    (gid, f"L{i}{j}", doc),
                )


if __name__ == "__main__":
    from .db import get_conn, init_db

    init_db()
    with get_conn() as connection:
        reseed(connection)
        seed_large(connection)
    print("seed completed: small hand-computable network + large 8x8 sample + west evidence sample")
