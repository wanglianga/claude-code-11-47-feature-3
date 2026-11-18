-- ============================================================================
-- 防汛需求与道路情景覆盖数据分析平台 —— 数据库结构
-- 设计要点：
--   1. 来源目录中的每类原文（人口/节点/有向边/安置点/事件/边界）均带单位、
--      发生时间、登记时间、修订号与截止时间，可追溯到 source_documents。
--   2. 路网按“资料截止点”形成 graph_version；事件带生效区间与修订审计。
--   3. 情景工作区固定人口基准日、出发时刻、总时限、道路资料截止点。
--   4. 计算结果以完整情景签名入缓存索引，证据截止时间随结果保存。
-- ============================================================================

CREATE TABLE IF NOT EXISTS source_documents (
    doc_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_type        TEXT NOT NULL CHECK (dataset_type IN
                        ('population','road_node','road_edge','shelter',
                         'road_event','boundary','road_evidence')),
    title               TEXT NOT NULL,
    source_org          TEXT,
    unit_note           TEXT,                     -- 原文计量单位说明
    source_occurred_at  TIMESTAMPTZ,              -- 原文所述事件发生时间
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutoff_at           TIMESTAMPTZ NOT NULL,     -- 该资料代表的证据截止时间
    revision_no         INTEGER NOT NULL DEFAULT 1,
    supersedes_doc_id   BIGINT REFERENCES source_documents(doc_id),
    raw_text            TEXT,                     -- 原文摘录
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 区域边界 --
CREATE TABLE IF NOT EXISTS region_boundaries (
    boundary_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    geometry            JSONB NOT NULL,           -- [[x,y],...] 简单多边形
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    revised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 道路节点 --
CREATE TABLE IF NOT EXISTS road_nodes (
    node_id             TEXT PRIMARY KEY,
    name                TEXT,
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    allow_wait          BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否允许在该节点等待
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------- 有向边（版本化修订） --
-- 同一边的每次修订一行，用 valid_from/valid_to 表示有效期；截止点取数时
-- 只选 valid_from <= cutoff < valid_to 的修订。
CREATE TABLE IF NOT EXISTS road_edge_revisions (
    edge_id             TEXT NOT NULL,
    revision_no         INTEGER NOT NULL,
    from_node           TEXT NOT NULL REFERENCES road_nodes(node_id),
    to_node             TEXT NOT NULL REFERENCES road_nodes(node_id),
    length_m            DOUBLE PRECISION NOT NULL CHECK (length_m > 0),
    base_speed_kmh      DOUBLE PRECISION NOT NULL CHECK (base_speed_kmh > 0),
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,                       -- NULL 表示至今有效
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    PRIMARY KEY (edge_id, revision_no),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_edge_rev_valid ON road_edge_revisions(valid_from, valid_to);

-- --------------------------------------------- 封路 / 分时限速事件（时变） --
-- 同一事件的每次修订各占一行（复合主键），快照按资料截止点取 received_at <= cutoff
-- 的最新修订，保证旧截止点分析在新修订录入后仍可精确复现。
CREATE TABLE IF NOT EXISTS road_event_registry (
    event_id            TEXT PRIMARY KEY,
    edge_id             TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('closure','speed_limit')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS road_events (
    event_id            TEXT NOT NULL REFERENCES road_event_registry(event_id),
    revision_no         INTEGER NOT NULL,
    edge_id             TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('closure','speed_limit')),
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,              -- NULL 表示尚未恢复
    speed_limit_kmh     DOUBLE PRECISION CHECK (speed_limit_kmh IS NULL OR speed_limit_kmh > 0),
    supersedes_event_id TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','cancelled')),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 平台收到该修订的时间
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, revision_no),
    CHECK (event_type = 'closure' OR speed_limit_kmh IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_event_edge ON road_events(edge_id);
CREATE INDEX IF NOT EXISTS idx_event_effective ON road_events(effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_event_received ON road_events(received_at);

-- 事件修订审计：封路、延期、恢复（重开）、限速调整均留痕
CREATE TABLE IF NOT EXISTS road_event_revisions (
    audit_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES road_event_registry(event_id),
    revision_no         INTEGER NOT NULL,
    change_summary      TEXT NOT NULL,
    change_json         JSONB NOT NULL,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id)
);

-- ---------------------------------------------------------------- 安置点 --
CREATE TABLE IF NOT EXISTS shelters (
    shelter_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    node_id             TEXT NOT NULL REFERENCES road_nodes(node_id),
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    revised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    active              BOOLEAN NOT NULL DEFAULT TRUE
);

-- 安置点开放区间：到达时刻必须落在任一区间内才视为开放
CREATE TABLE IF NOT EXISTS shelter_open_windows (
    window_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shelter_id          TEXT NOT NULL REFERENCES shelters(shelter_id),
    open_from           TIMESTAMPTZ NOT NULL,
    open_to             TIMESTAMPTZ,                 -- NULL 表示长期开放
    revision_no         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (shelter_id, open_from, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_shelter_window ON shelter_open_windows(shelter_id);

-- ---------------------------------------------------------------- 人口网格 --
CREATE TABLE IF NOT EXISTS pop_grids (
    grid_id             TEXT PRIMARY KEY,
    name                TEXT,
    geom_x              DOUBLE PRECISION NOT NULL,   -- 质心，供前端定位
    geom_y              DOUBLE PRECISION NOT NULL,
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 人口修订：同一网格按基准日取 as_of_date 当日可见的最新修订
CREATE TABLE IF NOT EXISTS pop_grid_revisions (
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    revision_no         INTEGER NOT NULL,
    population          DOUBLE PRECISION NOT NULL CHECK (population >= 0),
    as_of_date          DATE NOT NULL,               -- 人口基准日
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_pop_rev_asof ON pop_grid_revisions(as_of_date);

-- 网格→路网连接位置与互斥分配比例；多出口网格按子份额切分人口
CREATE TABLE IF NOT EXISTS grid_connectors (
    connector_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    node_id             TEXT NOT NULL REFERENCES road_nodes(node_id),
    allocation_ratio    DOUBLE PRECISION NOT NULL CHECK
                        (allocation_ratio > 0 AND allocation_ratio <= 1),
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    UNIQUE (grid_id, node_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_connector_grid ON grid_connectors(grid_id);

-- ------------------------------------------------- 路网版本（资料截止点） --
CREATE TABLE IF NOT EXISTS road_network_versions (
    version_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cutoff_at           TIMESTAMPTZ NOT NULL,
    edge_fingerprint    TEXT NOT NULL,               -- 截止点边修订集合指纹
    event_fingerprint   TEXT NOT NULL,               -- 人工事件+证据派生封路集合指纹
    evidence_strategy   TEXT,                        -- 该版本采用的证据融合策略
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cutoff_at, edge_fingerprint, event_fingerprint)
);

-- ---------------------------------------------------------------- 情景工作区 --
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id         UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    population_as_of    DATE NOT NULL,               -- 固定人口基准日
    departure_time      TIMESTAMPTZ NOT NULL,        -- 固定出发时刻
    max_trip_minutes    INTEGER NOT NULL CHECK (max_trip_minutes > 0),
    road_cutoff         TIMESTAMPTZ NOT NULL,        -- 道路资料截止点
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),  -- 分析边界，NULL=全部
    graph_version_id    BIGINT REFERENCES road_network_versions(version_id),
    signature           TEXT NOT NULL UNIQUE,        -- 完整输入的情景签名
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,        -- 随结果标注的证据截止时间
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 计算任务 --
-- ref_count 记录当前查看者数量；取消单个查看者只减引用，不归零不取消任务。
CREATE TABLE IF NOT EXISTS compute_tasks (
    task_id             UUID PRIMARY KEY,
    scenario_id         UUID NOT NULL REFERENCES scenarios(scenario_id),
    signature           TEXT NOT NULL,
    graph_version_id    BIGINT NOT NULL REFERENCES road_network_versions(version_id),
    kind                TEXT NOT NULL CHECK (kind IN ('precomputed','on_demand')),
    status              TEXT NOT NULL CHECK (status IN
                        ('queued','running','done','failed','cancelled'))
                        DEFAULT 'queued',
    ref_count           INTEGER NOT NULL DEFAULT 0,
    stale               BOOLEAN NOT NULL DEFAULT FALSE,  -- 路网更新后标记失效
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_task_signature ON compute_tasks(signature, status);

CREATE TABLE IF NOT EXISTS compute_task_viewers (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    viewer_id           TEXT NOT NULL,
    joined_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, viewer_id)
);

-- 任务实际经过的边依赖（来自见证路径），供封路修订精准定位受影响任务
CREATE TABLE IF NOT EXISTS task_edge_dependencies (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    edge_id             TEXT NOT NULL,
    PRIMARY KEY (task_id, edge_id)
);

-- 缓存索引：签名即完整输入，不同出发时刻/开放条件绝不共用缓存行
CREATE TABLE IF NOT EXISTS coverage_cache_index (
    signature           TEXT PRIMARY KEY,
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    graph_version_id    BIGINT NOT NULL,
    departure_time      TIMESTAMPTZ NOT NULL,
    max_trip_minutes    INTEGER NOT NULL,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    stale               BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 节点到达 --
CREATE TABLE IF NOT EXISTS node_arrivals (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    node_id             TEXT NOT NULL,
    arrival_time        TIMESTAMPTZ NOT NULL,
    wait_cost_sec       DOUBLE PRECISION NOT NULL DEFAULT 0,
    witness_node_path   JSONB NOT NULL,              -- 有序节点序列
    witness_edge_path   JSONB NOT NULL,              -- 有序边序列（见证路径）
    PRIMARY KEY (task_id, node_id)
);

-- ---------------------------------------------------------------- 网格份额结果 --
-- 每个互斥子份额一行；covered 去重在汇总阶段完成，同一网格只计一次。
CREATE TABLE IF NOT EXISTS grid_share_results (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    grid_id             TEXT NOT NULL,
    connector_node      TEXT NOT NULL,
    share_index         INTEGER NOT NULL,
    share_population    DOUBLE PRECISION NOT NULL,
    reachable           BOOLEAN NOT NULL,
    shelter_id          TEXT REFERENCES shelters(shelter_id),
    departure_time      TIMESTAMPTZ,
    arrival_time        TIMESTAMPTZ,
    total_wait_sec      DOUBLE PRECISION NOT NULL DEFAULT 0,
    witness_edge_path   JSONB,
    status              TEXT NOT NULL CHECK (status IN
                        ('covered','uncovered','unknown')),
    reason              TEXT,                        -- 被排除/未知原因
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, grid_id, share_index)
);
CREATE INDEX IF NOT EXISTS idx_share_task ON grid_share_results(task_id);

-- ---------------------------------------------------------------- 覆盖汇总 --
CREATE TABLE IF NOT EXISTS coverage_summaries (
    task_id             UUID PRIMARY KEY REFERENCES compute_tasks(task_id),
    total_population    DOUBLE PRECISION NOT NULL,   -- 边界内全部分母
    located_population  DOUBLE PRECISION NOT NULL,   -- 可定位到路网的人口
    unknown_population  DOUBLE PRECISION NOT NULL,   -- 无连接/缺分配依据
    covered_population  DOUBLE PRECISION NOT NULL,   -- 网格级去重后覆盖
    uncovered_population DOUBLE PRECISION NOT NULL,
    covered_grid_count  INTEGER NOT NULL,
    unknown_grid_count  INTEGER NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 结果发布 --
-- input_manifest 保存本次比较采用的完整输入（修订号、截止点、窗口原文等）
CREATE TABLE IF NOT EXISTS publications (
    publication_id      UUID PRIMARY KEY,
    title               TEXT NOT NULL,
    scenario_a_id       UUID REFERENCES scenarios(scenario_id),
    scenario_b_id       UUID REFERENCES scenarios(scenario_id),
    input_manifest      JSONB NOT NULL,
    comparison_json     JSONB,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 道路通行证据融合与冲突解释（feature-3 增量）
-- 设计要点：
--   1. 每条候选道路事件（积水传感器/巡查照片/群众上报）保留来源坐标、观测与
--      上传时间、方向、观测阈值、定位误差与原始证据指纹，证据只追加不覆盖。
--   2. 证据经规则投影为（边, 方向, 有效时间区间）；投影不确定时全部候选留痕，
--      绝不静默挑一条。空间匹配/时间推导的每一步都可在投影行中解释。
--   3. 融合按来源可靠性 × 空间误差折减加权，绝不按来源数量多数表决；存在相反
--      结论或证据不足时只给 unknown/conflict，不产生无来源的确定封路/通行。
--   4. 只有 confirmed_closure 结论物化为派生封路；撤回/修正/补传沿“边+时间窗”
--      精准失效引用任务，既有发布保留当时的证据清单。
-- ============================================================================

-- --------------------------------------------------------- 证据来源类型目录 --
CREATE TABLE IF NOT EXISTS evidence_source_types (
    source_type         TEXT PRIMARY KEY CHECK (source_type IN
                        ('water_sensor','inspection_photo','crowd_report')),
    type_name           TEXT NOT NULL,
    base_reliability    DOUBLE PRECISION NOT NULL CHECK
                        (base_reliability > 0 AND base_reliability <= 1),
    official            BOOLEAN NOT NULL,
    note                TEXT
);

-- 融合策略配置（单行）：official_only 仅采信官方传感器/巡查；multi_source 多源加权
CREATE TABLE IF NOT EXISTS evidence_fusion_config (
    single_row          TEXT PRIMARY KEY DEFAULT 'X' CHECK (single_row = 'X'),
    strategy            TEXT NOT NULL CHECK (strategy IN
                        ('official_only','multi_source')),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------- 原始证据（追加式，修订另起新行） --
CREATE TABLE IF NOT EXISTS road_evidence (
    evidence_id         TEXT NOT NULL,
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_type         TEXT NOT NULL REFERENCES evidence_source_types(source_type),
    source_ref          TEXT NOT NULL,                  -- 设备号/照片号/上报单号
    source_org          TEXT,
    -- 来源坐标与定位误差（米，半径）：传感器可能偏离道路、群众上报只描述附近地名
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    location_error_m    DOUBLE PRECISION NOT NULL CHECK (location_error_m >= 0),
    location_text       TEXT,                            -- 群众上报的附近地名词原文
    observed_at         TIMESTAMPTZ NOT NULL,            -- 观测/拍摄时间
    uploaded_at         TIMESTAMPTZ NOT NULL,            -- 平台收到（上传）时间
    -- 观测方向（度，0-360，NULL=来源未给出方向）；观测阈值与原始读数/原文
    bearing_deg         DOUBLE PRECISION CHECK
                        (bearing_deg IS NULL OR (bearing_deg >= 0 AND bearing_deg < 360)),
    threshold_value     DOUBLE PRECISION,                 -- 触发阈值（如积水深度 cm）
    observed_value      DOUBLE PRECISION,                 -- 实际读数（可空，照片/上报无读数）
    observed_recovery_at TIMESTAMPTZ,                     -- 来源给出的恢复时刻（可空）
    offline             BOOLEAN NOT NULL DEFAULT FALSE,   -- 传感器离线：无读数不产生立场
    threshold_unit      TEXT,
    raw_text            TEXT,
    evidence_fingerprint TEXT NOT NULL,                  -- 原始证据指纹（内容哈希）
    -- 生命周期：active 参与融合；withdrawn 保留行但不参与（撤回留痕）
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','withdrawn')),
    supersedes_revision INTEGER,
    change_note         TEXT,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (evidence_id, revision_no),
    CHECK (uploaded_at >= observed_at)
);
CREATE INDEX IF NOT EXISTS idx_evidence_observed ON road_evidence(observed_at);
CREATE INDEX IF NOT EXISTS idx_evidence_uploaded ON road_evidence(uploaded_at);
CREATE INDEX IF NOT EXISTS idx_evidence_status ON road_evidence(status);

-- 证据修订审计：撤回、修正、补传均留痕
CREATE TABLE IF NOT EXISTS road_evidence_audits (
    audit_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evidence_id         TEXT NOT NULL,
    revision_no         INTEGER NOT NULL,
    action              TEXT NOT NULL CHECK (action IN
                        ('ingest','revise','withdraw','backfill')),
    change_json         JSONB NOT NULL,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_audit ON road_evidence_audits(evidence_id, revision_no);

-- --------------------------------------------- 证据投影：证据 → 边/方向/时间窗 --
-- 一条证据可投影到多条候选边（平行道路歧义时全部保留），accepted=FALSE 记录
-- “为何不够”，供分析员从被排除路径回溯到具体证据与规则。
CREATE TABLE IF NOT EXISTS evidence_projections (
    projection_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evidence_id         TEXT NOT NULL,
    evidence_revision   INTEGER NOT NULL,
    edge_id             TEXT NOT NULL,
    -- 投影方向：forward=边的 from→to，reverse=反方向，both=来源无方向（覆盖双向）
    direction           TEXT NOT NULL CHECK (direction IN ('forward','reverse','both')),
    stance              TEXT NOT NULL CHECK (stance IN ('closure','open','none')),
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,                       -- NULL=观测未给出恢复时刻
    -- 空间匹配留痕
    perpendicular_m     DOUBLE PRECISION NOT NULL,         -- 来源点到边中心线垂距
    along_m             DOUBLE PRECISION NOT NULL,         -- 垂足距边起点的沿线距离
    edge_length_m       DOUBLE PRECISION NOT NULL,
    spatial_weight      DOUBLE PRECISION NOT NULL,         -- 空间误差折减后的权重(0,1]
    direction_match     TEXT NOT NULL CHECK
                        (direction_match IN ('exact','inferred','none','unknown')),
    direction_weight    DOUBLE PRECISION NOT NULL,
    -- 时间推导留痕
    time_basis          TEXT NOT NULL,                     -- observed | late_upload_discounted
    time_weight         DOUBLE PRECISION NOT NULL,         -- 晚传折减后的权重(0,1]
    upload_delay_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- 投影判定
    accepted            BOOLEAN NOT NULL,
    reject_reason       TEXT,
    rule_trace          JSONB NOT NULL,                    -- 完整规则轨迹（可解释）
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (evidence_id, evidence_revision, edge_id, direction),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_proj_edge ON evidence_projections(edge_id);
CREATE INDEX IF NOT EXISTS idx_proj_accepted ON evidence_projections(accepted);
CREATE INDEX IF NOT EXISTS idx_proj_valid ON evidence_projections(valid_from, valid_to);

-- --------------------------------------------- 融合批次（策略 + 截止点 + 指纹） --
CREATE TABLE IF NOT EXISTS evidence_fusion_runs (
    run_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy            TEXT NOT NULL CHECK (strategy IN
                        ('official_only','multi_source')),
    cutoff_at           TIMESTAMPTZ NOT NULL,
    input_fingerprint   TEXT NOT NULL,                    -- 参与证据修订集合指纹
    conclusion_fingerprint TEXT NOT NULL,                 -- 结论集合指纹（复现校验）
    rule_version        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'completed'
                        CHECK (status IN ('completed','superseded')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy, cutoff_at, input_fingerprint)
);

-- --------------------------------------------- 融合结论：边/方向/时间窗等级 -- --
-- level: confirmed_closure（确定封闭，可物化为封路）/ likely_closure（高倾向但不
--   确定，只提示不定态）/ conflict（同窗相反结论）/ unknown（证据不足，保持未知）。
-- 任何等级都带完整来源权重明细；confirmed 之外绝不生成确定性路网状态。
CREATE TABLE IF NOT EXISTS edge_evidence_conclusions (
    conclusion_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              BIGINT NOT NULL REFERENCES evidence_fusion_runs(run_id),
    strategy            TEXT NOT NULL,
    edge_id             TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('forward','reverse','both')),
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,
    level               TEXT NOT NULL CHECK (level IN
                        ('confirmed_closure','likely_closure','conflict','unknown')),
    -- 权重聚合（可靠性×空间/方向折减后的证据权重，非来源计数）
    weight_closure      DOUBLE PRECISION NOT NULL DEFAULT 0,
    weight_open         DOUBLE PRECISION NOT NULL DEFAULT 0,
    uncertainty         DOUBLE PRECISION NOT NULL,        -- 0..1，越大越不确定
    has_conflict        BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_count      INTEGER NOT NULL,
    evidence_refs       JSONB NOT NULL,                   -- 证据指纹与权重明细
    rule_trace          JSONB NOT NULL,
    cutoff_at           TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_concl_edge_time
    ON edge_evidence_conclusions(edge_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_concl_run ON edge_evidence_conclusions(run_id);
CREATE INDEX IF NOT EXISTS idx_concl_level ON edge_evidence_conclusions(level);

-- 结论 ↔ 证据 多对多留痕（同时支持按证据反查它影响过哪些结论）
CREATE TABLE IF NOT EXISTS conclusion_evidence_refs (
    conclusion_id       BIGINT NOT NULL REFERENCES edge_evidence_conclusions(conclusion_id)
                        ON DELETE CASCADE,
    evidence_id         TEXT NOT NULL,
    evidence_revision   INTEGER NOT NULL,
    projection_id       BIGINT REFERENCES evidence_projections(projection_id),
    stance              TEXT NOT NULL CHECK (stance IN ('closure','open')),
    weight              DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (conclusion_id, evidence_id, evidence_revision)
);
CREATE INDEX IF NOT EXISTS idx_ref_evidence ON conclusion_evidence_refs(evidence_id, evidence_revision);

-- --------------------------------------------- 派生封路（仅 confirmed 结论） -- --
-- 与人工 road_events 分开存放：它完全由证据融合生成，撤回/修正时可整组重算；
-- 快照按截止点把它与人工事件一并载入引擎。
CREATE TABLE IF NOT EXISTS derived_road_closures (
    derived_event_id    TEXT PRIMARY KEY,
    conclusion_id       BIGINT NOT NULL REFERENCES edge_evidence_conclusions(conclusion_id),
    run_id              BIGINT NOT NULL REFERENCES evidence_fusion_runs(run_id),
    edge_id             TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('forward','reverse','both')),
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,
    cutoff_at           TIMESTAMPTZ NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','retracted')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_derived_edge ON derived_road_closures(edge_id);
CREATE INDEX IF NOT EXISTS idx_derived_cutoff ON derived_road_closures(cutoff_at);

-- --------------------------------------------- 任务 ↔ 证据结论（时间窗依赖） -- --
-- 比 task_edge_dependencies 更细：记录任务在哪个时间窗实际经过该边，证据修订时
-- 只重算“边相同且时间窗相交”的情景，而非该边的全部历史任务。
CREATE TABLE IF NOT EXISTS task_evidence_dependencies (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    edge_id             TEXT NOT NULL,
    traversed_from      TIMESTAMPTZ NOT NULL,
    traversed_to        TIMESTAMPTZ NOT NULL,
    direction           TEXT NOT NULL,
    PRIMARY KEY (task_id, edge_id, traversed_from)
);
CREATE INDEX IF NOT EXISTS idx_task_ev_edge ON task_evidence_dependencies(edge_id);
CREATE INDEX IF NOT EXISTS idx_task_ev_task ON task_evidence_dependencies(task_id);

-- 任务证据清单快照：发布/计算时固化，证据后续撤回不改变既有发布的清单
CREATE TABLE IF NOT EXISTS task_evidence_manifests (
    task_id             UUID PRIMARY KEY REFERENCES compute_tasks(task_id),
    run_id              BIGINT REFERENCES evidence_fusion_runs(run_id),
    strategy            TEXT,
    cutoff_at           TIMESTAMPTZ NOT NULL,
    conclusion_fingerprint TEXT,
    manifest_json       JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 证据修订影响登记：每次撤回/修正/补传记录受影响任务与网格，供审计界面追溯
CREATE TABLE IF NOT EXISTS evidence_impact_logs (
    impact_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evidence_id         TEXT NOT NULL,
    action              TEXT NOT NULL,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    affected_edge_ids   JSONB NOT NULL,
    affected_task_ids   JSONB NOT NULL,
    affected_grid_ids   JSONB NOT NULL,
    detail_json         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_evidence ON evidence_impact_logs(evidence_id);
