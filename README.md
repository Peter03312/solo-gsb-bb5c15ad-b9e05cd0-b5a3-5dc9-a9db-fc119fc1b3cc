# 数字出版版税结算服务

基于 **Python / FastAPI + SQLite** 的版税结算服务：作品阶梯费率管理、作者分成版本管理、销售入账计提、作者分账、退款冲销、**销售净价更正（价差补提/冲回）**、关账批次报表，支持幂等重试与并发安全。

## 快速启动

### Docker（推荐，一键启动并自动初始化数据库）

```bash
docker compose up --build
```

或直接使用 Dockerfile：

```bash
docker build -t royalties .
docker run -d -p 8000:8000 -v royalties-data:/data royalties
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库 schema 在服务启动时自动创建（幂等），无需手工初始化。

### 地址与端口

| 地址 | 说明 |
| --- | --- |
| `http://localhost:8000` | 服务入口 |
| `http://localhost:8000/docs` | Swagger 交互式 API 文档 |
| `http://localhost:8000/health` | 健康检查 |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/royalties.db`，本地 `./royalties.db` | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（Docker 启动命令使用） |

Compose 默认将宿主机 `8000` 映射到容器 `8000`，数据持久化在命名卷 `royalties-data`。

## 业务规则

- **作品**：录入递增的阶梯销量门槛（首档门槛必须为 0，严格递增）、非负费率（小数，如 `0.08` 表示 8%）、作者分成比例（百分数，合计必须恰好为 100）。
- **作者分成版本**：作品的作者分成可随合作关系变化。创建作品时的分成为**版本 1**；之后按 `作品编号 + 全局唯一变更编号 + 作者编号/比例` 提交**新版本**，版本号按提交顺序（数据库提交顺序）递增。作者编号不得重复、比例非负且合计必须为 100；作品不存在返回 404，数据不合法返回 422。同变更编号同内容重试返回原版本（HTTP 200），异内容拒绝（HTTP 409，变更编号**全局唯一**，跨作品复用也算冲突）。**版本按提交顺序生效：新销售使用入账时的当前版本分账；旧销售的后续退款仍从原销售末册冲回并按原销售版本分账；业务时间不决定版本。** 作品详情展示当前分成；作品创建的同号同内容重试仍返回**创建时结果**（版本 1 的分成）。退出分成的作者仍可查询并结算既有余额，新作者可参与后续关账与结算。
- **销售计提**：销售按**入账序号**（数据库提交顺序）累计册数，与业务时间无关；跨档时分档计提。每册版税 = 每册净价 × 该档费率，**四舍五入到分**；分档金额 = 每册版税 × 该档册数。入账时记录当时生效的分成版本（响应字段 `split_version`）。
- **作者分账**：对整笔计提金额，各作者按分成比例**先向下取整到分**，剩余分按**作者编号顺序**（纯数字编号按数值，其余按字典序）依次补 1 分。
- **退款**：从原销售**最后一册**起按原每册计提冲回，分账按**原销售入账时的分成版本**计算，**不回退累计销量**；同一销售的累计退款册数不得超过原册数（超出返回 400）。
- **净价更正**：销售净价录错后，财务凭**全局唯一更正编号**、原销售编号与非负新每册净价提交更正。系统按**原销售阶梯费率与入账时分成版本**，仅对各档**未退册数**计算新旧净价的每册版税差额（每册版税仍四舍五入到分）；**逐作者差额 = 未退款总额按新价整体分账 − 按旧价整体分账**（两个总额各自独立分账后相减，余分规则不变）。旧价取该销售的"当前更正价"（首次更正即原价），故可多次串联更正。更正以**独立流水**（独立入账序号）进入**下一关账批次**，**不改写**历史销售、退款、已关账报表与付款。同更正编号同内容重试返回原结果（200），异内容拒绝（409，**编号全局唯一**、跨销售复用也算冲突）；销售不存在 404、**已全退 400**、新价等于当前更正价 409。
- **更正后退款**：退款本身仍按原销售**末册、原价、原分成版本**冲回，**原有退款字段及金额不变**；同时对**被退册数**另记一条"**已生效价差反向流水**"（同一事务内独立入账序号，随退款归入**同一关账批次**）。各档反向额 =（当前更正价 − 原价）下该档每册价差 × 本次被退册数；逐作者反向额按"未退总额分账之差"**望远镜式**计算（退款前后两个时点各算一次新旧价分账差额再相减），因此**全退后该销售逐作者累计价差恰为 0**（与更正、退款的交错顺序无关）。无已生效价差的退款不产生反向流水（字段为 `null`）；**旧退款不追溯**。更正与退款、关账并发时，按**提交顺序**（数据库提交顺序）判定未退册数与批次归属。
- **关账**：汇总上次关账之后到本次提交前的全部流水（按入账序号），生成**逐笔明细 + 作者合计**的快照报表，落库后**不可修改**（仅提供只读接口）；关账后补录的旧业务流水自然进入下一批次。
- **付款结算（关账后）**：以作者编号、**当前最新关账批次**编号、付款编号、支付金额（分）提交。应计额 = 截至该批次全部**不可改关账快照**中该作者净额之和；已付额 = 该作者全部已确认付款之和；可付额 = `max(应计额 − 已付额, 0)`。支付金额必须为正且不超过可付额；作者/批次不存在返回 404，批次不是当前最新返回 409。
- **负余额（待抵扣）**：关账后退款若使应计额低于已付额，差额记为作者的待抵扣负余额，**后续关账的正额先抵扣**，旧报表与旧付款均不改写；负余额未清零时可付额为 0，不能付款。
- **幂等**：作品 / 分成变更 / 销售 / 退款 / **净价更正** / 付款以唯一编号为准——同号同内容重试返回原结果（HTTP 200），同号异内容拒绝（HTTP 409）；付款的同号同内容重试**即使之后又有关账也返回原结算结果**（响应为提交时刻的快照）。关账可携带 `request_id` 作为幂等键。
- **并发**：所有入账、分成变更、关账与结算写操作在进程内串行化并在单事务中提交（`BEGIN IMMEDIATE`），版本生效与批次归属由**数据库提交顺序**决定，并发变更与入账有确定先后，并发结算不会超付。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/works` | 录入作品（阶梯费率 + 初始作者分成，即版本 1） |
| GET | `/works/{work_id}` | 查询作品（展示**当前**分成版本） |
| GET | `/works/{work_id}/stats` | 作品销量汇总（累计/已退/净册数） |
| POST | `/works/{work_id}/splits` | 提交作者分成**新版本**（变更编号幂等，按提交顺序生效） |
| GET | `/works/{work_id}/splits` | 查询当前及历次分成版本 |
| GET | `/works/{work_id}/splits/{version}` | 查询指定分成版本 |
| POST | `/sales` | 登记销售流水，返回分档计提与分账明细（含入账时的 `split_version`） |
| GET | `/sales/{sale_id}` | 查询销售流水 |
| POST | `/refunds` | 登记退款流水，返回冲销明细（按原销售版本分账；若该销售已更正，另附被退册数价差反向流水） |
| GET | `/refunds/{refund_id}` | 查询退款流水 |
| POST | `/price-corrections` | 提交销售净价更正（全局唯一更正编号；仅对各档未退册数补提/冲回差额） |
| GET | `/price-corrections/{correction_id}` | 按更正编号查询更正流水 |
| GET | `/price-corrections` | 查询更正列表（可选 `?sale_id=` 按原销售过滤） |
| POST | `/closes` | 关账，生成不可改报表（可选 `request_id` 幂等键） |
| GET | `/closes` | 关账批次列表 |
| GET | `/closes/{close_id}` | 查询关账报表 |
| POST | `/payments` | 提交作者付款结算（仅当前最新关账批次） |
| GET | `/payments/{payment_id}` | 查询单笔付款结算 |
| GET | `/authors/{author_id}/settlement` | 作者结算总览：分批应计、已付、负余额、可付、付款记录 |

