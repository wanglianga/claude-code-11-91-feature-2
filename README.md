# 充电预约 + 运维工单一体化演示（Vue 3 + FastAPI + PostgreSQL）

司机按「站点 + 时间窗 + 停留时长 + 最低功率」预约充电；运营方维护枪口并评估对既有预约的影响。
系统同时支持两种预约粒度，并以同一套容量数据给出可核验结论：

- **固定枪口（CONNECTOR）**：确认时锁定一支兼容枪口，位置承诺直观；枪口故障时需迁移订单。
- **资源池（POOL）**：只承诺一个满足插头/功率/无障碍条件的站点名额，到站再分配枪口；
  以「池内同时可履约的枪口数」限制订单，绝不会因为两支枪各自「空闲」就把同一份容量卖两次。

**Feature-2 新增（运维闭环）**：把**预防工单、告警工单、设备拓扑、备件批次、持证技师排班**接进运营端，
工单完整记录**故障证据、风险等级、最迟完成时点、隔离范围、作业步骤、验收读数、签字人**；
支持「合并停电窗口」与「分开作业」两种排程的安全校验与影响对比，处理**延期 / 验收失败 / 预约取消**
三类回执并重算可售容量、候选替代与通知；未通过绝缘与负载测试的设备绝不允许恢复可售。

## 一、目录结构

```
.
├── docker-compose.yml          # 一键启动 db + backend + frontend
├── .gitattributes              # 强制 *.sh 为 LF
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── e2e_check.sh            # 基线预约场景 E2E（16 项检查）
│   ├── e2e_workorders.sh       # Feature-2 工单/排程/回执 E2E（37 项检查）
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（含 /docs Swagger UI）
│   │   ├── models.py           # +设备拓扑/技师/备件批次/工单/排程留痕/通知
│   │   ├── enums.py            # +工单状态/风险/设备状态/方案/阈值常量
│   │   ├── schemas.py          # +排程/验收/签字/研判/回执请求模型
│   │   ├── seed.py             # 东站 A/B/C/D + 拓扑 + 两工单 + 技师备件
│   │   ├── bootstrap.py        # 容器启动：建表，空库才灌种子（可重复执行）
│   │   ├── services/
│   │   │   ├── capacity.py     # 时间切片容量引擎（含设备状态硬闸门）
│   │   │   ├── reservation.py  # 确认/取消/迟到/到站/开始充电 状态机
│   │   │   ├── maintenance.py   # 降额/停用/恢复 + 影响判定 + 客服动作
│   │   │   └── workorders.py   # Feature-2：拓扑/校验/方案对比/回执/验收/通知
│   │   └── routers/
│   │       └── workorders.py   # Feature-2 REST 接口
│   └── tests/
│       ├── test_scenarios.py    # 20 个基线场景
│       └── test_workorders.py   # 12 个 Feature-2 场景（合计 32）
└── frontend/
    └── src/views/
        ├── DriverView.vue        # 司机端
        ├── OperationsView.vue    # 运营端（含「工单/拓扑/排程」子页签）
        └── MaintenanceView.vue   # Feature-2 工单运维控制台（也有独立 #/maintenance 路由）
```

## 二、一键启动（Docker，推荐）

前置：本机已安装 Docker 与 Docker Compose。

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| 前端（司机端 / 运营端 / 运维工单页签） | http://localhost:8080 |
| 后端 API | http://localhost:8000 |
| Swagger UI（接口文档） | http://localhost:8000/docs |
| PostgreSQL | localhost:5432，库/用户/密码均为 `ccdev` |

后端容器启动时会自动建表，并**仅在空库时**灌入样例数据；重复重启不会清空已有数据。
需要恢复初始演示数据时：

```bash
docker compose exec backend python -m app.seed
```

停止并清理数据卷（会清掉旧表结构，适合跨版本升级后重建）：

```bash
docker compose down -v
```

> 若此前用旧版本启动过、保留了 `pgdata` 数据卷，新增字段/表不会自动迁移，请用上面的
> `down -v` 重建，或在 backend 容器内执行 `python -m app.seed`（会 drop/create 全部表）。

