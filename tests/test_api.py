"""端到端 API 测试：阶梯计提、分账舍入、退款冲销、关账批次、幂等与并发。"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client():
    db.open_database(":memory:")
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def make_work(client, work_id="W1", tiers=None, authors=None):
    body = {
        "work_id": work_id,
        "title": "测试作品",
        "tiers": tiers
        or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
        "authors": authors
        or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
    }
    return client.post("/works", json=body)


def make_sale(client, sale_id, work_id="W1", copies=1, price="33.33", ts="2026-09-01T10:00:00"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id,
            "work_id": work_id,
            "business_time": ts,
            "copies": copies,
            "net_price": price,
        },
    )


def correct_price(client, correction_id, sale_id, new_price):
    return client.post(
        "/price-corrections",
        json={"correction_id": correction_id, "sale_id": sale_id, "new_net_price": new_price},
    )


# ---------------------------------------------------------------- 作品校验


def test_work_ratio_sum_must_be_100(client):
    r = make_work(client, authors=[{"author_id": "A1", "ratio": 60}])
    assert r.status_code == 422


def test_work_thresholds_must_increase(client):
    r = make_work(client, tiers=[{"threshold": 0, "rate": 0.08}, {"threshold": 0, "rate": 0.1}])
    assert r.status_code == 422


def test_work_first_threshold_must_be_zero(client):
    r = make_work(client, tiers=[{"threshold": 100, "rate": 0.08}])
    assert r.status_code == 422


def test_work_idempotent_replay_and_conflict(client):
    assert make_work(client).status_code == 201
    r = make_work(client)
    assert r.status_code == 200  # 同号同内容 → 返回原结果
    assert r.json()["work_id"] == "W1"
    r = make_work(client, authors=[{"author_id": "A1", "ratio": 100}])
    assert r.status_code == 409  # 同号异内容 → 拒绝


# ---------------------------------------------------------------- 阶梯计提与分账


def test_sale_royalty_across_tiers(client):
    make_work(client)
    r = make_sale(client, "S1", copies=800)
    assert r.status_code == 201
    body = r.json()
    # 800 册全部落在第一档：33.33*0.08=2.6664 → 每册 267 分
    assert body["cum_before"] == 0 and body["cum_after"] == 800
    assert body["segments"] == [
        {
            "segment_index": 0,
            "tier_index": 0,
            "rate": "0.08",
            "copies": 800,
            "per_copy_royalty_cents": 267,
            "amount_cents": 213600,
            "refunded_copies": 0,
        }
    ]
    assert body["total_royalty_cents"] == 213600
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 128160},
        {"author_id": "A2", "amount_cents": 85440},
    ]

    # 跨档：累计销量 800→1200，前 200 册 8%，后 200 册 10%（每册 333 分）
    r = make_sale(client, "S2", copies=400)
    body = r.json()
    assert body["cum_before"] == 800 and body["cum_after"] == 1200
    assert [(s["tier_index"], s["copies"], s["per_copy_royalty_cents"]) for s in body["segments"]] == [
        (0, 200, 267),
        (1, 200, 333),
    ]
    assert body["total_royalty_cents"] == 200 * 267 + 200 * 333
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 72000},
        {"author_id": "A2", "amount_cents": 48000},
    ]


def test_split_remainder_filled_by_author_order(client):
    make_work(
        client,
        work_id="W2",
        tiers=[{"threshold": 0, "rate": 0.07}],
        authors=[
            {"author_id": "A3", "ratio": 33.34},
            {"author_id": "A1", "ratio": 33.33},
            {"author_id": "A2", "ratio": 33.33},
        ],
    )
    r = make_sale(client, "S9", work_id="W2", copies=1, price="10.00")
    body = r.json()
    assert body["total_royalty_cents"] == 70  # 10*0.07=0.70
    # 70*33.33%=23.31→23，70*33.34%=23.38→23，余 1 分按作者编号顺序补 A1
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 24},
        {"author_id": "A2", "amount_cents": 23},
        {"author_id": "A3", "amount_cents": 23},
    ]


def test_sale_idempotency(client):
    make_work(client)
    r1 = make_sale(client, "S1", copies=10)
    assert r1.status_code == 201
    r2 = make_sale(client, "S1", copies=10)
    assert r2.status_code == 200
    assert r1.json() == r2.json()  # 同号同内容 → 原结果
    r3 = make_sale(client, "S1", copies=11)
    assert r3.status_code == 409  # 同号异内容 → 拒绝


def test_sale_unknown_work_404(client):
    r = make_sale(client, "S1", work_id="NOPE")
    assert r.status_code == 404


# ---------------------------------------------------------------- 退款


def test_refund_reverses_from_last_copy(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)  # 200 册@267 + 200 册@333

    # 退 150 册：全部来自最后一档（每册 333 分）
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 150})
    assert r.status_code == 201
    body = r.json()
    assert body["total_refund_cents"] == 150 * 333
    assert [(s["sale_segment_index"], s["copies"]) for s in body["segments"]] == [(1, 150)]
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 29970},
        {"author_id": "A2", "amount_cents": 19980},
    ]

    # 再退 100 册：最后一档剩 50 册，继续冲倒数第二档 50 册
    r = client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 100})
    body = r.json()
    assert body["total_refund_cents"] == 50 * 333 + 50 * 267
    assert [(s["sale_segment_index"], s["copies"]) for s in body["segments"]] == [(0, 50), (1, 50)]

    # 累计销量不回退
    sale = client.get("/sales/S2").json()
    assert sale["cum_after"] == 1200
    assert sale["refunded_copies"] == 250

    # 累计退款不得超过原册数：已退 250，再退 200 → 450 > 400
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S2", "copies": 200})
    assert r.status_code == 400
    # 退满 400 册允许
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S2", "copies": 150})
    assert r.status_code == 201


def test_refund_idempotency_and_unknown_sale(client):
    make_work(client)
    make_sale(client, "S1", copies=10)
    r1 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r1.status_code == 201
    r2 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    r3 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 4})
    assert r3.status_code == 409
    r4 = client.post("/refunds", json={"refund_id": "R9", "sale_id": "NOPE", "copies": 1})
    assert r4.status_code == 404


# ---------------------------------------------------------------- 关账


def test_close_batches_and_immutable_report(client):
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 150})

    r = client.post("/closes", json={"request_id": "CLOSE-1"})
    assert r.status_code == 201
    report1 = r.json()
    assert report1["sales_count"] == 2 and report1["refunds_count"] == 1
    assert [l["type"] for l in report1["lines"]] == ["sale", "sale", "refund"]
    assert report1["total_sales_royalty_cents"] == 213600 + 120000
    assert report1["total_refund_cents"] == 49950
    assert report1["net_royalty_cents"] == 283650
    assert report1["author_totals"] == [
        {"author_id": "A1", "amount_cents": 128160 + 72000 - 29970},
        {"author_id": "A2", "amount_cents": 85440 + 48000 - 19980},
    ]

    # 同 request_id 重试 → 返回原报表
    r = client.post("/closes", json={"request_id": "CLOSE-1"})
    assert r.status_code == 200 and r.json()["close_id"] == report1["close_id"]

    # 关账后补录的旧业务流水进入下一批
    make_sale(client, "S3", copies=10, ts="2020-01-01T00:00:00")
    client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 100})
    r = client.post("/closes")
    report2 = r.json()
    assert report2["sales_count"] == 1 and report2["refunds_count"] == 1
    assert [(l["type"], l["sale_id"] if l["type"] == "sale" else l["refund_id"])
            for l in report2["lines"]] == [("sale", "S3"), ("refund", "R2")]
    assert report2["entry_seq_from"] == report1["entry_seq_to"] + 1

    # 第一批报表不可改：再次读取完全一致
    assert client.get(f"/closes/{report1['close_id']}").json() == report1
    # 流水归属批次已固定
    assert client.get("/sales/S1").json()["close_id"] == report1["close_id"]
    assert client.get("/sales/S3").json()["close_id"] == report2["close_id"]


def test_close_empty_batch(client):
    r = client.post("/closes")
    assert r.status_code == 201
    body = r.json()
    assert body["lines"] == [] and body["author_totals"] == []
    assert body["net_royalty_cents"] == 0


# ---------------------------------------------------------------- 并发


def test_concurrent_sales_and_close(client):
    make_work(client)
    n = 40

    def post_sale(i):
        return client.post(
            "/sales",
            json={
                "sale_id": f"CS{i}",
                "work_id": "W1",
                "business_time": "2026-09-01T10:00:00",
                "copies": 1,
                "net_price": "10.00",
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(post_sale, i) for i in range(n)]
        mid_close = client.post("/closes")  # 与入账并发，归属由提交顺序决定
        results = [f.result() for f in futures]
    assert all(r.status_code == 201 for r in results)
    final_close = client.post("/closes")

    # 累计销量无丢无重：cum_after 恰好是 1..n 的排列
    cums = sorted(client.get(f"/sales/CS{i}").json()["cum_after"] for i in range(n))
    assert cums == list(range(1, n + 1))

    # 每笔流水恰好归属一个批次，且两批区间首尾相接
    c1, c2 = mid_close.json(), final_close.json()
    assert c1["sales_count"] + c2["sales_count"] == n
    assert c2["entry_seq_from"] == c1["entry_seq_to"] + 1
    ids = [l["sale_id"] for l in c1["lines"]] + [l["sale_id"] for l in c2["lines"]]
    assert sorted(ids) == sorted(f"CS{i}" for i in range(n))


# ---------------------------------------------------------------- 付款结算


def pay(client, payment_id, author_id, close_id, amount):
    return client.post(
        "/payments",
        json={
            "payment_id": payment_id,
            "author_id": author_id,
            "close_id": close_id,
            "amount_cents": amount,
        },
    )


def test_payment_happy_path_and_replay(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 应计 128160 分
    r = client.post("/closes", json={"request_id": "C1"})
    close_id = r.json()["close_id"]

    s = client.get("/authors/A1/settlement").json()
    assert s["latest_close_id"] == close_id
    assert s["accrued_cents"] == 128160 and s["paid_cents"] == 0
    assert s["payable_cents"] == 128160 and s["negative_balance_cents"] == 0
    assert s["batches"] == [
        {"close_id": close_id, "batch_net_cents": 128160, "accrued_cents": 128160,
         "created_at": s["batches"][0]["created_at"]}
    ]

    r = pay(client, "P1", "A1", close_id, 100000)
    assert r.status_code == 201
    body = r.json()
    assert body["close_id"] == close_id and body["amount_cents"] == 100000
    assert body["accrued_cents"] == 128160
    assert body["paid_before_cents"] == 0 and body["payable_cents"] == 128160
    assert body["paid_after_cents"] == 100000 and body["negative_balance_cents"] == 0

    # 同付款编号同内容重试 → 原结算结果
    r2 = pay(client, "P1", "A1", close_id, 100000)
    assert r2.status_code == 200 and r2.json() == body
    # 同号异内容（金额不同）→ 409
    assert pay(client, "P1", "A1", close_id, 100001).status_code == 409

    # 结清剩余可付额 28160
    r = pay(client, "P2", "A1", close_id, 28160)
    assert r.status_code == 201
    assert r.json()["paid_before_cents"] == 100000 and r.json()["payable_cents"] == 28160

    # 可付额已为 0，任何正金额均被拒
    assert pay(client, "P3", "A1", close_id, 1).status_code == 400

    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 128160 and s["paid_cents"] == 128160
    assert s["payable_cents"] == 0 and s["negative_balance_cents"] == 0
    assert [p["payment_id"] for p in s["payments"]] == ["P1", "P2"]
    assert client.get("/payments/P1").json() == body


def test_payment_validation_errors(client):
    # 作者不存在 → 404
    assert pay(client, "P1", "NOBODY", 99, 100).status_code == 404
    make_work(client)
    client.post("/closes", json={"request_id": "C1"})
    # 批次不存在 → 404
    assert pay(client, "P1", "A1", 999, 100).status_code == 404
    # 再关一批，旧批次不再是当前最新 → 409
    client.post("/closes", json={"request_id": "C2"})
    assert pay(client, "P1", "A1", 1, 100).status_code == 409
    # 当前最新批次该作者应计为 0 → 可付额 0，正金额 → 400
    assert pay(client, "P1", "A1", 2, 100).status_code == 400
    # 金额非正 → 422（请求体校验）
    assert pay(client, "P2", "A1", 2, 0).status_code == 422


def test_payment_negative_balance_recovered_by_later_close(client):
    make_work(client)
    # 100 册 ×33.33×8% = 每册 267 分 → 26700；A1 60% = 16020
    make_sale(client, "S1", copies=100)
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    assert pay(client, "P1", "A1", c1, 16020).status_code == 201

    # 关账后全额退款 26700（A1 -16020），再关账
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]

    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 0 and s["paid_cents"] == 16020
    assert s["negative_balance_cents"] == 16020 and s["payable_cents"] == 0
    assert [(b["close_id"], b["batch_net_cents"], b["accrued_cents"]) for b in s["batches"]] == [
        (c1, 16020, 16020),
        (c2, -16020, 0),
    ]
    # 负余额时拒绝任何付款
    assert pay(client, "P2", "A1", c2, 1).status_code == 400

    # 后续关账的正额先抵扣：300 册全部在 8% 档 → 80100；A1 48060
    make_sale(client, "S2", copies=300)
    c3 = client.post("/closes", json={"request_id": "C3"}).json()["close_id"]
    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 48060 and s["paid_cents"] == 16020
    assert s["negative_balance_cents"] == 0 and s["payable_cents"] == 32040
    assert pay(client, "P3", "A1", c3, 32040).status_code == 201

    # 旧报表与旧付款均未被改写；旧付款重放返回原始结算
    assert client.get(f"/closes/{c1}").json()["author_totals"][0]["amount_cents"] == 16020
    old = client.get("/payments/P1").json()
    assert old["close_id"] == c1 and old["accrued_cents"] == 16020
    assert pay(client, "P1", "A1", c1, 16020).json() == old


def test_payment_replay_after_new_close_and_conflict(client):
    make_work(client)
    make_sale(client, "S1", copies=800)  # A1 128160
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    body = pay(client, "P1", "A1", c1, 100000).json()

    # 之后又关账（空批），原批次已不是最新
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]
    # 同号同内容重放仍返回原结算结果（不校验最新批次）
    r = pay(client, "P1", "A1", c1, 100000)
    assert r.status_code == 200 and r.json() == body
    # 同号异内容（金额/批次）→ 409
    assert pay(client, "P1", "A1", c1, 100001).status_code == 409
    assert pay(client, "P1", "A1", c2, 100000).status_code == 409
    # 新批次上可结算剩余可付额
    assert pay(client, "P2", "A1", c2, 28160).status_code == 201
    assert pay(client, "P3", "A1", c2, 1).status_code == 400


def test_concurrent_payments_never_overpay(client):
    make_work(
        client,
        tiers=[{"threshold": 0, "rate": 0.08}],
        authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
    )
    make_sale(client, "S1", copies=20, price="10.00")  # 20×80=1600；A1=960
    close_id = client.post("/closes").json()["close_id"]

    def do_pay(i):
        return pay(client, f"CP{i}", "A1", close_id, 50)  # 20×50=1000 > 可付 960

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_pay, range(20)))
    assert sum(1 for r in results if r.status_code == 201) == 19  # 19×50=950
    assert sum(1 for r in results if r.status_code == 400) == 1
    s = client.get("/authors/A1/settlement").json()
    assert s["paid_cents"] == 950 and s["payable_cents"] == 10
    assert pay(client, "CPX", "A1", close_id, 10).status_code == 201
    assert pay(client, "CPY", "A1", close_id, 1).status_code == 400


def test_payment_and_author_edge_cases(client):
    assert client.get("/payments/NOPE").status_code == 404
    make_work(client)
    # 作者存在但从未关账
    s = client.get("/authors/A1/settlement").json()
    assert s["latest_close_id"] is None
    assert s["accrued_cents"] == 0 and s["paid_cents"] == 0
    assert s["negative_balance_cents"] == 0 and s["payable_cents"] == 0
    assert s["batches"] == [] and s["payments"] == []
    assert client.get("/authors/NOBODY/settlement").status_code == 404


# ---------------------------------------------------------------- 作者分成版本


def change_split(client, work_id, change_id, authors):
    return client.post(
        f"/works/{work_id}/splits",
        json={"change_id": change_id, "authors": authors},
    )


def test_split_change_validation(client):
    make_work(client)
    # 作品不存在 → 404
    assert change_split(client, "NOPE", "X1", [{"author_id": "A1", "ratio": 100}]).status_code == 404
    # 作者编号重复 → 422
    assert change_split(
        client, "W1", "X2",
        [{"author_id": "A1", "ratio": 50}, {"author_id": "A1", "ratio": 50}],
    ).status_code == 422
    # 合计不为 100 → 422
    assert change_split(client, "W1", "X3", [{"author_id": "A1", "ratio": 60}]).status_code == 422
    # 比例为负（或超过 100）→ 422
    assert change_split(
        client, "W1", "X4",
        [{"author_id": "A1", "ratio": 120}, {"author_id": "A2", "ratio": -20}],
    ).status_code == 422
    # 空作者列表 → 422
    assert change_split(client, "W1", "X5", []).status_code == 422
    # 校验失败的提交不占用变更编号：修正后可用同号提交
    r = change_split(client, "W1", "X3", [{"author_id": "A1", "ratio": 100}])
    assert r.status_code == 201 and r.json()["version"] == 2


def test_split_change_idempotency_and_global_conflict(client):
    make_work(client)
    make_work(client, work_id="W2", authors=[{"author_id": "B1", "ratio": 100}])
    payload = [{"author_id": "A1", "ratio": 100}]
    r1 = change_split(client, "W1", "CHG-1", payload)
    assert r1.status_code == 201
    assert r1.json()["version"] == 2 and r1.json()["change_id"] == "CHG-1"
    # 同号同内容重试 → 返回原版本（作者顺序不同视为同内容）
    r2 = change_split(client, "W1", "CHG-1", payload)
    assert r2.status_code == 200 and r2.json() == r1.json()
    # 同号异内容 → 409
    assert change_split(client, "W1", "CHG-1", [{"author_id": "A2", "ratio": 100}]).status_code == 409
    # 变更编号全局唯一：同一编号用于另一作品 → 409
    assert change_split(client, "W2", "CHG-1", payload).status_code == 409


def test_split_versions_listing_and_detail(client):
    make_work(client)
    change_split(client, "W1", "CHG-1",
                 [{"author_id": "A1", "ratio": 70}, {"author_id": "A2", "ratio": 30}])
    change_split(client, "W1", "CHG-2", [{"author_id": "A9", "ratio": 100}])

    r = client.get("/works/W1/splits")
    assert r.status_code == 200
    body = r.json()
    assert body["current_version"] == 3
    assert [v["version"] for v in body["versions"]] == [1, 2, 3]
    # 版本 1 为创建时的初始分成，无变更编号
    assert body["versions"][0]["change_id"] is None
    assert body["versions"][0]["authors"] == [
        {"author_id": "A1", "ratio": "60"},
        {"author_id": "A2", "ratio": "40"},
    ]
    assert body["versions"][1]["change_id"] == "CHG-1"

    # 指定版本查询
    v2 = client.get("/works/W1/splits/2").json()
    assert v2["authors"] == [
        {"author_id": "A1", "ratio": "70"},
        {"author_id": "A2", "ratio": "30"},
    ]
    assert client.get("/works/W1/splits/99").status_code == 404
    assert client.get("/works/NOPE/splits").status_code == 404
    assert client.get("/works/NOPE/splits/1").status_code == 404


def test_work_detail_shows_current_split_and_create_replay_returns_original(client):
    assert make_work(client).status_code == 201
    change_split(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 100}])

    # 作品详情展示当前分成
    w = client.get("/works/W1").json()
    assert w["split_version"] == 2
    assert w["authors"] == [{"author_id": "A1", "ratio": "100"}]

    # 原作品编号同内容创建重试仍返回创建时结果（初始分成）
    r = make_work(client)
    assert r.status_code == 200
    assert r.json()["split_version"] == 1
    assert r.json()["authors"] == [
        {"author_id": "A1", "ratio": "60"},
        {"author_id": "A2", "ratio": "40"},
    ]
    # 同号异内容仍是 409
    assert make_work(client, authors=[{"author_id": "A1", "ratio": 100}]).status_code == 409


def test_sales_use_split_version_at_entry(client):
    make_work(client)
    s1 = make_sale(client, "S1", copies=10, price="10.00").json()
    assert s1["split_version"] == 1
    assert s1["splits"] == [
        {"author_id": "A1", "amount_cents": 480},
        {"author_id": "A2", "amount_cents": 320},
    ]

    change_split(client, "W1", "CHG-1",
                 [{"author_id": "A2", "ratio": 25}, {"author_id": "A1", "ratio": 75}])
    s2 = make_sale(client, "S2", copies=10, price="10.00").json()
    assert s2["split_version"] == 2
    assert s2["splits"] == [
        {"author_id": "A1", "amount_cents": 600},
        {"author_id": "A2", "amount_cents": 200},
    ]

    # 业务时间不决定版本：业务时间更早的销售仍按入账时版本
    s3 = make_sale(client, "S3", copies=10, price="10.00", ts="2020-01-01T00:00:00").json()
    assert s3["split_version"] == 2

    # 既有销售重试仍返回原结果（含原版本号）
    r = make_sale(client, "S1", copies=10, price="10.00")
    assert r.status_code == 200 and r.json() == s1


def test_refund_uses_original_sale_split_version(client):
    make_work(client)  # v1: A1 60 / A2 40
    make_sale(client, "S1", copies=100)  # 26700 → A1 16020 / A2 10680
    # 变更：A1 退出，A3 新加入
    r = change_split(client, "W1", "CHG-1",
                     [{"author_id": "A2", "ratio": 50}, {"author_id": "A3", "ratio": 50}])
    assert r.status_code == 201 and r.json()["version"] == 2

    # 新销售按新版本分账
    body = make_sale(client, "S2", copies=100).json()
    assert body["split_version"] == 2
    assert body["splits"] == [
        {"author_id": "A2", "amount_cents": 13350},
        {"author_id": "A3", "amount_cents": 13350},
    ]

    # 旧销售的后续退款仍从末册冲回并按原销售版本（v1）分账
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
    assert r.status_code == 201
    body = r.json()
    assert body["split_version"] == 1
    assert body["total_refund_cents"] == 2670
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 1602},
        {"author_id": "A2", "amount_cents": 1068},
    ]
    # 新销售的退款按新版本（v2）分账
    r = client.post("/refunds", json={"refund_id": "R2", "sale_id": "S2", "copies": 10})
    assert r.json()["split_version"] == 2
    assert r.json()["splits"] == [
        {"author_id": "A2", "amount_cents": 1335},
        {"author_id": "A3", "amount_cents": 1335},
    ]
    # 既有退款重试仍返回原结果
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
    assert r.status_code == 200 and r.json() == body


def test_exited_author_keeps_settlement_and_new_author_settles(client):
    make_work(client)  # v1: A1 60 / A2 40
    make_sale(client, "S1", copies=100)  # 26700 → A1 16020 / A2 10680
    client.post("/closes", json={"request_id": "C1"})

    # A2 退出，A3 加入
    change_split(client, "W1", "CHG-1",
                 [{"author_id": "A1", "ratio": 70}, {"author_id": "A3", "ratio": 30}])
    make_sale(client, "S2", copies=100)  # 26700 → A1 18690 / A3 8010
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]

    # 退出分成的作者仍可查询并结算既有余额
    s = client.get("/authors/A2/settlement").json()
    assert s["accrued_cents"] == 10680 and s["payable_cents"] == 10680
    assert pay(client, "P-A2", "A2", c2, 10680).status_code == 201
    assert client.get("/authors/A2/settlement").json()["payable_cents"] == 0

    # 新作者参与后续关账与结算
    s = client.get("/authors/A3/settlement").json()
    assert [(b["close_id"], b["batch_net_cents"]) for b in s["batches"]] == [
        (1, 0),
        (c2, 8010),
    ]
    assert s["accrued_cents"] == 8010
    assert pay(client, "P-A3", "A3", c2, 8010).status_code == 201

    # A1 跨版本累计应计
    s = client.get("/authors/A1/settlement").json()
    assert s["accrued_cents"] == 16020 + 18690

    # 已关账快照与付款记录不改写
    c1_report = client.get("/closes/1").json()
    assert c1_report["author_totals"] == [
        {"author_id": "A1", "amount_cents": 16020},
        {"author_id": "A2", "amount_cents": 10680},
    ]


def test_concurrent_split_changes_and_sales(client):
    make_work(client)

    def change(i):
        return change_split(client, "W1", f"CHG-{i}", [{"author_id": "A1", "ratio": 100}])

    def sale(i):
        return make_sale(client, f"VS{i}", copies=1, price="10.00")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(change, i) for i in range(5)]
        futures += [pool.submit(sale, i) for i in range(20)]
        results = [f.result() for f in futures]
    assert all(r.status_code == 201 for r in results)

    # 版本号按提交顺序连续递增，无重号无缺口
    versions = client.get("/works/W1/splits").json()
    assert [v["version"] for v in versions["versions"]] == [1, 2, 3, 4, 5, 6]
    assert versions["current_version"] == 6

    # 并发变更与入账有确定先后：入账序号单调 ⇒ 使用的分成版本单调不减
    sales_are = [client.get(f"/sales/VS{i}").json() for i in range(20)]
    by_seq = sorted(sales_are, key=lambda s: s["entry_seq"])
    used = [s["split_version"] for s in by_seq]
    assert used == sorted(used)
    assert all(1 <= v <= 6 for v in used)


LEGACY_SCHEMA = """
CREATE TABLE works (work_id TEXT PRIMARY KEY, title TEXT, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE work_tiers (work_id TEXT NOT NULL, idx INTEGER NOT NULL, threshold INTEGER NOT NULL, rate TEXT NOT NULL, PRIMARY KEY (work_id, idx));
CREATE TABLE work_authors (work_id TEXT NOT NULL, author_id TEXT NOT NULL, ratio TEXT NOT NULL, PRIMARY KEY (work_id, author_id));
CREATE TABLE entries (entry_seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL CHECK (kind IN ('sale','refund')), ref_id TEXT NOT NULL);
CREATE TABLE sales (sale_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, business_time TEXT NOT NULL, copies INTEGER NOT NULL, net_price TEXT NOT NULL, entry_seq INTEGER NOT NULL UNIQUE, cum_before INTEGER NOT NULL, cum_after INTEGER NOT NULL, total_royalty_cents INTEGER NOT NULL, close_id INTEGER, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE sale_segments (sale_id TEXT NOT NULL, idx INTEGER NOT NULL, tier_idx INTEGER NOT NULL, rate TEXT NOT NULL, copies INTEGER NOT NULL, per_copy_cents INTEGER NOT NULL, amount_cents INTEGER NOT NULL, refunded_copies INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (sale_id, idx));
CREATE TABLE sale_splits (sale_id TEXT NOT NULL, author_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, PRIMARY KEY (sale_id, author_id));
"""


def test_migration_from_legacy_schema(tmp_path):
    """旧库（无分成版本）启动自动迁移：初始分成迁为版本 1，历史销售记为版本 1。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO works VALUES('W1','旧作品','fp','2026-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO work_tiers VALUES('W1',0,0,'0.08')")
    conn.execute("INSERT INTO work_authors VALUES('W1','A1','60')")
    conn.execute("INSERT INTO work_authors VALUES('W1','A2','40')")
    conn.execute("INSERT INTO entries(kind, ref_id) VALUES('sale','S1')")
    conn.execute(
        "INSERT INTO sales(sale_id, work_id, business_time, copies, net_price, entry_seq,"
        " cum_before, cum_after, total_royalty_cents, close_id, fingerprint, created_at)"
        " VALUES('S1','W1','2026-01-01T00:00:00',100,'33.33',1,0,100,26700,NULL,'fp',"
        " '2026-01-01T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO sale_segments VALUES('S1',0,0,'0.08',100,267,26700,0)")
    conn.execute("INSERT INTO sale_splits VALUES('S1','A1',16020)")
    conn.execute("INSERT INTO sale_splits VALUES('S1','A2',10680)")
    conn.commit()
    conn.close()

    db.open_database(str(path))
    db.init_db()
    with TestClient(main.app) as client:
        # 旧作品分成迁为版本 1，详情可查
        w = client.get("/works/W1").json()
        assert w["split_version"] == 1
        assert w["authors"] == [
            {"author_id": "A1", "ratio": "60"},
            {"author_id": "A2", "ratio": "40"},
        ]
        # 历史销售记为版本 1；其退款仍按原版本分账
        assert client.get("/sales/S1").json()["split_version"] == 1
        r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
        assert r.status_code == 201
        assert r.json()["split_version"] == 1
        assert r.json()["splits"] == [
            {"author_id": "A1", "amount_cents": 1602},
            {"author_id": "A2", "amount_cents": 1068},
        ]
        # 迁移后可正常变更分成，新销售使用新版本
        assert change_split(client, "W1", "CHG-1", [{"author_id": "A1", "ratio": 100}]).status_code == 201
        s = make_sale(client, "S2", copies=1, price="10.00").json()
        assert s["split_version"] == 2
        assert s["splits"] == [{"author_id": "A1", "amount_cents": 80}]


# ---------------------------------------------------------------- 销售净价更正


def test_price_correction_tier_delta_and_author_split(client):
    """各档未退册数的新旧每册版税差额；逐作者为两个未退总额独立分账后相减。"""
    make_work(client)
    make_sale(client, "S1", copies=800)
    make_sale(client, "S2", copies=400)  # 800→1000 档 200 册@267；1000→1200 档 200 册@333
    # 先退 250 册（200@333 + 50@267）：剩余 950 册全在 0 档
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S2", "copies": 250})

    r = correct_price(client, "PC1", "S2", 40)  # 0.08 档 ppc 267→320（+53）
    assert r.status_code == 201
    b = r.json()
    assert b["type"] == "price_correction"
    assert b["sale_id"] == "S2" and b["sale_entry_seq"] == 2
    assert b["split_version"] == 1  # 原销售入账时的分成版本
    assert b["entry_seq"] == 4  # S1=1, S2=2, R1=3, PC1=4
    assert b["segments"] == [
        {
            "segment_index": 0,
            "sale_segment_index": 0,
            "tier_index": 0,
            "rate": "0.08",
            "unrefunded_copies": 150,
            "old_per_copy_royalty_cents": 267,
            "new_per_copy_royalty_cents": 320,
            "delta_per_copy_cents": 53,
            "amount_cents": 150 * 53,
        }
    ]
    assert b["total_delta_cents"] == 150 * 53
    assert b["splits"] == [
        {"author_id": "A1", "amount_cents": int(150 * 53 * 0.6)},
        {"author_id": "A2", "amount_cents": int(150 * 53 * 0.4)},
    ]
    assert b["close_id"] is None


def test_price_correction_multi_tier(client):
    """未退款销售跨档更正：逐档分别计算新旧每册版税差额。"""
    make_work(client)
    make_sale(client, "S1", copies=1200)  # 1000@267 + 200@333
    r = correct_price(client, "PC1", "S1", 40)  # 320（+53）/ 400（+67）
    assert r.status_code == 201
    b = r.json()
    assert [(s["tier_index"], s["unrefunded_copies"], s["delta_per_copy_cents"])
            for s in b["segments"]] == [(0, 1000, 53), (1, 200, 67)]
    total = 1000 * 53 + 200 * 67
    assert b["total_delta_cents"] == total
    assert b["splits"] == [
        {"author_id": "A1", "amount_cents": 39840},
        {"author_id": "A2", "amount_cents": 26560},
    ]


def test_price_correction_negative_delta(client):
    """降价更正产生负差额；退款反向流水同号冲回。"""
    make_work(client)
    make_sale(client, "S1", copies=1200)
    # 33.33→25：0.08 档 267→200（−67）；0.10 档 333→250（−83）
    r = correct_price(client, "PC1", "S1", 25)
    assert r.status_code == 201
    b = r.json()
    assert b["total_delta_cents"] == 1000 * -67 + 200 * -83
    assert b["splits"] == [
        {"author_id": "A1", "amount_cents": -50160},
        {"author_id": "A2", "amount_cents": -33440},
    ]
    # 退 200 册（全部 10% 档）：反向流水每册 −83，带负号
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 200})
    rev = r.json()["price_delta_reversal"]
    assert rev["total_reversal_cents"] == 200 * -83
    assert rev["splits"] == [
        {"author_id": "A1", "amount_cents": -9960},
        {"author_id": "A2", "amount_cents": -6640},
    ]


def test_price_correction_idempotency_conflict_and_gates(client):
    make_work(client)
    make_sale(client, "S1", copies=10)
    r1 = correct_price(client, "PC1", "S1", 40)
    assert r1.status_code == 201
    # 同号同内容重试 → 原结果
    r2 = correct_price(client, "PC1", "S1", 40)
    assert r2.status_code == 200 and r2.json() == r1.json()
    # 同号异内容 → 409
    assert correct_price(client, "PC1", "S1", 41).status_code == 409
    # 新价等于当前更正价 → 409
    assert correct_price(client, "PC2", "S1", 40).status_code == 409
    # 更正编号全局唯一：跨销售复用 → 409
    make_sale(client, "S2", copies=10)
    assert correct_price(client, "PC1", "S2", 40).status_code == 409
    # 销售不存在 → 404
    assert correct_price(client, "PC9", "NOPE", 40).status_code == 404
    # 新价为负 → 422
    assert correct_price(client, "PC8", "S1", -1).status_code == 422


def test_price_correction_rejected_after_full_refund(client):
    make_work(client)
    make_sale(client, "S1", copies=10)
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
    assert correct_price(client, "PC1", "S1", 40).status_code == 400


def test_refund_creates_reversal_and_original_fields_unchanged(client):
    """更正后退款：原价/原分成冲回字段不变，另附价差反向流水（独立入账序号）。"""
    make_work(client)
    make_sale(client, "S1", copies=1200)
    correct_price(client, "PC1", "S1", 40)
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 250})
    assert r.status_code == 201
    body = r.json()
    # 原退款字段：200 册@333 + 50 册@267，按原销售版本 v1
    assert body["split_version"] == 1
    assert body["total_refund_cents"] == 200 * 333 + 50 * 267
    assert body["segments"][0]["per_copy_royalty_cents"] == 267
    # 反向流水：200 册每册 +67、50 册每册 +53（独立流水，序号紧随退款）
    rev = body["price_delta_reversal"]
    assert rev["type"] == "price_delta_reversal"
    assert rev["entry_seq"] == body["entry_seq"] + 1
    assert rev["sale_id"] == "S1" and rev["split_version"] == 1
    assert [(g["tier_index"], g["copies"], g["delta_per_copy_cents"])
            for g in rev["segments"]] == [(0, 50, 53), (1, 200, 67)]
    assert rev["total_reversal_cents"] == 50 * 53 + 200 * 67
    # 退款重放返回完全相同的结果（含同一反向流水）
    r2 = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 250})
    assert r2.status_code == 200 and r2.json() == body


