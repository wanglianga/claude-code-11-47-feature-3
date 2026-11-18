"""证据融合样例：独立“城西积水片”路网，不改动主任务 E1..E8 的基线状态。

六类验收情形（T0=2026-07-15 08:00 UTC）：
  1. 平行道路误匹配：传感器 WS-PARALLEL 位于 WB 与平行支路 WP2 正中，定位误差
     包络同时覆盖多条边 → 全部投影拒绝（parallel_road_ambiguity），不得封闭 WB。
  2. 方向性封闭：巡查照片 IP-DIR 明确西向（270°），投影规范化到对向有向边 WC-R，
     东向 WC 仍可通行。
  3. 晚传照片：IP-LATE 拍摄 08:10、上传 11:15，晚传折减后只达 likely_closure，
     且在 08:45 截止点尚不可见，不产生确定封路。
  4. 相反上报：WF 上两条群众封路上报 + 一条官方传感器“未超阈值”，按权重而非数量
     判定为 conflict，不生成无来源的确定状态。
  5. 传感器离线：WS-OFF 无读数，投影拒绝（sensor_offline_no_reading），WE 保持未知。
  6. 修订证据：WS-A 首发封路（r1），09:10 复核修正为误报并给出恢复时刻（r2）；
     旧截止点仍复现 r1 封路，新截止点撤回派生封路并定向失效受影响任务。

融合不自动执行：种子只写证据与投影，分析员显式 POST /evidence/fusion/run。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from .seed import C1, T0, _add_doc
from .services import evidence as ev_service

UTC = timezone.utc
C_EV = T0 + timedelta(minutes=45)    # 证据融合第一截止点 08:45
C3 = T0 + timedelta(minutes=210)     # 晚传/修订可见后的截止点 11:30

BOUNDARY_WEST = "B_WEST"


def _dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def seed_evidence(conn) -> None:
    _seed_catalog_and_config(conn)
    _seed_west_network(conn)
    _seed_evidence_rows(conn)


def _seed_catalog_and_config(conn) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO evidence_source_types (source_type, type_name, base_reliability, official, note) "
            "VALUES (%s,%s,%s,%s,%s)",
            [
                ("water_sensor", "积水传感器", 0.90, True, "官方设备，阈值化读数；离线时无立场"),
                ("inspection_photo", "巡查照片标注", 0.85, True, "官方巡查，含拍摄/上传双时间"),
                ("crowd_report", "群众上报", 0.55, False, "仅描述附近地名时方向/立场可能缺失"),
            ],
        )
        cur.execute(
            "INSERT INTO evidence_fusion_config (single_row, strategy) VALUES ('X','multi_source') "
            "ON CONFLICT DO NOTHING"
        )
        conn.commit()


def _seed_west_network(conn) -> None:
    with conn.cursor() as cur:
        bdoc = _add_doc(
            cur, "boundary", "城西积水片边界", "坐标单位：米（地方平面坐标系）",
            datetime(2026, 6, 1, tzinfo=UTC), C1,
            raw="城西防汛责任区 B_WEST，与主城区 B_MAIN 不重叠",
        )
        cur.execute(
            "INSERT INTO region_boundaries (boundary_id, name, geometry, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,1,%s)",
            (BOUNDARY_WEST, "城西积水片",
             Json([[-6600, -1300], [-400, -1300], [-400, 1300], [-6600, 1300]]), bdoc),
        )
        ndoc = _add_doc(
            cur, "road_node", "城西积水片路网节点台账", "坐标单位：米",
            datetime(2026, 6, 1, tzinfo=UTC), C1,
            raw="城西片节点 W1..W15，含平行支路与对向有向边",
        )
        edoc = _add_doc(
            cur, "road_edge", "城西积水片有向边台账", "长度：米；速度：km/h",
            datetime(2026, 6, 1, tzinfo=UTC), C1,
            raw="城西片有向边，WA..WF 为主通道，WP 为平行支路，WC-R 为 WC 对向车道",
        )
        # node_id, name, x, y, allow_wait
        nodes = [
            ("W1", "城西路口", -6000, 0, False),
            ("W2", "槐安桥西", -5000, 0, True),
            ("W3", "槐安桥东", -4000, 0, True),
            ("W4", "城西门户", -3000, 0, True),
            ("W5", "平行支路北点", -5000, 1000, False),
            ("W6", "平行支路东点", -4000, 1000, False),
            ("W7", "河西巷西", -6000, -500, True),
            ("W8", "河西巷东", -5000, -500, True),
            ("W9", "河南立交", -4000, -500, True),
            ("W10", "南苑西口", -3000, -500, True),
            ("W11", "南苑东口", -2000, -500, True),
            ("W12", "孤屯西", -6000, -1000, True),
            ("W13", "孤屯东", -5000, -1000, True),
            ("W14", "站前路西", -2000, 0, True),
            ("W15", "站前路东", -1000, 0, True),
        ]
        cur.executemany(
            "INSERT INTO road_nodes (node_id, name, geom_x, geom_y, allow_wait, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(nid, name, x, y, wait, ndoc) for nid, name, x, y, wait in nodes],
        )

        # (edge_id, from, to, length_m)
        edges = [
            ("WA", "W1", "W2", 1000),     # 修订证据样例边
            ("WB", "W2", "W3", 1000),     # 平行道路误匹配样例边
            ("WB2", "W3", "W4", 1000),
            ("WP1", "W2", "W5", 1000),    # 平行支路连接线
            ("WP2", "W5", "W6", 1000),    # 与 WB 平行、相距 1000m
            ("WC", "W7", "W8", 1000),     # 东向车道（保持通行）
            ("WC-R", "W8", "W7", 1000),   # 西向车道（方向性封闭落在这条）
            ("WC2", "W8", "W9", 1000),
            ("WC3", "W9", "W4", 1118),
            ("WD", "W10", "W11", 1000),   # 晚传照片样例边
            ("WD2", "W11", "W4", 1118),
            ("WE", "W12", "W13", 1000),   # 传感器离线样例边
            ("WE2", "W13", "W8", 1118),
            ("WF", "W14", "W15", 1000),   # 相反上报样例边
            ("WF2", "W15", "W11", 1118),
        ]
        cur.executemany(
            """INSERT INTO road_edge_revisions
                   (edge_id, revision_no, from_node, to_node, length_m, base_speed_kmh,
                    valid_from, source_doc_id)
               VALUES (%s,1,%s,%s,%s,60,%s,%s)""",
            [(eid, f, t, length, datetime(2026, 6, 1, tzinfo=UTC), edoc)
             for eid, f, t, length in edges],
        )

        sdoc = _add_doc(
            cur, "shelter", "城西片安置点名录", "容量单位：人（本平台不做分配）",
            datetime(2026, 7, 1, tzinfo=UTC), C1,
            raw="城西片唯一既有安置点 SW，位于城西门户 W4",
        )
        cur.execute(
            "INSERT INTO shelters (shelter_id, name, node_id, geom_x, geom_y, revision_no, source_doc_id) "
            "VALUES ('SW','城西门户安置点','W4',-3000,0,1,%s)",
            (sdoc,),
        )
        cur.execute(
            "INSERT INTO shelter_open_windows (shelter_id, open_from, open_to, revision_no) "
            "VALUES ('SW',%s,%s,1)",
            (datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 7, 15, 18, tzinfo=UTC)),
        )

        pdoc = _add_doc(
            cur, "population", "城西片规则网格人口", "单位：人；网格 500m×500m",
            datetime(2026, 6, 1, tzinfo=UTC), C1,
            raw="城西片 GA/GC/GD/GE/GF 五个网格，连接到各自责任路口",
        )
        grids = [
            ("GA", "城西网格 A（修订证据片）", -6000, 200, "W1", 800),
            ("GC", "城西网格 C（方向封闭片）", -6000, -300, "W7", 500),
            ("GD", "城西网格 D（晚传照片片）", -3000, -300, "W10", 500),
            ("GE", "城西网格 E（传感器离线片）", -6000, -800, "W12", 600),
            ("GF", "城西网格 F（相反上报片）", -2000, 200, "W14", 600),
        ]
        cur.executemany(
            "INSERT INTO pop_grids (grid_id, name, geom_x, geom_y, boundary_id, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(gid, name, x, y, BOUNDARY_WEST, pdoc) for gid, name, x, y, _, _ in grids],
        )
        cur.executemany(
            "INSERT INTO pop_grid_revisions (grid_id, revision_no, population, as_of_date, source_doc_id) "
            "VALUES (%s,1,%s,%s,%s)",
            [(gid, pop, datetime(2026, 6, 1, tzinfo=UTC).date(), pdoc)
             for gid, _, _, _, _, pop in grids],
        )
        cur.executemany(
            "INSERT INTO grid_connectors (grid_id, node_id, allocation_ratio, revision_no, source_doc_id) "
            "VALUES (%s,%s,1.0,1,%s)",
            [(gid, node, pdoc) for gid, _, _, _, node, _ in grids],
        )
        conn.commit()


def _ev(**kw) -> dict:
    base = {
        "source_org": "市防汛办",
        "threshold_unit": "cm",
        "raw_text": "",
        "bearing_deg": None,
        "threshold_value": None,
        "observed_value": None,
        "observed_recovery_at": None,
        "offline": False,
        "location_text": None,
        "change_note": None,
    }
    base.update(kw)
    return base


def _seed_evidence_rows(conn) -> None:
    # 1) 平行道路误匹配：传感器在 WB(y=0) 与 WP2(y=1000) 正中，误差包络覆盖三条边
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S1", source_type="water_sensor", source_ref="WS-PARALLEL",
        geom_x=-4500, geom_y=500, location_error_m=600,
        observed_at=_dt(20), uploaded_at=_dt(22),
        threshold_value=27, observed_value=35,
        raw_text="传感器读数 35cm，超阈值 27cm，但安装位置偏离道路且贴近平行支路",
    ))

    # 2) 方向性封闭：照片明确西向 270°，规范化到对向有向边 WC-R
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S2", source_type="inspection_photo", source_ref="IP-DIR",
        geom_x=-5500, geom_y=-500, location_error_m=80,
        observed_at=_dt(20), uploaded_at=_dt(25), bearing_deg=270,
        raw_text="巡查标注：河西巷西向车道积水封闭，东向车道仍可通行",
    ))

    # 3) 晚传照片：08:10 拍摄，11:15 才上传（晚 185 分钟），08:45 截止点不可见
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S3", source_type="inspection_photo", source_ref="IP-LATE",
        geom_x=-2500, geom_y=-500, location_error_m=100,
        observed_at=_dt(10), uploaded_at=_dt(195), bearing_deg=90,
        raw_text="巡查照片：南苑西口积水，网络中断导致延后上传",
        change_note="晚传补录",
    ), action="backfill")

    # 4) 相反上报：两条群众封路上报 + 一条官方传感器未超阈值 → conflict
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S4A", source_type="crowd_report", source_ref="CR-20260715-01",
        source_org="市民热线", geom_x=-1500, geom_y=0, location_error_m=250,
        observed_at=_dt(25), uploaded_at=_dt(33), bearing_deg=90,
        location_text="火车东站站前路口",
        raw_text="群众上报：站前路路口严重积水无法通行",
    ))
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S4B", source_type="crowd_report", source_ref="CR-20260715-02",
        source_org="市民热线", geom_x=-1600, geom_y=30, location_error_m=250,
        observed_at=_dt(27), uploaded_at=_dt(40),
        location_text="东站附近",
        raw_text="群众上报：东站方向积水封路，麻烦处理",
    ))
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S4C", source_type="water_sensor", source_ref="WS-OPEN",
        geom_x=-1500, geom_y=0, location_error_m=60,
        observed_at=_dt(30), uploaded_at=_dt(31),
        threshold_value=27, observed_value=5,
        raw_text="传感器读数 5cm，未超阈值 27cm",
    ))

    # 5) 传感器离线：无读数 → 不产生立场，WE 保持未知
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S5", source_type="water_sensor", source_ref="WS-OFF",
        geom_x=-5500, geom_y=-1000, location_error_m=50,
        observed_at=_dt(15), uploaded_at=_dt(20), offline=True,
        threshold_value=27, raw_text="设备离线，未回传读数",
    ))

    # 6a) 修订证据首发：WA 传感器超阈值封路
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S6", source_type="water_sensor", source_ref="WS-A",
        geom_x=-5500, geom_y=0, location_error_m=50,
        observed_at=_dt(5), uploaded_at=_dt(8),
        threshold_value=27, observed_value=33,
        raw_text="传感器读数 33cm，超阈值 27cm",
    ))
    # 6b) 09:10 复核修正：误报，08:25 已退水（新修订行，旧截止点仍只见 r1）
    ev_service.ingest_evidence(conn, _ev(
        evidence_id="EV-S6", source_type="water_sensor", source_ref="WS-A",
        geom_x=-5500, geom_y=0, location_error_m=50,
        observed_at=_dt(5), uploaded_at=_dt(70),
        threshold_value=27, observed_value=12,
        observed_recovery_at=_dt(25),
        raw_text="现场复核：首轮阈值误报，实测 12cm，08:25 已退水恢复通行",
        change_note="复核修正：撤销封路结论",
    ), action="revise", evidence_id="EV-S6")