## 三、本地开发启动（不使用 Docker）

容器内已预装 Python 3.12 / Node 22 / PostgreSQL 18，下面的命令也适用于普通开发机
（自行准备一个可连接的 PostgreSQL 即可）。

### 1. 后端

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 指向你的 PostgreSQL，默认 postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev
export DATABASE_URL="postgresql+psycopg://用户:密码@主机:5432/库名"
.venv/bin/python -m app.seed           # 重置并灌入样例数据
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev      # http://localhost:8080，/api 已代理到 127.0.0.1:8000
```

> 若 Windows 检出导致 `e2e_*.sh` 变成 CRLF，运行前执行 `sed -i 's/\r$//' e2e_check.sh e2e_workorders.sh`；
> 仓库已通过 `.gitattributes` 强制 `*.sh` 使用 LF。

## 四、测试数据与「账号」说明

本演示系统不设登录认证（无账号体系），打开页面即可操作；下列固定种子身份相当于测试入口：

- **站点**：东站充电站（id=1），四支枪口
  - **A**：CCS / 120kW / 无障碍 —— 已被有效预约 `R-SEEDA01` 在周五 18:00–18:30 占用
  - **B**：CCS / 120kW / 无障碍 —— 18:00 时段唯一剩余的 90kW 合格枪；周四绝缘读数 **6MΩ（临界，未达 5MΩ 强制停机线）**
  - **C**：CCS / **60kW** / 普通车位 —— 功率不足 90kW，但可承接 60kW 需求
  - **D**：**CHAdeMO** / 120kW —— 与 CCS 车辆接口不兼容
- **车辆**：V1 120kW（A 枪单）、V2 最大受电 60kW、V3 无障碍 100kW、V4 150kW（发起 90kW 请求）
- **设备拓扑**：`PANEL-EAST → FEEDER-BC → BR-BC（B/C 共用断路器，温升 78℃ 告警）→ ISO-B / ISO-C → B / C`；
  隔离 **BR-BC = B、C 同时断电**，隔离 **ISO-B = 只停 B、保留 C**
- **工单**：
  - `WO-B-INS`（PREVENTIVE 预防）：B 绝缘临界换 CCS 接头，计划 17:40–18:10，最迟 18:30
  - `WO-BC-BRKR`（ALARM 告警）：共用断路器温升检查，需 B、C 同时断电，核心检查 20 分钟，最迟 18:30
- **技师**：id=1 王建国，唯一持 `HV_ELECTRICAL + CONNECTOR_REPLACEMENT + BREAKER_INSPECTION + INSULATION_TEST`
  全证，周五班次 **08:00–18:30**；id=2 学徒仅持高压证（资质不足），班次到 22:00
- **备件**：`CONN-CCS-120` 接头总成；合格批次 `LOT-2026-11-A` **仅 1 件**，另有冻结批次 `LOT-2025-Q4-X`（2 件但不可领用）
- **签字人**：演示默认填「王建国」，可在验收签字框改成任意值班主管姓名
- 所有演示时间固定在 **2026-11-27（周五）**，前端表单已预填 18:00 等时刻

## 五、本题（Feature-2）验证方式

### 方式 A：自动化场景测试（推荐，最快）

```bash
cd backend
.venv/bin/python -m pytest tests/ -q
# 实际结果：32 passed（基线 20 + 工单特性 12）
```

新增的 `tests/test_workorders.py` 覆盖：拓扑隔离范围、工单档案七要素、待排程仍可售、
合并 vs 分开的影响差异、四类不安全排程 422 且无副作用、延期/测试失败/取消三回执、
验收通过+冷却合规+签字恢复、冷却不足判无效、强制停机阈值拦截、候选替代必须显式确认、
合并排程 C 退出与幂等重放不重复扣库存。

### 方式 B：真实 HTTP E2E（37 项）

先重置数据并启动后端，再运行：

```bash
cd backend
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --port 8000 &
bash e2e_workorders.sh
# 实际结果：通过 37 项，失败 0 项
```

脚本用真实 HTTP 请求核对：拓扑、工单档案、两种方案的订单判定、资质/批次/冷却/班次拒绝、
备件扣减、候选 `confirmed=false`、延期两条安全冲突、测试失败后 B 为 `ACCEPTANCE_FAILED`
且 90kW 容量清空、失败后签字 409、取消回执、六类通知、复测合格签字后 B 恢复 `OPERATIONAL`、
绝缘恶化到 2MΩ 后强制停机并对 90kW 下单返回 409。

基线预约能力未被破坏：

```bash
bash e2e_check.sh
# 实际结果：通过 16 项，失败 0 项
```

### 方式 C：前端手工验证

`npm run dev` 后访问 http://localhost:8080 → 顶部「运营端」→ 页内第二个页签
**「🛠 预防/告警工单 · 拓扑 · 备件 · 持证技师 · 停电方案对比」**
（也可直接打开顶部「运维工单/停电排程」或 `/#/maintenance`）：