金额单位：响应中 `*_cents` 字段均为**分**；`net_price` 为元（字符串原样返回，避免浮点误差）。

## 调用示例

完整可运行脚本：`./examples/demo.sh`（需服务已启动）。

```bash
# 1. 录入作品：两档阶梯，两位作者 60/40 分成
curl -X POST http://localhost:8000/works -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-1",
  "title": "示例作品",
  "tiers": [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
  "authors": [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}]
}'

# 2. 登记销售 1200 册（跨档计提：1000 册×8% + 200 册×10%）
curl -X POST http://localhost:8000/sales -H 'Content-Type: application/json' -d '{
  "sale_id": "S100", "work_id": "BOOK-1",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200, "net_price": 33.33
}'
# → total_royalty_cents = 333600（1000×267 + 200×333）
# → splits: A1 200160 分，A2 133440 分

# 3. 退款 250 册（从最后一册起冲回：200 册×333 + 50 册×267 = 79950 分）
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF100", "sale_id": "S100", "copies": 250
}'

# 4. 关账，生成不可改报表（request_id 为幂等键，重试返回原报表）
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-09"
}'

# 5. 查看报表与批次列表
curl http://localhost:8000/closes/1
curl http://localhost:8000/closes

# 6. 财务确认作者实际付款（close_id 必须是当前最新批次）
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 100000
}'

# 7. 同付款编号同内容重试 → 原样返回；按作者查询结算总览
curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -d '{
  "payment_id": "PAY-2026-09-A1", "author_id": "A1", "close_id": 2, "amount_cents": 100000
}'
curl http://localhost:8000/authors/A1/settlement
curl http://localhost:8000/payments/PAY-2026-09-A1

# 8. 合作关系变化：提交作者分成新版本（A2 退出、A3 加入；变更编号全局唯一）
curl -X POST http://localhost:8000/works/BOOK-1/splits -H 'Content-Type: application/json' -d '{
  "change_id": "CHG-2026-10-01",
  "authors": [{"author_id": "A1", "ratio": 70}, {"author_id": "A3", "ratio": 30}]
}'
# → 201 {"work_id": "BOOK-1", "version": 2, "change_id": "CHG-2026-10-01", "authors": [...], ...}
#    同号同内容重试 → 200 返回原版本；同号异内容（含跨作品复用编号）→ 409

# 9. 查询当前及历次分成版本；作品详情展示当前分成
curl http://localhost:8000/works/BOOK-1/splits        # current_version=2，含版本 1/2 全量
curl http://localhost:8000/works/BOOK-1/splits/1      # 指定历史版本
curl http://localhost:8000/works/BOOK-1               # authors 为当前版本（A1 70 / A3 30）

# 10. 新销售按入账时的分成版本（v2）分账
curl -X POST http://localhost:8000/sales -H 'Content-Type: application/json' -d '{
  "sale_id": "S102", "work_id": "BOOK-1",
  "business_time": "2026-10-01T09:00:00",
  "copies": 10, "net_price": 20.00
}'
# → split_version=2，splits 为 A1 / A3

# 11. 旧销售 S100 的退款仍从末册冲回，并按原销售版本（v1）分账
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF101", "sale_id": "S100", "copies": 10
}'
# → split_version=1，splits 为 A1 / A2（60/40）；price_delta_reversal 为 null（未更正过，旧退款不追溯）

# 12. 再次关账后：退出作者 A2 仍可查询并结算既有余额，新作者 A3 参与关账与结算
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-2026-10"}'
curl http://localhost:8000/authors/A2/settlement
curl http://localhost:8000/authors/A3/settlement
```

