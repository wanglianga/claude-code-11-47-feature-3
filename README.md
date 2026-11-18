# 防汛需求与道路情景覆盖数据分析平台

预案分析员在暴雨情景下比较居民在不同出发时刻，能否沿**可通行道路**到达**既有安置点**，
并区分覆盖面变化究竟来自**道路恢复、人口资料变化还是分析边界改变**。

平台只计算**可达人口及证据限制**，不做安置点选址、人员分配或物资调运。

技术栈：**React 18 + TypeScript + Vite + Leaflet（离线 Simple CRS 平面地图）**、
**FastAPI**、**PostgreSQL 18**、**Redis 8（失效通知/任务状态，可选降级）**。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 前端 | http://localhost:8080 | Nginx 托管静态资源并反代 `/api` |
| 后端 API | http://localhost:8000 | 交互式文档 http://localhost:8000/docs |
| PostgreSQL | localhost:5432 | 库/用户/密码 `ccdev/ccdev/ccdev` |
| Redis | localhost:6379 | 无密码 |

后端容器启动时自动建表；**空库**自动写入三套样例（小型可手算路网、8×8 大区样例、
城西积水片证据融合样例），已有数据则保留不重置。重新播种可调用 `POST /admin/reseed`。

## 2. 本地开发启动（使用容器预装运行时）

### 后端

```bash
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
export FLOOD_DATABASE_URL="postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev"
export FLOOD_REDIS_URL="redis://127.0.0.1:6379/0"
python -m app.seed                      # 建表 + 写入两套样例（会重建样例数据）
uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install --registry=https://registry.npmmirror.com
npm run dev                             # http://localhost:5173 ，/api 已代理到 8000
npm run build                           # 类型检查 + 生产构建
```

### 测试身份

平台不设登录。查看者身份由前端生成的 `viewer_id` 标识（形如 `viewer-1001`），
用于计算任务的引用计数：一个查看者离开只减引用，不取消其他查看者仍需要的任务。
接口调用时可在 `POST /scenarios` 的 `viewer_id` 字段任意指定，例如 `analyst-01`。

#### 证据融合快速验证（本地起服务后）

```bash
# 1. 08:45 截止点执行多源融合（confirmed 才会派生封路）
curl -s -X POST localhost:8000/evidence/fusion/run \
  -H 'Content-Type: application/json' \
  -d '{"cutoff_at":"2026-07-15T08:45:00+00:00"}'
# 2. 城西片情景：边界 B_WEST、08:10 出发、45 分钟时限（GA 会因 WA 证据封路不可覆盖）
curl -s -X POST localhost:8000/scenarios -H 'Content-Type: application/json' -d '{
  "name":"城西证据情景","population_as_of":"2026-06-01",
  "departure_time":"2026-07-15T08:10:00+00:00","max_trip_minutes":45,
  "road_cutoff":"2026-07-15T08:45:00+00:00","boundary_id":"B_WEST","viewer_id":"analyst-01"}'
# 3. 从被排除网格下钻到证据（把 <task_id> 换成上一步返回值）
curl -s localhost:8000/evidence/tasks/<task_id>/grids/GA/drilldown
# 4. 11:30 再融合：晚传照片可见、修订生效，仅引用 WA 的任务被定向失效
curl -s -X POST localhost:8000/evidence/fusion/run \
  -H 'Content-Type: application/json' \
  -d '{"cutoff_at":"2026-07-15T11:30:00+00:00"}'
```

---

## 3. 数据与口径说明

### 3.1 来源目录（全部带原文、单位、发生/登记时间、修订号与证据截止点）

- 规则网格人口（含修订表与基准日）、路网节点、**有向边修订**、安置点及开放区间、
  封路/分时限速事件（**追加式多修订行** + 修订审计）、区域边界；
- 每个网格保存与路网的**连接位置与互斥分配比例**；多出口网格按互斥子份额切分人口；
- 事件每次修订只追加新行（含 `received_at`），快照按截止点取该事件最新的**可见**修订；
  因此旧截止点分析在新修订录入后仍可精确复现，不会被原地 UPDATE 改写历史。

### 3.2 情景工作区固定四项输入

人口基准日、出发时刻、允许行程时长、道路资料截止点（另可选分析边界）。
四要素 + 路网版本 + 边界共同组成**完整情景签名**，作为缓存唯一键。

### 3.3 时变路网求解规则（`app/engine/traversal.py`）