1. 顶部选择技师/备件批次/冷却分钟，点「重新计算两种方案影响」。
2. 对比三张方案卡：
   - **合并 17:40–18:30**：校验通过；若已建 B 的 90kW 单与 C 的 60kW 单，两者都判 `NEEDS_RESELECTION`，高峰 90/60 均无可售枪。
   - **分开·B_ONLY 17:40–18:10**：校验通过；B 单改选、**C 单继续履约（60kW 保留）**，B 在 18:10 后恢复 90kW。
   - **分开·今晚连做断路器 18:10–18:40**：红色「不可安全排程」，列出 `TECH_SHIFT/DEADLINE`；点「尝试排程」收到 422。
   - 把技师换成学徒、批次换成冻结批次、冷却改成 2，再采纳都会被拒绝并列出原因。
3. 先在「司机端」或 API 建一笔 B 的 18:00–18:30 / 90kW 单（和一笔 C 的 60kW 单），再回到方案对比即可看到订单影响。
4. 采纳「分开·B_ONLY」→ 容量看板中 B 变「已排程」、合格备件变 0、通知出现；订单变「需改选」。
5. 在「② 三类外部回执」依次点：**延期 +25**（提示击穿 18:30 班次/截止）→
   **测试失败回执**（B 变「验收失败·停运」，看板对应时间片清空，自动生成返工工单）→
   选择 B 单点**预约取消回执**。
6. 在通知列表核对 `WORK_DELAYED / SCHEDULE_BREACH / ACCEPTANCE_FAILED / REWORK_CREATED / CANCEL_RECEIPT`。
7. 点「测试合格回执」→ 在工单卡点「签字并恢复设备」，B 回到「正常可售」，18:10 后 90kW 重新出现。
8. 在「③」对订单点「候选替代」：所有建议都带 **「候选·未确认」** 标记，不会改写订单；
   取消占用 A 的 `R-SEEDA01` 后，候选出现 A，点「执行客服改派并确认」后订单才真正 `CONFIRMED` 到 A。
9. 点「模拟恶化到 2.0（强制停机）」，B 立即 `FORCED_OUT`，司机端无法再就 18:00 履约卖出 B。