### 净价更正调用示例

```bash
# 13. 销售净价录错：凭全局唯一更正编号 + 原销售编号 + 非负新每册净价更正
#     仅对各档未退册数，按原销售阶梯费率与入账时分成版本计算新旧每册版税差额
curl -X POST http://localhost:8000/price-corrections -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "new_net_price": 40.00
}'
# → 201：各档 old/new 每册版税、未退册数与差额，逐作者差额，total_delta_cents，
#        sale_entry_seq（原销售入账序号）、entry_seq（更正独立流水序号）、split_version

# 14. 同更正编号同内容重试 → 200 返回原结果；异内容 → 409
curl -X POST http://localhost:8000/price-corrections -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001", "sale_id": "S100", "new_net_price": 40.00
}'

# 15. 按更正编号查询；或按原销售列出全部更正（按入账序号）
curl http://localhost:8000/price-corrections/COR-2026-10-001
curl "http://localhost:8000/price-corrections?sale_id=S100"

# 16. 更正后再退款：原退款字段/金额不变（末册、原价、原版本），
#     响应另附 price_delta_reversal——被退册数已生效价差的反向流水（独立入账序号，同批次）
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF102", "sale_id": "S100", "copies": 100
}'
# → total_refund_cents 仍按原价；price_delta_reversal.splits 为带符号反向分账。
#    全退后该销售逐作者"更正 − 反向"累计价差恰为 0。

# 17. 再次关账：更正与反向作为独立流水进入本批，历史批次与历史付款均不改写
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-2026-11"}'
```