1. 遍历边时检查**整条通行时段** `[进入, 离开)` 是否与封路区间相交，而非只看出发时刻；
2. 分时限速按覆盖整段的最严格限速计算（定点细化）；
3. 仅 `allow_wait` 节点可等待道路恢复，等待段与等待成本逐段记录；
4. 等待后到达仍超出总时限的标签被剪枝，**不计入覆盖**；
5. 安置点按**实际到达时刻**判断开放；到达时关闭且节点允许等待，可等到下一开放区间起点，
   等待同样计入总时长；
6. 每个标签携带见证路径（节点序列 + 边序列 + 等待段）。

### 3.4 覆盖、去重与未知边界

- 份额人口 = 网格人口 × 互斥比例；同一份额能到多个安置点只计一次（取最早实际到达）；
- 网格级汇总天然去重，同一网格人口绝不重复计数；
- 未知单列且不默认全覆盖或零需求：
  `unknown_no_connector`（无连接）、`unknown_allocation_gap`（比例缺口）、
  `unknown_ratio_over_one`（比例矛盾）、`unknown_connector_missing`（连接节点不在该路网版本）。

### 3.5 混合计算与缓存纪律

- 常规出发窗口**预计算**；临时封路/限速登记后立即参与后续**即时计算**；
- 每项结果标注**证据截止时间**；不同出发时刻/开放条件签名不同，缓存命中也不复用他者覆盖面；
- 封路修订沿 `task_edge_dependencies` 定位受影响任务，并显式筛查**恢复候选**
  （路网含该边但旧路径未经过的任务——重开可能恢复此前不可达区域）；
- 同截止点迟到修订物化为**新路网版本**（新指纹、新 version_id、新签名），
  旧任务物理上无法覆盖新版本缓存行；重建期间的路网更新只会再产生新版本。

### 3.6 对比报告的共同分母

- 仅在两侧共有的网格集合上计算覆盖率与变化；
- 道路/开放窗口效应、人口修订效应、分析边界增删、未知份额变化**分桶展示**；
- 人口或边界口径不一致时给出显式警告，不能把换了分母的覆盖率当作改善；
- 发布记录（`publications`）固化两侧完整输入清单（修订号、截止点、版本指纹、网格人口与边界），
  并随两侧任务固化**当时的证据清单与融合批次指纹**（见 3.7）。

### 3.7 道路通行证据融合与冲突解释（规则版本 EV-FUSION-R1）

**证据接入只追加**：积水传感器 / 巡查照片 / 群众上报三类候选事件统一进入 `road_evidence`，
每条保留来源坐标、观测时间与上传时间、方向、观测阈值、定位误差与原始证据指纹；
撤回、修正、补传均新增修订行（`road_evidence_audits` 留痕），旧修订永不被原地改写。

**投影规则**（`services/evidence.py` + `services/evidence_geo.py`）把证据映射为
（边, 方向, 有效时间区间），全过程写入 `evidence_projections`：

1. 空间匹配：计算来源点到边中心线的垂距与沿线距离，按定位误差做指数折减；
   误差包络同时覆盖平行道路且无显著唯一候选时，模糊带内候选**全部拒绝**
   （`parallel_road_ambiguity`），不静默选边；
2. 方向规则：来源未给方向投影为双向并折减；方向明确时按 ±45° 解析，
   存在端点互换的对向有向边则规范化到对向车道（方向性封闭只作用于一个方向）；
3. 时间推导：有效区间从观测时刻起算，晚传按延迟折减（2 小时折半），
   上传晚于截止点的证据对该截止点不可见；离线无读数、立场不明的证据拒绝投影但保留行。

**融合规则禁止多数表决**：证据权重 = 来源基础可靠性（传感器 0.90 / 巡查 0.85 / 群众 0.55）
× 空间折减 × 方向折减 × 晚传折减。同一时间窗两侧都有有效权重即 `conflict`；
只有 `confirmed_closure`（封路侧权重 ≥0.55）才物化为 `derived_road_closures` 进入时变路网，
`likely_closure / conflict / unknown` 只记录等级与不确定度，**系统绝不生成无来源的确定状态**。

两种策略可切换（`evidence_fusion_config`）：`official_only` 仅采信官方来源，误报低但偏远道路
长期未知；`multi_source` 覆盖更广但必须展示冲突与不确定度。切换策略即失效全部缓存。

**定向重算与历史复现**：