def test_legacy_refund_has_no_reversal(client):
    """未做过更正的销售，退款不产生反向流水（旧退款不追溯）。"""
    make_work(client)
    make_sale(client, "S1", copies=10)
    body = client.post(
        "/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3}
    ).json()
    assert body["price_delta_reversal"] is None


def test_full_refund_zeroes_correction_per_author(client):
    """全退后该销售逐作者累计价差（更正 − 反向）归零，含跨档余分错位的极端情形。"""
    make_work(client)
    make_sale(client, "S1", copies=1200)  # 1000@267 + 200@333
    correct_price(client, "PC1", "S1", 40)  # +53 / +67
    # 退掉 200 册（10% 档）：冲回 200×67
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 200})
    # 串联第二次更正：剩余 1000 册全在 0 档，40→45，320→360（+40）
    pc2 = correct_price(client, "PC2", "S1", 45)
    assert pc2.status_code == 201
    assert pc2.json()["old_net_price"] == "40"
    assert pc2.json()["total_delta_cents"] == 1000 * 40
    # 退满剩余 1000 册：反向流水按望远镜冲回全部累计价差
    r = client.post("/refunds", json={"refund_id": "R2", "sale_id": "S1", "copies": 1000})
    rev = r.json()["price_delta_reversal"]
    corrections = client.get(
        "/price-corrections", params={"sale_id": "S1"}
    ).json()["corrections"]
    corr_by_author: dict[str, int] = {}
    for pc in corrections:
        for s in pc["splits"]:
            corr_by_author[s["author_id"]] = corr_by_author.get(s["author_id"], 0) + s["amount_cents"]
    # 第一次退款也带有反向流水
    rev1 = client.get("/refunds/R1").json()["price_delta_reversal"]
    rev_by_author = {s["author_id"]: s["amount_cents"] for s in rev1["splits"]}
    for s in rev["splits"]:
        rev_by_author[s["author_id"]] = rev_by_author.get(s["author_id"], 0) + s["amount_cents"]
    assert rev_by_author == corr_by_author  # 反向合计 = 更正合计 ⇒ 累计价差归零