净价更正响应示例（`POST /price-corrections`；S100 已退 260 册，剩余 940 册在 8% 档）：

```json
{
  "type": "price_correction",
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "work_id": "BOOK-1",
  "sale_entry_seq": 1,
  "entry_seq": 8,
  "old_net_price": "33.33",
  "new_net_price": "40",
  "split_version": 1,
  "segments": [
    {"segment_index": 0, "sale_segment_index": 0, "tier_index": 0, "rate": "0.08",
     "unrefunded_copies": 940,
     "old_per_copy_royalty_cents": 267, "new_per_copy_royalty_cents": 320,
     "delta_per_copy_cents": 53, "amount_cents": 49820}
  ],
  "splits": [
    {"author_id": "A1", "amount_cents": 29892},
    {"author_id": "A2", "amount_cents": 19928}
  ],
  "total_delta_cents": 49820,
  "close_id": null,
  "created_at": "2026-10-05T09:00:00+00:00"
}
```

退款响应中的价差反向流水（`POST /refunds` 的 `price_delta_reversal` 字段；未更正或旧退款为 `null`；下例为 S100 更正后再退 100 册，均落在剩余的 8% 档）：

```json
{
  "refund_id": "RF102",
  "sale_id": "S100",
  "total_refund_cents": 26700,
  "splits": [{"author_id": "A1", "amount_cents": 16020}, {"author_id": "A2", "amount_cents": 10680}],
  "price_delta_reversal": {
    "type": "price_delta_reversal",
    "refund_id": "RF102",
    "sale_id": "S100",
    "entry_seq": 8,
    "split_version": 1,
    "segments": [
      {"segment_index": 0, "sale_segment_index": 0, "tier_index": 0, "rate": "0.08",
       "copies": 100,
       "current_per_copy_royalty_cents": 320, "original_per_copy_royalty_cents": 267,
       "delta_per_copy_cents": 53, "amount_cents": 5300}
    ],
    "splits": [
      {"author_id": "A1", "amount_cents": 3180},
      {"author_id": "A2", "amount_cents": 2120}
    ],
    "total_reversal_cents": 5300
  }
}
```

关账报表新增（既有字段与口径不变）：`price_corrections_count`、`price_delta_reversals_count`、
`total_price_correction_cents`（带符号：补提为正/冲回为负）、`total_price_delta_reversal_cents`（带符号）、
`total_author_net_cents`（含更正与反向的作者净分账合计）。`lines` 中新增
`"type": "price_correction"` 与 `"type": "price_delta_reversal"` 两类逐笔明细。

### 错误码（净价更正）

| 状态码 | 场景 |
| --- | --- |
| 400 | 原销售已全部退款（无未退册数可更正） |
| 404 | 原销售不存在（列表按不存在的 `sale_id` 过滤亦为 404） |
| 409 | 同更正编号异内容（含跨销售复用编号）；或新价等于当前更正价 |
| 422 | 请求体校验失败（如 `new_net_price < 0`） |