- 任务逐边记录实际通行时间窗（`task_evidence_dependencies`）与任务级证据清单
  （`task_evidence_manifests`）；证据撤回/修正/补传与融合批次差异，只失效
  “边相同且时间窗相交”的任务（被阻断而没走成的任务通过清单结论通道同样命中），
  影响登记到 `evidence_impact_logs`，含受影响边、任务与网格；
- 派生封路按融合批次全量保留，快照只载入截止点可见的最近批次；旧截止点分析仍命中旧批次，
  后续修订无法改写历史，既有发布保留当时的证据清单。

城西积水片独立样例（边界 `B_WEST`，节点/边/网格以 W/WA…WF/GA…GF 命名）覆盖六类验收情形，
见第 4.2 节。

---

## 4. 小型可手算样例（每条边 1000m / 60km/h = 1 分钟，T0=2026-07-15 08:00 UTC）

- 单向边 `E1(N1→N2)`（无反向边）、绕行 `N2→N5→N6→N4`；
- `EV1`：E3 河东桥 08:05–08:20 临时封闭，N3 不可等待；
- `EV2`：E1 分时限速 20km/h，08:10–08:30（该时段通过需 3 分钟）；
- `EV3`：E7 东站引道 C1 时“尚未恢复”，C2 修订为 08:40 重开；
- 安置点 S1@N3、S2@N6（07–18 点开放），S3@N7（仅 09–12 点开放）；
- 网格：G1 多点重叠、G2 双出口 0.6/0.4、G3 无连接、G4 比例缺口 0.3、
  G5 比例和 1.2、G6 人口修订 1000→1200、G7 属另一边界、G8 仅靠 E7 外接（恢复验证）。

### 手工复算要点

| 验证点 | 预期 |
| --- | --- |
| 08:05 出发去 N4 | E3 封闭且 N3 不可等待 → 绕行 E1,E4,E5,E6，08:09 到达 |
| 08:10 进入 E1 | 整段时限速 20km/h → 180 秒，08:13 到 N2 |
| G8（C1，任意时限） | E7 无恢复时间 → uncovered |
| G8（C2，30 分钟时限） | 最早 08:41 到 N7，等到 09:00 超过时限 → uncovered（等待后超时不计） |
| G8（C2，60 分钟时限） | 09:00 到达 S3，等待合计 3540 秒 → covered |
| G2 | 互斥份额 300/200，分别到 S1/S2，合计仍为 500，不翻倍 |
| G3/G4 缺口/G5 | 分别落入三类 unknown，未知人口单列 |
| C1→C2 同口径对比 | 新增覆盖仅 G8（700 人），道路净效应 = 700；人口修订 G6 单列 |

大区样例：8×8 规则路网（奇数行单向交通组织）、固定模式限速/封闭事件、
4 个角安置点、40 个网格（第 40 个无连接 → 未知）。

### 4.2 城西积水片证据融合样例（B_WEST，T0 同上）

独立路网（不改动主样例 E1..E8）：主通道 WA..WF、平行支路 WP1/WP2、对向有向边
WC/WC-R，安置点 SW@W4，网格 GA/GC/GD/GE/GF。两个证据截止点：
`C_EV=08:45`、`C3=11:30`。融合**不自动执行**，播种只写证据与投影，需显式调用融合接口。

| # | 验收情形 | 证据与规则结果 |
| --- | --- | --- |
| 1 | 平行道路误匹配 | WS-PARALLEL 位于 WB 与 WP2 正中、误差包络覆盖多边 → 投影全部 `parallel_road_ambiguity` 拒绝，无确定封路 |
| 2 | 方向性封闭 | IP-DIR 明确西向 270° → 规范化到对向边 WC-R 并 confirmed；东向 WC 自由流，西向只能等 08:50 重开 |
| 3 | 晚传照片 | IP-LATE 08:10 拍摄、11:15 上传：08:45 截止点不可见；11:30 可见但晚传折减后仅 `likely_closure` |
| 4 | 相反上报 | WF 上两条群众封路 + 一条官方传感器未超阈值 → 按权重判 `conflict`（非多数表决），不物化封路 |
| 5 | 传感器离线 | WS-OFF 无读数 → `sensor_offline_no_reading` 拒绝，WE 保持未知 |
| 6 | 修订证据 | WS-A r1 确认 WA 封闭；09:10 复核 r2 修正为误报并给恢复时刻 → 新批次 WA 转 unknown，旧截止点仍复现封路，引用 WA 的任务被定向失效 |

