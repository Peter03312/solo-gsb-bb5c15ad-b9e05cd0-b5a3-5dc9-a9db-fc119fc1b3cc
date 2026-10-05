"""端到端 API 测试：销售净价更正与退款价差反向流水。

覆盖：
- 更正按原销售阶梯费率/入账时分成版本、仅对各档未退册数计新旧净价差额；
- 逐作者差额 = 两种净价下未退款总额分别整额分账后相减；
- 更正以独立流水进入下一关账批次，历史销售/退款/已关账报表/付款不改写；
- 此后退款原价冲回不变，另记价差反向流水，随退款同批次；全退后逐作者价差归零；
- 拒绝条件、幂等重放/冲突、并发提交顺序。
"""
from __future__ import annotations

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
    return client.post(
        "/works",
        json={
            "work_id": work_id,
            "title": "测试作品",
            "tiers": tiers
            or [{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10}],
            "authors": authors
            or [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
        },
    )


def make_sale(client, sale_id="S1", work_id="W1", copies=1200, price="33.33",
              ts="2026-09-01T10:00:00"):
    return client.post(
        "/sales",
        json={
            "sale_id": sale_id, "work_id": work_id,
            "business_time": ts, "copies": copies, "net_price": price,
        },
    )


def correct(client, correction_id, sale_id="S1", price="40.00"):
    return client.post(
        "/corrections",
        json={"correction_id": correction_id, "sale_id": sale_id, "new_net_price": price},
    )


# ---------------------------------------------------------------- 基本计提差额


def test_correction_diff_per_tier_and_per_author(client):
    make_work(client)
    # 1200 册 @33.33：1000 册档@267 + 200 册档@333
    make_sale(client)
    r = correct(client, "K1", price="40.00")
    assert r.status_code == 201
    body = r.json()
    assert body["sale_id"] == "S1" and body["split_version"] == 1
    assert body["old_net_price"] == "33.33" and body["new_net_price"] == "40"
    # 40×8%=3.20→320（差 53/册）；40×10%=4.00→400（差 67/册）
    assert [
        (g["sale_segment_index"], g["copies"],
         g["old_per_copy_royalty_cents"], g["new_per_copy_royalty_cents"], g["diff_cents"])
        for g in body["segments"]
    ] == [(0, 1000, 267, 320, 53000), (1, 200, 333, 400, 13400)]
    # 旧总额 333600，新总额 400000，差额 66400
    assert body["old_total_royalty_cents"] == 333600
    assert body["new_total_royalty_cents"] == 400000
    assert body["total_diff_cents"] == 66400
    # 逐作者差额：400000 与 333600 各自整额 60/40 分账后相减
    assert body["splits"] == [
        {"author_id": "A1", "diff_cents": 240000 - 200160},
        {"author_id": "A2", "diff_cents": 160000 - 133440},
    ]
    # 关联销售与入账序号
    assert body["entry_seq"] == 2
    sale = client.get("/sales/S1").json()
    assert sale["net_price"] == "33.33"  # 原销售净价不改写
    assert sale["current_net_price"] == "40"
    assert [k["correction_id"] for k in sale["price_corrections"]] == ["K1"]
    # 可按更正编号查询
    assert client.get("/corrections/K1").json() == body


def test_correction_uses_only_unrefunded_copies_and_sale_version(client):
    make_work(client)
    make_sale(client)
    # 先退第二档 200 册；更正只针对第一档未退 1000 册
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 200})
    # 分成变更为 v2，旧销售更正仍按原销售版本 v1
    client.post("/works/W1/splits", json={
        "change_id": "CHG1",
        "authors": [{"author_id": "A1", "ratio": 70}, {"author_id": "A3", "ratio": 30}],
    })
    body = correct(client, "K1", price="40.00").json()
    assert body["split_version"] == 1
    assert [(g["sale_segment_index"], g["copies"], g["diff_cents"]) for g in body["segments"]] == [
        (0, 1000, 53000)
    ]
    assert body["splits"] == [
        {"author_id": "A1", "diff_cents": 31800},
        {"author_id": "A2", "diff_cents": 21200},
    ]


# ---------------------------------------------------------------- 校验与幂等


def test_correction_validation(client):
    make_work(client)
    make_sale(client, copies=100)
    # 销售不存在 → 404
    assert correct(client, "K0", sale_id="NOPE").status_code == 404
    # 新价 = 原净价（当前更正价）→ 400
    assert correct(client, "K0", price="33.33").status_code == 400
    # 负价 → 422
    assert correct(client, "K0", price="-1").status_code == 422
    # 全退后拒绝
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    assert correct(client, "K2").status_code == 400
    # 更正编号不存在
    assert client.get("/corrections/NOPE").status_code == 404