## 六、Feature-2 主要接口（均在 `/docs` 可见可试）

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/topology?site_id=1` | 设备拓扑树 + 每个节点的隔离影响枪 |
| `GET /api/technicians` / `GET /api/spare-parts` | 持证技师班次 / 备件型号与批次库存 |
| `GET /api/work-orders` / `GET /api/work-orders/{id}` | 工单档案（证据/风险/截止/隔离/步骤/验收/签字） |
| `POST /api/work-orders/compare` | 三方案 dry-run：校验项、违规、影响、回算容量、候选（不落库） |
| `POST /api/work-orders/schedule` | 采纳并排程；不安全返回 422 且无副作用（幂等） |
| `POST /api/work-orders/{id}/start` | 挂牌开工，隔离范围内设备置 `ISOLATED` |
| `POST /api/work-orders/{id}/delay` | 延期，延长窗口、重算影响/容量、识别班次截止冲突并通知 |
| `POST /api/work-orders/{id}/acceptance` | 录入验收读数，校验绝缘≥50MΩ、负载≥90kW、冷却≥5 分钟 |
| `POST /api/work-orders/{id}/signoff` | 合格后签字恢复；未通过验收返回 409 |
| `POST /api/work-orders/triage-reading` | 绝缘读数研判；<5MΩ 立即 `FORCED_OUT` 并建停用窗口 |
| `POST /api/receipts` | 统一三类回执：`WORK_ORDER_DELAYED` / `ACCEPTANCE_FAILED_RECEIPT` / `RESERVATION_CANCELLED` |
| `GET /api/reservations/{id}/alternatives` | 该单的候选替代（恒 `confirmed=false`，仅建议） |
| `GET /api/ops/board` | 设备状态 + 高峰/逐 10 分钟时间片可售枪与池名额 |
| `GET /api/notifications` | 已发通知留痕 |

## 七、关键设计与约束的落实

- **候选搜索不构成订单**：`POST /api/search` 为纯只读；真正占用容量只发生在确认事务里。
- **事务内重算 + 并发安全**：确认先取站点级 `pg_advisory_xact_lock`，在同一事务内重算重叠切片后落单。
- **池容量不重复售卖**：每切片统计「合格且未锁定的枪数 − 在途池预约数」，全程取最小值。
- **幂等**：每个写操作携带 `idempotency_key`；首次请求（含 409/422 业务失败）的响应码与响应体被持久化，重复提交原样返回，同键不同参数返回 409。
- **状态机**：`CONFIRMED → ARRIVED → CHARGING → COMPLETED`，以及 `CANCELLED / NO_SHOW`；
  受维护影响可进入 `NEEDS_RESELECTION / MANUAL_CONTACT`，这两类单也允许取消；非法转换 409。
- **维护按真实交集判定**：半开区间求交，交集为空完全不影响；恢复不改写历史快照、不自动回滚订单状态。
- **充电会话保护**：`CHARGING` 订单不能被取消或迁移，维护只给 `PROTECTED_CHARGING`。
- **历史不可改写**：确认快照、客服动作时的订单/设备快照均固化。
- **安全排程四校验 + 截止时点**：资质（所需证书并集）、班次边界、备件**合格批次**与数量、
  拓扑隔离依赖（断路器必须 B+C）、分开窗口不得重叠、负载后**最短冷却 5 分钟**、最迟完成时点。
- **容量状态硬闸门**：`ISOLATED / PENDING_ACCEPTANCE / ACCEPTANCE_FAILED / FORCED_OUT`
  的设备在容量引擎中任何时间片功率直接置 0；验收失败会把停用窗口延长到当日运营结束，
  签字恢复前不可能被搜索、确认或客服改派当成合格履约枪。
- **候选 ≠ 已确认**：替代建议全部带 `confirmed=false`，不写订单、不锁容量；
  只有显式调用客服替代动作并通过同一套容量校验后，订单才迁移并确认。

## 八、本次实际验证结论（仅记录实际执行过的内容）

在交付容器内（Python 3.12.10、Node 22.14.0、PostgreSQL 18 本机 `ccdev` 库）实际执行：

- `.venv/bin/python -m pytest tests/ -q` → **32 passed**。
- 重置种子并真实启动 `uvicorn app.main:app --port 8000` 后：
  - `bash e2e_check.sh` → **通过 16 项，失败 0 项**；
  - `bash e2e_workorders.sh` → **通过 37 项，失败 0 项**；
  - `GET /openapi.json` → 200，Feature-2 的 12 个路径正常生成文档；
  - 前端 `npm install`（临时 cache 绕过宿主 root 属主缓存）与 `npm run build` → **构建成功**。
- `docker` / `docker compose` 在当前创作环境中未安装/无守护进程，因此**未在此环境实际执行
  `docker compose up --build`**；已校验 `docker-compose.yml` YAML 合法，Dockerfile 与 nginx 代理
  配置沿用并保持完整，一键启动命令以第二节为准，待评测/部署环境实际构建运行。