付款结算响应示例：

```json
{
  "payment_id": "PAY-2026-09-A1",
  "author_id": "A1",
  "close_id": 2,
  "amount_cents": 100000,
  "accrued_cents": 152790,
  "paid_before_cents": 0,
  "payable_cents": 152790,
  "paid_after_cents": 100000,
  "negative_balance_cents": 0,
  "created_at": "2026-10-04T12:00:00+00:00"
}
```

作者结算总览响应（`GET /authors/{author_id}/settlement`）：

```json
{
  "author_id": "A1",
  "latest_close_id": 2,
  "accrued_cents": 152790,
  "paid_cents": 100000,
  "negative_balance_cents": 0,
  "payable_cents": 52790,
  "batches": [
    {"close_id": 1, "batch_net_cents": 152190, "accrued_cents": 152190, "created_at": "..."},
    {"close_id": 2, "batch_net_cents": 600,    "accrued_cents": 152790, "created_at": "..."}
  ],
  "payments": [
    {"payment_id": "PAY-2026-09-A1", "close_id": 2, "amount_cents": 100000,
     "accrued_cents": 152790, "paid_before_cents": 0, "payable_cents": 152790, "created_at": "..."}
  ]
}
```

字段说明：`batches[].batch_net_cents` 为该批次快照中作者净额（退款可为负），`accrued_cents` 为截至该批次的累计应计；顶层 `negative_balance_cents = max(paid − accrued, 0)`，`payable_cents = max(accrued − paid, 0)`。

### 错误码（付款结算）

| 状态码 | 场景 |
| --- | --- |
| 400 | 支付金额超过可付额（含可付额为 0 / 存在待抵扣负余额） |
| 404 | 作者或关账批次不存在 |
| 409 | 批次不是当前最新；或同付款编号异内容重试 |
| 422 | 请求体校验失败（如 `amount_cents ≤ 0`） |

响应示例（销售入账，节选）：

```json
{
  "sale_id": "S100",
  "entry_seq": 1,
  "cum_before": 0,
  "cum_after": 1200,
  "split_version": 1,
  "segments": [
    {"tier_index": 0, "rate": "0.08", "copies": 1000, "per_copy_royalty_cents": 267, "amount_cents": 267000},
    {"tier_index": 1, "rate": "0.1",  "copies": 200,  "per_copy_royalty_cents": 333, "amount_cents": 66600}
  ],
  "total_royalty_cents": 333600,
  "splits": [
    {"author_id": "A1", "amount_cents": 200160},
    {"author_id": "A2", "amount_cents": 133440}
  ],
  "close_id": null
}
```

分成版本响应示例（`POST /works/{work_id}/splits`、`GET /works/{work_id}/splits/{version}`）：

```json
{
  "work_id": "BOOK-1",
  "version": 2,
  "change_id": "CHG-2026-10-01",
  "authors": [
    {"author_id": "A1", "ratio": "70"},
    {"author_id": "A3", "ratio": "30"}
  ],
  "created_at": "2026-10-05T08:00:00+00:00"
}
```

### 错误码（分成版本变更）

| 状态码 | 场景 |
| --- | --- |
| 404 | 作品不存在 |
| 409 | 同变更编号异内容（含变更编号跨作品复用） |
| 422 | 请求体校验失败（作者编号重复 / 比例为负 / 合计不为 100 / 空作者列表） |

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：阶梯计提、分账余分补齐、退款跨段冲回与超退拦截、幂等重试与冲突、关账批次归属与报表不可改、付款结算（过期批次拒绝/超付拦截/幂等重放/负余额抵扣）、并发入账、关账与结算，作者分成版本（校验/幂等与全局冲突/版本查询/入账版本/旧销售退款按原版本分账/退出作者结算/并发变更与入账/旧库迁移），以及净价更正（各档未退册数差额与独立分账相减/正负差额/幂等与全局冲突/销售不存在·已全退·同价拒绝/更正后退款的价差反向流水与原字段不变/串联更正与全退逐作者归零/更正与反向进入下一批次且历史不改写/按更正编号查询/并发提交顺序/旧库迁移新流水类型）。

