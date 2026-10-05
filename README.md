# 数字出版版税结算服务

基于 **Python / FastAPI + SQLite** 的版税结算服务：作品阶梯费率管理、作者分成版本管理、销售入账计提、作者分账、退款冲销、关账批次报表，支持幂等重试与并发安全。

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
- **销售净价更正**：财务凭**全局唯一更正编号、原销售编号、非负新每册净价**提交。按**原销售阶梯费率与原销售入账时的分成版本**，仅对更正时点**各档未退册数**计算新旧净价的每册版税差额；**逐作者差额 = 新净价下未退款总额的分账 − 旧净价下未退款总额的分账**（两种净价分别整额分账后相减，而非对差额分账）。更正以**独立流水**进入**下一关账批次**，**不改写**历史销售、退款、已关账报表和付款；销售详情新增 `current_net_price` 与 `price_corrections`，原 `net_price` 等历史字段不变。同一销售可多次更正（链式），每笔相对上一生效净价（首笔相对原净价）。**销售不存在返回 404；已全退、新价等于当前生效净价返回 400**；同更正编号同内容重试返回原结果（HTTP 200），异内容拒绝（HTTP 409，编号跨销售全局唯一）。
- **更正后的退款价差反向**：此后退款仍按原销售**末册、原价、原分成版本**冲回（原退款字段及金额不变），并对被退册数**另记已生效价差的反向流水**（退款响应中的 `price_diff_reversal` 字段）。该流水与退款**同一事务、入账序号相邻**，必随退款**归入同一关账批次**；服务内部按"分档×作者价差池"回拨，某档退净即该档池清零，**全退后该销售逐作者累计价差严格归零**。**旧退款不追溯**（更正之前的退款无反向流水）。
- **关账**：汇总上次关账之后到本次提交前的全部流水（按入账序号，含销售 / 退款 / 净价更正 / 退款价差反向），生成**逐笔明细 + 作者合计**的快照报表，落库后**不可修改**（仅提供只读接口）；关账后补录的旧业务流水自然进入下一批次。
- **付款结算（关账后）**：以作者编号、**当前最新关账批次**编号、付款编号、支付金额（分）提交。应计额 = 截至该批次全部**不可改关账快照**中该作者净额之和；已付额 = 该作者全部已确认付款之和；可付额 = `max(应计额 − 已付额, 0)`。支付金额必须为正且不超过可付额；作者/批次不存在返回 404，批次不是当前最新返回 409。
- **负余额（待抵扣）**：关账后退款若使应计额低于已付额，差额记为作者的待抵扣负余额，**后续关账的正额先抵扣**，旧报表与旧付款均不改写；负余额未清零时可付额为 0，不能付款。
- **幂等**：作品 / 分成变更 / 销售 / 退款 / **净价更正** / 付款以唯一编号为准——同号同内容重试返回原结果（HTTP 200），同号异内容拒绝（HTTP 409）；付款的同号同内容重试**即使之后又有关账也返回原结算结果**（响应为提交时刻的快照）。关账可携带 `request_id` 作为幂等键。
- **并发**：所有入账、分成变更、**净价更正**、关账与结算写操作在进程内串行化并在单事务中提交（`BEGIN IMMEDIATE`），版本生效、未退册数判定与批次归属由**数据库提交顺序**决定，并发变更/入账/更正/退款有确定先后，并发结算不会超付。

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
| POST | `/refunds` | 登记退款流水，返回冲销明细（按原销售版本分账；含更正后价差反向 `price_diff_reversal`） |
| GET | `/refunds/{refund_id}` | 查询退款流水 |
| POST | `/corrections` | 提交销售净价更正（独立流水；仅按各档未退册数计新旧净价差额，更正编号幂等） |
| GET | `/corrections/{correction_id}` | 按更正编号查询净价更正（各档/作者差额、关联销售与入账序号） |
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
# → split_version=1，splits 为 A1 / A2（60/40）

# 12. 再次关账后：退出作者 A2 仍可查询并结算既有余额，新作者 A3 参与关账与结算
curl -X POST http://localhost:8000/closes -H 'Content-Type: application/json' -d '{"request_id": "CLOSE-2026-10"}'
curl http://localhost:8000/authors/A2/settlement
curl http://localhost:8000/authors/A3/settlement

# 13. 净价更正：全局唯一更正编号 + 原销售编号 + 非负新每册净价
#     仅对更正时点各档未退册数、按原销售费率与入账时分成版本计差额；
#     逐作者差额为两种净价下未退款总额分别分账后相减；独立流水进入下一关账批次
curl -X POST http://localhost:8000/corrections -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "new_net_price": 40.00
}'
curl http://localhost:8000/corrections/COR-2026-10-001     # 按更正编号查询
# 同号同内容重试 → 200 原结果；异内容（含跨销售复用编号）→ 409
# 销售不存在 → 404；已全退 / 新价等于当前生效净价 → 400；负价 → 422