def test_correction_idempotency_and_global_conflict(client):
    make_work(client)
    make_sale(client, copies=100)
    make_sale(client, sale_id="S2", copies=10)
    r1 = correct(client, "K1", price="40")
    assert r1.status_code == 201
    # 同号同内容重试 → 原结果（即使销售后来全退）
    r2 = correct(client, "K1", price="40")
    assert r2.status_code == 200 and r2.json() == r1.json()
    # 同号异内容 → 409
    assert correct(client, "K1", price="41").status_code == 409
    # 更正编号全局唯一：复用到另一销售 → 409
    assert correct(client, "K1", sale_id="S2", price="40").status_code == 409
    # 全退后同号重放仍返回原结果
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 100})
    r3 = correct(client, "K1", price="40")
    assert r3.status_code == 200 and r3.json() == r1.json()


def test_chained_correction_compares_with_latest_price(client):
    make_work(client)
    make_sale(client, copies=100)
    assert correct(client, "K1", price="40").status_code == 201
    # 新价等于当前更正价 → 拒绝（即使与原价不同）
    assert correct(client, "K2", price="40").status_code == 400
    # 第二笔相对上一生效价 40
    body = correct(client, "K2", price="35").json()
    assert body["old_net_price"] == "40" and body["new_net_price"] == "35"
    # 35×8%=2.80→280，每册 -40 × 100
    assert body["segments"][0]["diff_cents"] == -4000
    sale = client.get("/sales/S1").json()
    assert sale["current_net_price"] == "35"
    assert [k["correction_id"] for k in sale["price_corrections"]] == ["K1", "K2"]


# ---------------------------------------------------------------- 退款价差反向


def test_refund_keeps_original_amounts_and_adds_reversal(client):
    make_work(client)
    make_sale(client, copies=100)  # 100 册 @267
    correct(client, "K1", price="40")  # 320/册，差 53/册
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
    body = r.json()
    # 原退款字段及金额不变：仍按原价 267/册、原版本分账
    assert body["total_refund_cents"] == 2670
    assert body["splits"] == [
        {"author_id": "A1", "amount_cents": 1602},
        {"author_id": "A2", "amount_cents": 1068},
    ]
    rev = body["price_diff_reversal"]
    assert rev is not None
    # 价差反向：-53×10 = -530，序号紧跟退款
    assert rev["total_diff_cents"] == -530
    assert rev["entry_seq"] == body["entry_seq"] + 1
    assert rev["segments"] == [
        {"segment_index": 0, "sale_segment_index": 0, "copies": 10, "diff_cents": -530}
    ]
    assert rev["splits"] == [
        {"author_id": "A1", "amount_cents": -318},
        {"author_id": "A2", "amount_cents": -212},
    ]
    # GET 退款同样可查
    assert client.get("/refunds/R1").json()["price_diff_reversal"]["total_diff_cents"] == -530


def test_old_refund_has_no_reversal_not_retroactive(client):
    make_work(client)
    make_sale(client, copies=100)
    # 更正前的旧退款不追溯
    client.post("/refunds", json={"refund_id": "R0", "sale_id": "S1", "copies": 10})
    correct(client, "K1", price="40")
    old = client.get("/refunds/R0").json()
    assert old["price_diff_reversal"] is None
    # 更正只对剩余 90 册生效；之后退款才有反向流水
    assert correct(client, "K1").json()["segments"][0]["copies"] == 90
    new = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10}).json()
    assert new["price_diff_reversal"]["total_diff_cents"] == -530


def test_full_refund_zeroes_per_author_price_diff(client):
    make_work(client)
    make_sale(client, copies=1200)
    correct(client, "K1", price="40")  # +66400
    # 部分退 300 册（第二档 200 + 第一档 100），再全退剩余 900 册
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 300})
    # 中间再来一笔只作用于 900 册未退的降价更正
    client.post("/corrections", json={
        "correction_id": "K2", "sale_id": "S1", "new_net_price": "35",
    })
    client.post("/refunds", json={"refund_id": "R2", "sale_id": "S1", "copies": 900})
    report = client.post("/closes").json()
    # 全退后作者合计净额严格为 0（销售 − 退款 + 更正 + 反向 = 0）
    assert report["author_totals"] == [
        {"author_id": "A1", "amount_cents": 0},
        {"author_id": "A2", "amount_cents": 0},
    ]
    assert report["net_royalty_cents"] == 0
    # 分档价差池清零
    conn = db.get_conn()
    assert conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM correction_segment_pools WHERE sale_id='S1'"
    ).fetchone()[0] == 0
    # 历史销售/退款金额未被改写
    assert client.get("/sales/S1").json()["total_royalty_cents"] == 333600
    assert client.get("/refunds/R1").json()["total_refund_cents"] == 300 * 333 - 100 * 66
    # 逐作者累计价差（更正 + 全部反向）归零
    corr = sum(
        s["diff_cents"]
        for cid in ("K1", "K2")
        for s in client.get(f"/corrections/{cid}").json()["splits"]
    )
    revs = sum(
        s["amount_cents"]
        for rid in ("R1", "R2")
        for s in client.get(f"/refunds/{rid}").json()["price_diff_reversal"]["splits"]
    )
    assert corr + revs == 0


# ---------------------------------------------------------------- 关账批次