## 本轮变动（v1.3.0）

- **新增销售净价更正**：`POST /price-corrections`（更正编号全局唯一、幂等），`GET /price-corrections/{correction_id}` 按更正编号查询，`GET /price-corrections` 列表（可 `?sale_id=` 过滤）。
- **差额口径**：按**原销售阶梯费率与入账时分成版本**，仅对各档**未退册数**计算新旧每册净价的版税差额（四舍五入到分）；逐作者差额为"新价未退总额分账 − 旧价未退总额分账"（两个总额各自整体分账后相减）。旧价取当前更正价，支持多次串联更正。
- **独立流水、不改历史**：更正以独立入账序号进入**下一关账批次**；历史销售、退款、已关账报表与付款一律不改写。
- **更正后退款**：退款仍按原销售**末册、原价、原分成版本**冲回，**原退款字段及金额不变**；对被退册数另记"已生效价差反向流水"（同事务独立序号、随退款同批次），逐作者按望远镜式差额冲回，**全退后该销售逐作者累计价差归零**；旧退款不追溯。
- **拒绝规则**：销售不存在 404、已全退 400、新价等于当前更正价 409、同号异内容（含跨销售）409、非负校验 422。
- **并发**：更正与退款、关账共用同一进程锁与 `BEGIN IMMEDIATE` 单事务，未退册数与批次归属按数据库提交顺序判定。
- **报表字段（均为新增，既有字段与状态码不变）**：关账报表新增 `price_corrections_count` / `price_delta_reversals_count` / `total_price_correction_cents` / `total_price_delta_reversal_cents` / `total_author_net_cents`；`lines` 新增 `price_correction`、`price_delta_reversal` 两类明细；退款响应新增可空字段 `price_delta_reversal`。
- **存储与迁移**：新增 `price_corrections` / `price_correction_segments` / `price_correction_splits` / `price_delta_reversals` / `price_delta_reversal_segments` / `price_delta_reversal_splits` 表；启动时自动迁移旧库——重建 `entries` 表将 CHECK 约束扩展为含两种新流水类型（保留历史入账序号），无需手工干预。启动方式（`uvicorn app.main:app` / Docker）不变。

## 历史变动（v1.2.0）

- **新增作者分成版本**：`POST /works/{work_id}/splits` 提交新版本（`change_id` 全局唯一幂等），`GET /works/{work_id}/splits`、`GET /works/{work_id}/splits/{version}` 查询当前及历次版本；创建作品时的分成为版本 1。
- **版本生效规则**：新销售按入账时的当前版本分账；旧销售的退款仍从末册冲回并按原销售版本分账；业务时间不决定版本；并发变更与入账由数据库提交顺序确定先后。
- **响应新增字段（均为新增，既有字段与状态码不变）**：作品响应 `split_version`（所展示分成的版本号；`GET` 为当前版本，创建及其重试恒为 1）；销售/退款响应 `split_version`（计提/冲回所用的分成版本）。
- **作者结算**：退出分成的作者仍可查询并结算既有余额；新版本加入的作者可参与后续关账与结算；已关账快照与付款记录不改写。
- **存储与迁移**：新增 `work_versions` / `work_version_authors` 表，`sales` 增加 `split_version` 列；启动时自动迁移旧库——既有作品的创建时分成迁为版本 1，历史销售回填 `split_version = 1`，无需手工干预。

## 项目结构

```
app/
  main.py     # FastAPI 路由
  ledger.py   # 核心账务逻辑（计提/分账/退款/关账）
  db.py       # SQLite 连接与 schema 初始化
  schemas.py  # 请求模型与校验
tests/test_api.py
examples/demo.sh
Dockerfile / docker-compose.yml
```