# 14. 更正后的退款：原退款仍按末册、原价、原版本冲回（金额字段不变），
#     另在 price_diff_reversal 中对被退册数记已生效价差的反向流水（随退款同批关账）
curl -X POST http://localhost:8000/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "RF102", "sale_id": "S100", "copies": 40
}'
# 全退后该销售逐作者累计价差（更正 splits + 各退款反向 splits）严格归零
```

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

净价更正响应示例（`POST /corrections`、`GET /corrections/{correction_id}`）：

```json
{
  "type": "correction",
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "entry_seq": 9,
  "old_net_price": "33.33",
  "new_net_price": "40",
  "split_version": 1,
  "segments": [
    {"segment_index": 0, "sale_segment_index": 0, "tier_index": 0, "rate": "0.08",
     "copies": 940, "old_per_copy_royalty_cents": 267,
     "new_per_copy_royalty_cents": 320, "diff_cents": 49820}
  ],
  "old_total_royalty_cents": 250980,
  "new_total_royalty_cents": 300800,
  "total_diff_cents": 49820,
  "splits": [
    {"author_id": "A1", "diff_cents": 29892},
    {"author_id": "A2", "diff_cents": 19928}
  ],
  "close_id": null,
  "created_at": "2026-10-05T09:00:00+00:00"
}
```

更正后退款响应中的价差反向字段（`POST/GET /refunds` 的 `price_diff_reversal`；销售从未更正时为 `null`）：

```json
{
  "refund_id": "RF102",
  "total_refund_cents": 10680,
  "splits": [{"author_id": "A1", "amount_cents": 6408}, {"author_id": "A2", "amount_cents": 4272}],
  "price_diff_reversal": {
    "entry_seq": 11,
    "sale_id": "S100",
    "total_diff_cents": -2120,
    "segments": [
      {"segment_index": 0, "sale_segment_index": 0, "copies": 40, "diff_cents": -2120}
    ],
    "splits": [
      {"author_id": "A1", "amount_cents": -1272},
      {"author_id": "A2", "amount_cents": -848}
    ],
    "close_id": null
  }
}
```

说明：关账报表的逐笔 `lines` 中，更正行 `type="correction"`（作者差额字段名为 `diff_cents`）、价差反向行 `type="price_diff_reversal"`；批次作者合计净额 = 销售 − 原退款 + 更正 + 价差反向；报表新增 `corrections_count`、`diff_reversals_count`、`total_price_correction_cents`、`total_price_diff_reversal_cents` 四个汇总字段，既有字段含义不变。

### 错误码（净价更正）

| 状态码 | 场景 |
| --- | --- |
| 400 | 原销售已全退（无未退册数）；新每册净价与当前生效净价相同 |
| 404 | 原销售（或更正编号查询）不存在 |
| 409 | 同更正编号异内容（含编号跨销售复用） |
| 422 | 请求体校验失败（如 `new_net_price < 0`） |

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

覆盖：阶梯计提、分账余分补齐、退款跨段冲回与超退拦截、幂等重试与冲突、关账批次归属与报表不可改、付款结算（过期批次拒绝/超付拦截/幂等重放/负余额抵扣）、并发入账、关账与结算，作者分成版本（校验/幂等与全局冲突/版本查询/入账版本/旧销售退款按原版本分账/退出作者结算/并发变更与入账/旧库迁移），以及销售净价更正（分档差额/分别分账后相减/仅未退册数/原版本/链式更正/校验与幂等/退款价差反向随批/全退逐作者价差归零/并发更正退款关账/旧库迁移）。

## 本轮变动（v1.3.0）

- **新增销售净价更正**：`POST /corrections` 提交（更正编号全局唯一、幂等），`GET /corrections/{correction_id}` 查询。按原销售阶梯费率与入账时分成版本、仅对更正时点各档未退册数计算新旧净价的每册版税差额；逐作者差额为两种净价下未退款总额分别整额分账后相减；响应返回各档与作者差额、关联销售与入账序号。
- **独立流水、不改历史**：更正（`entries.kind='correction'`）进入下一关账批次；历史销售、退款、已关账报表与付款一律不改写。销售响应新增 `current_net_price`、`price_corrections`（原 `net_price` 等字段不变）。
- **退款价差反向**：更正之后的退款仍按原销售末册、原价、原分成版本冲回（原字段金额不变），同事务另插 `entries.kind='price_diff_reversal'` 反向流水（入账序号相邻、随退款同批）；按"分档×作者价差池"回拨，分档退净清零，全退后该销售逐作者累计价差严格归零；更正之前的旧退款不追溯。
- **关账报表**：`lines` 增加 `correction` / `price_diff_reversal` 两类行，汇总新增 `corrections_count`、`diff_reversals_count`、`total_price_correction_cents`、`total_price_diff_reversal_cents`；`net_royalty_cents` 口径纳入更正与反向（旧批次无此类流水时数值与历史一致）。
- **校验与并发**：销售不存在 404，已全退或新价等于当前更正价 400，同号同内容重试 200、异内容 409；更正与退款、关账并发按数据库提交顺序判定未退册数与批次归属。
- **存储与迁移**：新增 `corrections` / `correction_segments` / `correction_splits` / `correction_segment_pools` / `refund_diff_reversals` / `refund_diff_reversal_segments` / `refund_diff_reversal_splits` 表；`entries` 的 CHECK 增加两类流水（旧库启动时自动重建该表并保留原序号），无需手工干预。

## 上一轮变动（v1.2.0）

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
tests/test_corrections.py
examples/demo.sh
Dockerfile / docker-compose.yml
```