def test_correction_and_reversal_in_next_close_batch_only(client):
    """更正与反向作为独立流水进入下一关账批次；历史销售/退款/已关账报表/付款不改写。"""
    make_work(client)
    make_sale(client, "S1", copies=1200)
    c1_id = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    rep1 = client.get(f"/closes/{c1_id}").json()
    assert rep1["price_corrections_count"] == 0

    correct_price(client, "PC1", "S1", 40)
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 1200})
    c2 = client.post("/closes", json={"request_id": "C2"}).json()
    types = [l["type"] for l in c2["lines"]]
    assert types == ["price_correction", "refund", "price_delta_reversal"]
    # 全退后作者净额为 0：C2 = 更正 − 退款 − 反向 = −退款 = −原销售（更正与反向抵消）
    assert c2["author_totals"] == [
        {"author_id": "A1", "amount_cents": -200160},
        {"author_id": "A2", "amount_cents": -133440},
    ]
    assert c2["price_corrections_count"] == 1
    assert c2["price_delta_reversals_count"] == 1
    assert c2["total_price_correction_cents"] == 66400
    assert c2["total_price_delta_reversal_cents"] == 66400
    assert c2["total_author_net_cents"] == 66400 - 333600 - 66400
    # net_royalty_cents 仍仅为销售−退款口径（新增字段不改变既有语义）
    assert c2["net_royalty_cents"] == -333600

    # 历史销售与第一批报表未被改写
    s = client.get("/sales/S1").json()
    assert s["net_price"] == "33.33" and s["total_royalty_cents"] == 333600
    rep1 = client.get(f"/closes/{c1_id}").json()
    assert rep1["total_sales_royalty_cents"] == 333600
    assert rep1["price_corrections_count"] == 0
    # 全退后作者累计应计为 0，历史付款逻辑不受影响
    assert client.get("/authors/A1/settlement").json()["accrued_cents"] == 0


