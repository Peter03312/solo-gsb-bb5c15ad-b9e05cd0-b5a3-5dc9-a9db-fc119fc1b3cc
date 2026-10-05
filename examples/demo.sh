#!/usr/bin/env bash
# 端到端调用示例：录入作品 → 销售 → 退款 → 关账 → 查看报表 → 付款结算
#              → 作者分成版本变更 → 新版本销售/旧销售退款 → 退出作者结算
# 用法：BASE_URL=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
fmt() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1. 录入作品（两档阶梯 + 两位作者 60/40 分成）=="
curl -s -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{
  "work_id": "BOOK-1",
  "title": "示例作品",
  "tiers": [
    {"threshold": 0,    "rate": 0.08},
    {"threshold": 1000, "rate": 0.10}
  ],
  "authors": [
    {"author_id": "A1", "ratio": 60},
    {"author_id": "A2", "ratio": 40}
  ]
}' | fmt

echo "== 2. 登记销售 1200 册（跨档：1000 册 8% + 200 册 10%）=="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S100",
  "work_id": "BOOK-1",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200,
  "net_price": 33.33
}' | fmt

echo "== 3. 退款 250 册（从最后一册起冲回：200 册 10% 档 + 50 册 8% 档）=="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF100",
  "sale_id": "S100",
  "copies": 250
}' | fmt

echo "== 4. 关账，生成不可改报表 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-09"
}' | fmt

echo "== 5. 关账后补录旧业务流水（计入下一批）=="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S101",
  "work_id": "BOOK-1",
  "business_time": "2026-08-15T08:00:00",
  "copies": 5,
  "net_price": 20.00
}' | fmt

echo "== 6. 再次关账并查看两批报表 =="
curl -s -X POST "$BASE/closes" | fmt
curl -s "$BASE/closes" | fmt

echo "== 7. 财务确认作者付款（针对当前最新批次，即第 2 批）=="
LATEST_CLOSE=$(curl -s "$BASE/authors/A1/settlement" | python3 -c 'import sys,json; print(json.load(sys.stdin)["latest_close_id"])')
echo "当前最新关账批次：$LATEST_CLOSE"
# A1 第1批净额 = 200160（销售）- 47970（退款冲回）= 152190
# A1 第2批净额 = 5 册×200 分(10% 档)×60% = 600；可付额 = 152190 + 600 = 152790 分
curl -s -X POST "$BASE/payments" -H 'Content-Type: application/json' -d "{
  \"payment_id\": \"PAY-2026-09-A1\",
  \"author_id\": \"A1\",
  \"close_id\": $LATEST_CLOSE,
  \"amount_cents\": 100000
}" | fmt

echo "== 8. 同付款编号同内容重试（返回原结算结果，即使之后再关账也不变）=="
curl -s -X POST "$BASE/payments" -H 'Content-Type: application/json' -d "{
  \"payment_id\": \"PAY-2026-09-A1\",
  \"author_id\": \"A1\",
  \"close_id\": $LATEST_CLOSE,
  \"amount_cents\": 100000
}" | fmt

echo "== 9. 按作者查询：分批应计/已付/负余额/可付额/付款记录 =="
curl -s "$BASE/authors/A1/settlement" | fmt
curl -s "$BASE/payments/PAY-2026-09-A1" | fmt

echo "== 10. 合作关系变化：提交作者分成新版本（A2 退出、A3 加入；变更编号全局唯一）=="
curl -s -X POST "$BASE/works/BOOK-1/splits" -H 'Content-Type: application/json' -d '{
  "change_id": "CHG-2026-10-01",
  "authors": [
    {"author_id": "A1", "ratio": 70},
    {"author_id": "A3", "ratio": 30}
  ]
}' | fmt

echo "== 11. 查询当前及历次分成版本；作品详情展示当前分成 =="
curl -s "$BASE/works/BOOK-1/splits" | fmt
curl -s "$BASE/works/BOOK-1/splits/1" | fmt
curl -s "$BASE/works/BOOK-1" | fmt

echo "== 12. 新销售按入账时的分成版本（v2：A1 70 / A3 30）分账 =="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S102",
  "work_id": "BOOK-1",
  "business_time": "2026-10-01T09:00:00",
  "copies": 10,
  "net_price": 20.00
}' | fmt

echo "== 13. 旧销售 S100 的退款仍从末册冲回，并按原销售版本（v1：A1 60 / A2 40）分账 =="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF101",
  "sale_id": "S100",
  "copies": 10
}' | fmt

echo "== 14. 再次关账：退出作者 A2 仍可查询并结算既有余额，新作者 A3 参与关账与结算 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-10"
}' | fmt
curl -s "$BASE/authors/A2/settlement" | fmt
curl -s "$BASE/authors/A3/settlement" | fmt

echo "== 15. 销售净价录错：凭全局唯一更正编号对 S100 提交更正（33.33 → 40）=="
echo "    仅对各档未退册数、按原销售版本 v1 计算新旧每册版税差额（独立流水，不改历史）"
curl -s -X POST "$BASE/price-corrections" -H 'Content-Type: application/json' -d '{
  "correction_id": "COR-2026-10-001",
  "sale_id": "S100",
  "new_net_price": 40.00
}' | fmt

echo "== 16. 同更正编号同内容重试（200，返回原结果）；异内容/同价会被拒绝 =="
curl -s -o /dev/null -w 'replay status: %{http_code}\n' -X POST "$BASE/price-corrections" \
  -H 'Content-Type: application/json' -d '{
    "correction_id": "COR-2026-10-001", "sale_id": "S100", "new_net_price": 40.00
  }'
curl -s -o /dev/null -w 'same-price status: %{http_code} (409)\n' -X POST "$BASE/price-corrections" \
  -H 'Content-Type: application/json' -d '{
    "correction_id": "COR-2026-10-002", "sale_id": "S100", "new_net_price": 40.00
  }'

echo "== 17. 按更正编号查询 / 按原销售列出全部更正 =="
curl -s "$BASE/price-corrections/COR-2026-10-001" | fmt
curl -s "$BASE/price-corrections?sale_id=S100" | fmt

echo "== 18. 更正后退款：原价/原版本冲回字段不变，另附已生效价差反向流水 =="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "RF102",
  "sale_id": "S100",
  "copies": 100
}' | fmt

echo "== 19. 再次关账：更正与反向作为独立流水进入本批，历史批次与付款不改写 =="
curl -s -X POST "$BASE/closes" -H 'Content-Type: application/json' -d '{
  "request_id": "CLOSE-2026-11"
}' | fmt