手工核对口径（多源融合 @C_EV）：结论分级为
`confirmed×2（WA、WC-R）、likely×1（WF2）、conflict×1（WF）`；08:10 出发、45 分钟时限的
城西情景中 GA 因 WA 封闭不可覆盖，其余网格可覆盖；网格下钻可从 GA 看到 WA 的 confirmed
结论、从 GF 看到 WF 的冲突与双侧权重。@C3 再融合后 WA 不再有派生封路，晚传照片让 WD
进入 likely，旧任务标记 stale 而旧截止点快照与已发布清单保持原样。

---

## 5. 已执行的验证与结论

以下验证均在本工作区实际执行通过：

1. **后端单元/集成测试 47 项全部通过**（`pytest`，约 26 秒）：
   - 基线 33 项（单向边、绕行、整段封路、分时限速、等待剪枝、安置点开放、互斥份额、
     四类未知、签名缓存、引用计数、即时失效、恢复候选、版本防覆、追加式修订复现、
     共同分母对比与发布清单等）保持零回归；
   - 新增证据融合 14 项：平行道路歧义拒绝、方向性封闭只作用对向车道、晚传折减与截止点
     不可见、相反上报按权重判冲突而非多数表决、传感器离线保持未知、修订撤回的定向失效、
     官方-only 策略排除群众来源、网格证据下钻、指纹与追加式修订、撤回无时间窗也命中，
     以及 FastAPI TestClient 的证据接入/修订/撤回/融合/策略/下钻端到端。
2. **前端 `npm run build` 通过**（TypeScript 严格模式 + Vite 生产构建，42 个模块）。
3. **真实 HTTP 端到端流程通过**（uvicorn 实跑，12 步）：重新播种含证据样例、投影拒绝
   原因可查、C_EV 融合分级（confirmed×2/likely×1/conflict×1）、城西情景 GA 不可覆盖且
   下钻到 WA 证据、GF 下钻到 WF 冲突、任务证据清单固化（结论+20 条拒绝投影）、临时撤回
   精准失效引用任务、C3 再融合仅 WA 差异失效 1 个任务/GA 网格、发布保留当时证据清单、
   官方-only 策略下冲突消失。

重新执行验证：

```bash
cd backend && . .venv/bin/activate
pytest                                   # 47 项测试
cd ../frontend && npm run build          # 前端构建
```

## 6. 主要 API 一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/scenarios` | 按签名创建/复用情景并同步得到覆盖面 |
| GET | `/tasks/{id}/summary` `/grids` `/arrivals` `/witness` | 汇总、份额、节点到达、网格下钻 |
| POST | `/tasks/{id}/release` | 查看者离开（引用计数，不取消他人任务） |
| POST | `/events` | 临时封路/限速登记并即时失效受影响范围 |
| POST | `/cache/precompute` `/cache/rebuild` `/cache/invalidate` | 预计算、重建、按边失效 |
| POST | `/comparisons` | 共同分母对比与三因分解 |
| POST | `/publications` GET `/publications` | 结果发布与完整输入清单（含证据清单） |
| GET | `/sources/*` | 来源原文、事件审计、边修订、安置点、人口连接 |
| POST | `/evidence` | 证据接入（坐标/双时间/方向/阈值/误差/指纹） |
| POST | `/evidence/{id}/revise` `/backfill` `/withdraw` | 修正、补传、临时撤回（追加修订+定向失效） |
| GET | `/evidence` `/evidence/{id}` | 证据最新修订与单证据全修订/投影/审计 |
| GET | `/evidence/projections/list` | 投影解释（含拒绝原因与规则轨迹） |
| GET/POST | `/evidence/strategy` | 读取/切换融合策略（切换即失效全部缓存） |
| POST | `/evidence/fusion/run` | 按策略与截止点融合，物化 confirmed 派生封路 |
| GET | `/evidence/conclusions/list` `/runs/list` `/derived-closures/list` | 结论等级、批次指纹、派生封路 |
| GET | `/evidence/impact-logs/list` | 撤回/修正/补传与融合差异的影响审计 |
| GET | `/evidence/tasks/{id}/manifest` | 任务计算时固化的证据清单 |
| GET | `/evidence/tasks/{id}/grids/{grid}/drilldown` | 被排除路径 → 证据/结论/拒绝原因下钻 |
| POST | `/admin/reseed` `/admin/apply-c2-revision` | 样例数据管理 |