def test_correction_query_endpoints(client):
    make_work(client)
    make_sale(client, "S1", copies=10)
    make_sale(client, "S2", copies=10)
    correct_price(client, "PC1", "S1", 40)
    correct_price(client, "PC2", "S2", 40)
    # 按更正编号查询
    r = client.get("/price-corrections/PC1")
    assert r.status_code == 200 and r.json()["sale_id"] == "S1"
    assert client.get("/price-corrections/NOPE").status_code == 404
    # 列表（全部 / 按销售过滤）
    assert [x["correction_id"] for x in client.get("/price-corrections").json()["corrections"]] == ["PC1", "PC2"]
    assert [x["correction_id"]
            for x in client.get("/price-corrections", params={"sale_id": "S2"}).json()["corrections"]] == ["PC2"]
    assert client.get("/price-corrections", params={"sale_id": "NOPE"}).status_code == 404


def test_concurrent_corrections_refunds_and_close(client):
    """更正/退款/关账并发：按提交顺序判定未退册数与批次，全退后累计价差归零。"""
    make_work(client, tiers=[{"threshold": 0, "rate": 0.5}])
    make_sale(client, "S1", copies=100, price="10.00")  # ppc 500，总额 50000

    barrier = threading.Barrier(8)

    def refund(i):
        barrier.wait()
        return client.post("/refunds", json={"refund_id": f"R{i}", "sale_id": "S1", "copies": 5})

    def correct(i):
        barrier.wait()
        return correct_price(client, f"PC{i}", "S1", 10 + i)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(refund, i) for i in range(10)]  # 10×5 = 50 册
        futures += [pool.submit(correct, i) for i in range(6)]
        results = [f.result() for f in futures]

    # 每个编号至多成功一次；其余按提交顺序被拒：已全退（400）或新价等于当前价（409）
    assert all(r.status_code in (201, 400, 409) for r in results)
    accepted_corr = [
        r.json() for r in results
        if r.status_code == 201 and r.json().get("type") == "price_correction"
    ]

    # 把剩余 50 册退完后：无论更正与退款如何交错，望远镜恒等式保证逐作者累计价差归零
    client.post("/refunds", json={"refund_id": "RFINAL", "sale_id": "S1", "copies": 50})
    report = client.post("/closes").json()
    # 销售/退款/更正/反向全部在同一批：全退后各作者净额为 0
    assert report["author_totals"] == [
        {"author_id": "A1", "amount_cents": 0},
        {"author_id": "A2", "amount_cents": 0},
    ]
    # 累计价差（更正 − 反向）合计为 0
    corr_total = sum(x["total_delta_cents"] for x in accepted_corr)
    rev_total = report["total_price_delta_reversal_cents"]
    refund_total = report["total_refund_cents"]
    assert corr_total - rev_total == 0  # 全退后累计价差为 0
    assert refund_total == 50000  # 原价冲回，与更正无关
    assert report["net_royalty_cents"] == 0

    # 此后该销售已全退，任何新更正均拒绝
    assert correct_price(client, "PCFINAL", "S1", 99).status_code == 400
    # 入账序号无重号：本批流水序号严格递增
    seqs = [l["entry_seq"] for l in report["lines"]]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_price_correction_uses_original_split_version(client):
    """更正及反向分账始终按原销售入账版本，与当前分成版本无关。"""
    make_work(client)  # v1: A1 60 / A2 40
    make_sale(client, "S1", copies=100)
    change_split(client, "W1", "CHG-1",
                 [{"author_id": "A1", "ratio": 70}, {"author_id": "A3", "ratio": 30}])
    b = correct_price(client, "PC1", "S1", 40).json()
    assert b["split_version"] == 1
    assert {s["author_id"] for s in b["splits"]} == {"A1", "A2"}  # 不含 A3
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100}).json()
    assert r["price_delta_reversal"]["split_version"] == 1
    assert {s["author_id"] for s in r["price_delta_reversal"]["splits"]} == {"A1", "A2"}