def test_correction_enters_next_close_and_history_immutable(client):
    make_work(client)
    make_sale(client, copies=100)
    first = client.post("/closes", json={"request_id": "C1"}).json()
    assert first["corrections_count"] == 0 and first["diff_reversals_count"] == 0

    correct(client, "K1", price="40")  # +5300
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
    second = client.post("/closes", json={"request_id": "C2"}).json()
    assert [l["type"] for l in second["lines"]] == ["correction", "refund",
                                                     "price_diff_reversal"]
    assert second["corrections_count"] == 1 and second["diff_reversals_count"] == 1
    assert second["total_price_correction_cents"] == 5300
    assert second["total_price_diff_reversal_cents"] == -530
    # 作者净额：更正 5300 − 退款原价 2670 − 价差反向 530 = 2100
    a1 = next(t for t in second["author_totals"] if t["author_id"] == "A1")
    assert a1["amount_cents"] == round((5300 - 2670 - 530) * 0.6)
    # 第一批报表不可改：无更正行
    assert client.get("/closes/1").json() == first
    # 流水批次归属固定
    assert client.get("/corrections/K1").json()["close_id"] == second["close_id"]
    assert client.get("/refunds/R1").json()["price_diff_reversal"]["close_id"] == second["close_id"]


def test_refund_and_reversal_always_same_batch(client):
    make_work(client)
    make_sale(client, copies=100)
    correct(client, "K1", price="40")
    # 更正先进一批
    c1 = client.post("/closes").json()
    assert c1["corrections_count"] == 1 and c1["diff_reversals_count"] == 0
    # 退款与其反向流水序号相邻，同批进入下一关账
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10}).json()
    assert r["price_diff_reversal"]["entry_seq"] == r["entry_seq"] + 1
    c2 = client.post("/closes").json()
    assert c2["entry_seq_from"] == r["entry_seq"]
    assert [l["type"] for l in c2["lines"]] == ["refund", "price_diff_reversal"]
    assert c2["total_refund_cents"] == 2670
    assert c2["total_price_diff_reversal_cents"] == -530


# ---------------------------------------------------------------- 并发


def test_concurrent_corrections_refunds_and_close(client):
    make_work(
        client,
        tiers=[{"threshold": 0, "rate": 0.08}],
        authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}],
    )
    make_sale(client, copies=200, price="10.00")  # 80 分/册，总 16000

    def correct(i):
        return client.post("/corrections", json={
            "correction_id": f"KC{i}", "sale_id": "S1", "new_net_price": str(10 + i + 1),
        })

    def refund(i):
        return client.post("/refunds", json={
            "refund_id": f"RC{i}", "sale_id": "S1", "copies": 10,
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(correct, i) for i in range(5)]
        futs += [pool.submit(refund, i) for i in range(20)]  # 20×10=200 全退
        mid_close = client.post("/closes")  # 与更正/退款并发
        results = [f.result() for f in futs]
    # 提交顺序确定：成功退款恰好退满 200 册，其余退款 400；更正可能在全退后被拒（400）
    refund_ok = [r for r in results if r.status_code == 201
                 and r.json().get("type") == "refund"]
    assert sum(r.json()["copies"] for r in refund_ok) == 200
    assert all(
        r.status_code == 201 and r.json().get("type") in ("correction", "refund")
        or r.status_code == 400
        for r in results
    )
    final_close = client.post("/closes")

    # 无论提交顺序如何，全退后逐作者净额为 0（销售 − 退款 + 更正 − 反向）
    total = {"A1": 0, "A2": 0}
    for rep in (mid_close.json(), final_close.json()):
        for t in rep["author_totals"]:
            total[t["author_id"]] = total.get(t["author_id"], 0) + t["amount_cents"]
    assert total == {"A1": 0, "A2": 0}
    conn = db.get_conn()
    assert conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM correction_segment_pools WHERE sale_id='S1'"
    ).fetchone()[0] == 0
    # 每条成功退款都带价差反向流水，且序号相邻
    for r in refund_ok:
        rev = r.json()["price_diff_reversal"]
        assert rev is not None
        assert rev["entry_seq"] == r.json()["entry_seq"] + 1


def test_close_after_correction_then_payment_uses_snapshot(client):
    make_work(client)
    make_sale(client, copies=100)  # 26700 → A1 16020
    c1 = client.post("/closes", json={"request_id": "C1"}).json()["close_id"]
    client.post("/payments", json={
        "payment_id": "P1", "author_id": "A1", "close_id": c1, "amount_cents": 16020,
    })
    # 关账后更正（+5300，A1 +3180）进入下一批；旧批次与旧付款不改写
    correct(client, "K1", price="40")
    c2 = client.post("/closes", json={"request_id": "C2"}).json()["close_id"]
    s = client.get("/authors/A1/settlement").json()
    # 应计 = 16020 + 3180；已付 16020；可付 3180
    assert s["accrued_cents"] == 16020 + 3180 and s["payable_cents"] == 3180
    assert client.post("/payments", json={
        "payment_id": "P2", "author_id": "A1", "close_id": c2, "amount_cents": 3180,
    }).status_code == 201
    assert client.get("/closes/1").json()["author_totals"][0]["amount_cents"] == 16020