def test_migration_accepts_new_entry_kinds(tmp_path):
    """旧库迁移后 entries 约束接受净价更正与价差反向两种新流水。"""
    path = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO works VALUES('W1','旧作品','fp','2026-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO work_tiers VALUES('W1',0,0,'0.08')")
    conn.execute("INSERT INTO work_authors VALUES('W1','A1','60')")
    conn.execute("INSERT INTO work_authors VALUES('W1','A2','40')")
    conn.execute("INSERT INTO entries(kind, ref_id) VALUES('sale','S1')")
    conn.execute(
        "INSERT INTO sales(sale_id, work_id, business_time, copies, net_price, entry_seq,"
        " cum_before, cum_after, total_royalty_cents, close_id, fingerprint, created_at)"
        " VALUES('S1','W1','2026-01-01T00:00:00',100,'33.33',1,0,100,26700,NULL,'fp',"
        " '2026-01-01T00:00:00+00:00')"
    )
    conn.execute("INSERT INTO sale_segments VALUES('S1',0,0,'0.08',100,267,26700,0)")
    conn.execute("INSERT INTO sale_splits VALUES('S1','A1',16020)")
    conn.execute("INSERT INTO sale_splits VALUES('S1','A2',10680)")
    conn.commit()
    conn.close()

    db.open_database(str(path))
    db.init_db()
    with TestClient(main.app) as client:
        r = correct_price(client, "PC1", "S1", 40)
        assert r.status_code == 201  # 新 CHECK 约束已生效
        # 迁移保留了原销售的入账序号 1，更正取新序号
        assert r.json()["sale_entry_seq"] == 1 and r.json()["entry_seq"] == 2
        # 全退：原价冲回 + 价差反向，逐作者归零
        body = client.post(
            "/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100}
        ).json()
        assert body["entry_seq"] == 3 and body["price_delta_reversal"]["entry_seq"] == 4
        report = client.post("/closes").json()
        assert report["author_totals"] == [
            {"author_id": "A1", "amount_cents": 0},
            {"author_id": "A2", "amount_cents": 0},
        ]
