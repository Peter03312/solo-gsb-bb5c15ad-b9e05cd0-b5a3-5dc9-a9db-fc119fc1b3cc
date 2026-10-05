"""核心账务逻辑：阶梯版税计提、作者分账、退款冲销、关账批次。

业务规则：
- 销售按入账序号（entries.entry_seq，即数据库提交顺序）累计册数，跨档时分档计提；
- 每册版税 = 每册净价 × 该档费率，四舍五入到分（ROUND_HALF_UP）；
- 作者分账：对整笔计提金额，各作者先向下取整到分，剩余分按作者编号顺序依次补齐 1 分；
- 退款从原销售最后一册起按原每册计提冲回，并同步冲回分账；不回退累计销量；
  同一销售的累计退款册数不得超过原册数；
- 关账汇总上一批次之后到本次提交前的全部流水，生成逐笔明细与作者合计的快照报表，
  报表落库后不可修改；关账后补录的旧业务流水按入账序号自然进入下一批次；
- 同号同内容重试返回原结果，同号异内容拒绝（409）。

作者分成版本：
- 作品的作者分成可随合作关系变化：创建作品时的分成为版本 1，之后按
  "作品编号 + 全局唯一变更编号 + 作者编号/比例"提交新版本，版本号按提交顺序递增；
- 作者编号不得重复，比例非负且合计必须为 100；作品不存在或数据不合法拒绝；
- 同变更编号同内容重试返回原版本，异内容拒绝（409，变更编号跨作品亦全局唯一）；
- 版本按提交顺序生效：新销售使用入账时的当前版本分账；旧销售的后续退款
  仍从原销售末册冲回并按原销售版本分账；业务时间不决定版本；
- 作品详情展示当前分成；作品创建的同号同内容重试仍返回创建时结果（版本 1）；
- 退出分成的作者仍可查询并结算既有余额，新作者可参与后续关账与结算；
- 并发变更与入账在同一全局锁与单事务内串行化，先后确定。

付款结算（关账后）：
- 以作者编号、当前最新关账批次编号、付款编号、支付金额（分）提交；
- 应计额 = 截至该批次全部不可改关账快照中该作者净额之和；
- 已付额 = 该作者全部已确认付款之和；可付额 = max(应计额 - 已付额, 0)；
- 关账后退款若使应计额低于已付额，差额为待抵扣负余额，后续关账的正额先抵扣，
  旧报表与旧付款均不改写；
- 支付金额必须为正且不超过可付额；作者/批次不存在、批次非当前最新一律拒绝；
- 同付款编号同内容重试返回原结算结果（即使之后又有关账），异内容拒绝（409）；
- 全部结算写操作在同一全局锁与单事务内完成，并发结算不会超付。

净价更正（销售净价录错）：
- 财务凭全局唯一更正编号、原销售编号与非负新每册净价提交更正；
- 按原销售阶梯费率与入账时分成版本，仅对各档"未退册数"计算新旧净价的每册版税差额；
  逐作者差额 = 未退款总额按新价整体分账 − 按旧价整体分账（各自独立分账后相减）；
- 旧价取该销售的"当前更正价"（首次更正即原销售原价），多次更正串联；
- 更正以独立流水（独立入账序号）进入下一关账批次，不改写历史销售、退款、
  已关账报表与付款；同更正编号同内容重试返回原结果，异内容（含跨销售复用编号）拒绝；
- 销售不存在拒绝（404），已全退拒绝（400），新价等于当前更正价拒绝（409）。

更正后退款：
- 退款本身仍按原销售末册、原价、原分成版本冲回，原有退款字段及金额不变；
- 对被退册数另记"已生效价差的反向流水"（同一事务内独立入账序号，随退款归入同批次）：
  各档反向额 =（当前更正价 − 原价）下该档每册价差 × 本次被退册数；逐作者反向额按
  "未退总额分账之差"望远镜式计算，保证全退后该销售逐作者累计价差恰为 0；
  无已生效价差时不产生反向流水；旧退款不追溯；
- 更正与退款、关账并发时，一律按提交顺序（数据库提交顺序）判定未退册数与批次归属。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from .db import LOCK, get_conn
from .schemas import PaymentIn, PriceCorrectionIn, RefundIn, SaleIn, SplitChangeIn, WorkIn

CENT = Decimal("0.01")
HUNDRED = Decimal(100)


class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _conflict(msg: str) -> ApiError:
    return ApiError(409, msg)


def _not_found(msg: str) -> ApiError:
    return ApiError(404, msg)


def _bad_request(msg: str) -> ApiError:
    return ApiError(400, msg)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def dec_str(d: Decimal) -> str:
    """Decimal 的规范字符串形式（用于存储与幂等指纹，保证数值相等则串相等）。"""
    return format(d.normalize(), "f")


def _fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def author_order_key(author_id: str):
    """作者编号顺序：纯数字按数值排序，其余按字典序。"""
    return (0, int(author_id)) if author_id.isdigit() else (1, author_id)


def per_copy_cents(net_price: Decimal, rate: Decimal) -> int:
    """每册版税（分）：净价 × 费率，四舍五入到分。"""
    yuan = (net_price * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return int(yuan * 100)


def split_cents(total_cents: int, authors: list[dict]) -> list[tuple[str, int]]:
    """作者分账：先向下取整到分，余分按作者编号顺序补齐。

    authors: [{"author_id": str, "ratio": Decimal}]，须已按作者编号顺序排列。
    """
    shares: list[list] = []
    for a in authors:
        exact = Decimal(total_cents) * a["ratio"] / HUNDRED
        shares.append([a["author_id"], int(exact.to_integral_value(rounding=ROUND_FLOOR))])
    remainder = total_cents - sum(s[1] for s in shares)
    i = 0
    while remainder > 0:  # 余分必然小于作者数，逐个补 1 分
        shares[i][1] += 1
        remainder -= 1
        i += 1
    return [(aid, amt) for aid, amt in shares]


def tier_segments(tiers: list[dict], cum_before: int, copies: int) -> list[dict]:
    """按累计销量区间 [cum_before, cum_before+copies) 切分阶梯档。

    返回 [{"tier_idx", "rate", "rate_str", "copies"}]，按档序排列。
    """
    end = cum_before + copies
    out = []
    for idx, tier in enumerate(tiers):
        nxt = tiers[idx + 1]["threshold"] if idx + 1 < len(tiers) else None
        lo = max(cum_before, tier["threshold"])
        hi = end if nxt is None else min(end, nxt)
        if lo < hi:
            out.append(
                {
                    "tier_idx": idx,
                    "rate": tier["rate"],
                    "rate_str": tier["rate_str"],
                    "copies": hi - lo,
                }
            )
    return out


# ---------------------------------------------------------------- 净价更正


def _latest_corrected_price(conn: sqlite3.Connection, sale_id: str) -> Decimal | None:
    """该销售的"当前更正价"：最近一次更正的新价；从未更正则返回 None（即原价）。"""
    r = conn.execute(
        "SELECT new_net_price FROM price_corrections WHERE sale_id=?"
        " ORDER BY entry_seq DESC LIMIT 1",
        (sale_id,),
    ).fetchone()
    return Decimal(r["new_net_price"]) if r is not None else None


def _split_unrefunded_at_price(
    conn: sqlite3.Connection,
    sale: sqlite3.Row,
    authors: list[dict],
    price: Decimal,
    unrefunded_by_seg: dict[int, int],
) -> tuple[int, list[dict], list[tuple[str, int]]]:
    """在指定净价下，对销售各档未退册数计算版税总额与逐作者分账（整体分账）。

    返回 (总额, 各档明细 [{idx, tier_idx, rate_str, per_copy_cents, unrefunded, amount}],
    逐作者分账 [(author_id, amount)])。
    """
    segs = conn.execute(
        "SELECT * FROM sale_segments WHERE sale_id=? ORDER BY idx", (sale["sale_id"],)
    ).fetchall()
    seg_rows: list[dict] = []
    total = 0
    for seg in segs:
        n = unrefunded_by_seg.get(seg["idx"], seg["copies"] - seg["refunded_copies"])
        ppc = per_copy_cents(price, Decimal(seg["rate"]))
        amt = ppc * n
        total += amt
        seg_rows.append(
            {
                "idx": seg["idx"],
                "tier_idx": seg["tier_idx"],
                "rate_str": seg["rate"],
                "per_copy_cents": ppc,
                "unrefunded": n,
                "amount_cents": amt,
            }
        )
    return total, seg_rows, split_cents(total, authors)


def _reversal_splits(
    conn: sqlite3.Connection,
    sale: sqlite3.Row,
    authors: list[dict],
    current_price: Decimal,
    u_before: dict[int, int],
    u_after: dict[int, int],
) -> tuple[list[dict], list[tuple[str, int]], int]:
    """退款时计算"已生效价差反向流水"的各档明细与逐作者带符号反向金额。

    望远镜式：对每个作者，反向额 =
        [SPLIT(当前更正价下退款前未退总额) − SPLIT(原价下退款前未退总额)]
      − [SPLIT(当前更正价下退款后未退总额) − SPLIT(原价下退款后未退总额)]
    其中四个未退总额各自独立整体分账（沿用余分补齐规则），再按作者相减。
    全部退完时退款后未退额为 0，两时点之差恰等于退款前已生效价差，
    故该销售逐作者累计价差（历次更正 − 历次反向）归零，与更正、退款的
    交错顺序无关。
    """
    cur_total_b, cur_segs_b, cur_split_b = _split_unrefunded_at_price(
        conn, sale, authors, current_price, u_before
    )
    orig_total_b, _, orig_split_b = _split_unrefunded_at_price(
        conn, sale, authors, Decimal(sale["net_price"]), u_before
    )
    cur_total_a, cur_segs_a, cur_split_a = _split_unrefunded_at_price(
        conn, sale, authors, current_price, u_after
    )
    orig_total_a, _, orig_split_a = _split_unrefunded_at_price(
        conn, sale, authors, Decimal(sale["net_price"]), u_after
    )
    before = dict(cur_split_b)
    before_ro = dict(orig_split_b)
    after = dict(cur_split_a)
    after_ro = dict(orig_split_a)
    author_ids = sorted(
        {a for a, _ in cur_split_b} | {a for a, _ in cur_split_a}
        | {a for a, _ in orig_split_b} | {a for a, _ in orig_split_a},
        key=author_order_key,
    )
    author_splits = [
        (aid, (before.get(aid, 0) - before_ro.get(aid, 0))
              - (after.get(aid, 0) - after_ro.get(aid, 0)))
        for aid in author_ids
    ]
    # 各档明细：按当前更正价每册版税与原价每册版税之差，乘以本次被退册数
    segs = {s["idx"]: s for s in conn.execute(
        "SELECT * FROM sale_segments WHERE sale_id=? ORDER BY idx", (sale["sale_id"],)
    ).fetchall()}
    seg_rows: list[dict] = []
    for cb in cur_segs_b:
        idx = cb["idx"]
        k = u_before[idx] - u_after[idx]
        if k <= 0:
            continue
        seg = segs[idx]
        orig_ppc = seg["per_copy_cents"]  # 原价、原费率四舍五入后的每册版税（历史不变）
        seg_rows.append(
            {
                "idx": idx,
                "tier_idx": cb["tier_idx"],
                "rate_str": cb["rate_str"],
                "refunded": k,
                "current_per_copy_cents": cb["per_copy_cents"],
                "original_per_copy_cents": orig_ppc,
                "delta_per_copy_cents": cb["per_copy_cents"] - orig_ppc,
                "amount_cents": (cb["per_copy_cents"] - orig_ppc) * k,
            }
        )
    total = (cur_total_b - orig_total_b) - (cur_total_a - orig_total_a)
    return seg_rows, author_splits, total


# ---------------------------------------------------------------- 作品


def _work_fingerprint(p: WorkIn) -> str:
    return _fingerprint(
        {
            "work_id": p.work_id,
            "title": p.title,
            "tiers": [{"threshold": t.threshold, "rate": dec_str(t.rate)} for t in p.tiers],
            "authors": [
                {"author_id": a.author_id, "ratio": dec_str(a.ratio)} for a in p.authors
            ],
        }
    )


def _version_authors(conn: sqlite3.Connection, work_id: str, version: int) -> list[dict]:
    """指定分成版本的作者列表（按作者编号顺序）。"""
    authors = [
        {"author_id": r["author_id"], "ratio": Decimal(r["ratio"]), "ratio_str": r["ratio"]}
        for r in conn.execute(
            "SELECT author_id, ratio FROM work_version_authors"
            " WHERE work_id=? AND version=?",
            (work_id, version),
        )
    ]
    authors.sort(key=lambda a: author_order_key(a["author_id"]))
    return authors


def _current_split_version(conn: sqlite3.Connection, work_id: str) -> int:
    r = conn.execute(
        "SELECT COALESCE(MAX(version),1) AS v FROM work_versions WHERE work_id=?",
        (work_id,),
    ).fetchone()
    return r["v"]


def _load_work(conn: sqlite3.Connection, work_id: str) -> dict:
    row = conn.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
    if row is None:
        raise _not_found(f"作品 {work_id} 不存在")
    tiers = [
        {"threshold": r["threshold"], "rate": Decimal(r["rate"]), "rate_str": r["rate"]}
        for r in conn.execute(
            "SELECT threshold, rate FROM work_tiers WHERE work_id=? ORDER BY idx", (work_id,)
        )
    ]
    version = _current_split_version(conn, work_id)
    return {
        "row": row,
        "tiers": tiers,
        "authors": _version_authors(conn, work_id, version),
        "current_version": version,
    }


def work_response(
    conn: sqlite3.Connection, work_id: str, version: int | None = None
) -> dict:
    """作品视图。version 缺省为当前分成版本；传入版本号则展示该版本的分成。"""
    w = _load_work(conn, work_id)
    if version is None:
        version = w["current_version"]
        authors = w["authors"]
    else:
        authors = _version_authors(conn, work_id, version)
    return {
        "work_id": w["row"]["work_id"],
        "title": w["row"]["title"],
        "created_at": w["row"]["created_at"],
        "split_version": version,
        "tiers": [{"threshold": t["threshold"], "rate": t["rate_str"]} for t in w["tiers"]],
        "authors": [
            {"author_id": a["author_id"], "ratio": a["ratio_str"]} for a in authors
        ],
    }


def create_work(p: WorkIn) -> tuple[dict, bool]:
    fp = _work_fingerprint(p)
    with LOCK:
        conn = get_conn()
        row = conn.execute(
            "SELECT fingerprint FROM works WHERE work_id=?", (p.work_id,)
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"作品 {p.work_id} 已存在且内容不一致")
            # 同号同内容重试 → 返回创建时结果（初始分成版本 1）
            return work_response(conn, p.work_id, version=1), False
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = _utcnow()
            conn.execute(
                "INSERT INTO works(work_id, title, fingerprint, created_at) VALUES(?,?,?,?)",
                (p.work_id, p.title, fp, now),
            )
            for i, t in enumerate(p.tiers):
                conn.execute(
                    "INSERT INTO work_tiers(work_id, idx, threshold, rate) VALUES(?,?,?,?)",
                    (p.work_id, i, t.threshold, dec_str(t.rate)),
                )
            # 初始作者分成 = 版本 1（无变更编号）
            conn.execute(
                "INSERT INTO work_versions(work_id, version, change_id, fingerprint,"
                " created_at) VALUES(?,1,NULL,?,?)",
                (p.work_id, fp, now),
            )
            for a in p.authors:
                conn.execute(
                    "INSERT INTO work_version_authors(work_id, version, author_id, ratio)"
                    " VALUES(?,1,?,?)",
                    (p.work_id, a.author_id, dec_str(a.ratio)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return work_response(conn, p.work_id, version=1), True


def get_work(work_id: str) -> dict:
    with LOCK:
        return work_response(get_conn(), work_id)


def work_stats(work_id: str) -> dict:
    with LOCK:
        conn = get_conn()
        _load_work(conn, work_id)  # 不存在则 404
        row = conn.execute(
            "SELECT COALESCE(MAX(cum_after),0) AS cum, COUNT(*) AS n,"
            " COALESCE(SUM(copies),0) AS sold FROM sales WHERE work_id=?",
            (work_id,),
        ).fetchone()
        refunded = conn.execute(
            "SELECT COALESCE(SUM(r.copies),0) AS c FROM refunds r"
            " JOIN sales s ON s.sale_id=r.sale_id WHERE s.work_id=?",
            (work_id,),
        ).fetchone()["c"]
        return {
            "work_id": work_id,
            "cumulative_copies": row["cum"],
            "sales_count": row["n"],
            "sold_copies": row["sold"],
            "refunded_copies": refunded,
            "net_copies": row["sold"] - refunded,
        }


# ---------------------------------------------------------------- 作者分成版本


def _split_change_fingerprint(work_id: str, p: SplitChangeIn) -> str:
    # 作者列表按编号排序后参与指纹：同一组作者不同提交顺序视为同内容
    authors = sorted(p.authors, key=lambda a: author_order_key(a.author_id))
    return _fingerprint(
        {
            "change_id": p.change_id,
            "work_id": work_id,
            "authors": [
                {"author_id": a.author_id, "ratio": dec_str(a.ratio)} for a in authors
            ],
        }
    )


def _version_response(conn: sqlite3.Connection, work_id: str, version: int) -> dict:
    v = conn.execute(
        "SELECT * FROM work_versions WHERE work_id=? AND version=?", (work_id, version)
    ).fetchone()
    if v is None:
        raise _not_found(f"作品 {work_id} 的分成版本 {version} 不存在")
    return {
        "work_id": work_id,
        "version": v["version"],
        "change_id": v["change_id"],
        "authors": [
            {"author_id": a["author_id"], "ratio": a["ratio_str"]}
            for a in _version_authors(conn, work_id, version)
        ],
        "created_at": v["created_at"],
    }


def submit_split_change(work_id: str, p: SplitChangeIn) -> tuple[dict, bool]:
    """提交作者分成新版本。版本号按提交顺序（数据库提交顺序）递增。"""
    fp = _split_change_fingerprint(work_id, p)
    with LOCK:
        conn = get_conn()
        # 变更编号全局唯一：同号同内容 → 返回原版本；异内容（含跨作品）→ 409
        row = conn.execute(
            "SELECT work_id, version, fingerprint FROM work_versions WHERE change_id=?",
            (p.change_id,),
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"变更编号 {p.change_id} 已存在且内容不一致")
            return _version_response(conn, row["work_id"], row["version"]), False
        if conn.execute(
            "SELECT 1 FROM works WHERE work_id=?", (work_id,)
        ).fetchone() is None:
            raise _not_found(f"作品 {work_id} 不存在")
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM work_versions"
                " WHERE work_id=?",
                (work_id,),
            ).fetchone()["v"]
            conn.execute(
                "INSERT INTO work_versions(work_id, version, change_id, fingerprint,"
                " created_at) VALUES(?,?,?,?,?)",
                (work_id, version, p.change_id, fp, _utcnow()),
            )
            for a in p.authors:
                conn.execute(
                    "INSERT INTO work_version_authors(work_id, version, author_id, ratio)"
                    " VALUES(?,?,?,?)",
                    (work_id, version, a.author_id, dec_str(a.ratio)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _version_response(conn, work_id, version), True


def list_split_versions(work_id: str) -> dict:
    """当前及历次分成版本（按版本号升序）。"""
    with LOCK:
        conn = get_conn()
        _load_work(conn, work_id)  # 不存在则 404
        versions = [
            _version_response(conn, work_id, r["version"])
            for r in conn.execute(
                "SELECT version FROM work_versions WHERE work_id=? ORDER BY version",
                (work_id,),
            )
        ]
        return {
            "work_id": work_id,
            "current_version": versions[-1]["version"],
            "versions": versions,
        }


def get_split_version(work_id: str, version: int) -> dict:
    with LOCK:
        conn = get_conn()
        _load_work(conn, work_id)  # 不存在则 404
        return _version_response(conn, work_id, version)


# ---------------------------------------------------------------- 销售


def _sale_fingerprint(p: SaleIn) -> str:
    return _fingerprint(
        {
            "sale_id": p.sale_id,
            "work_id": p.work_id,
            "business_time": p.business_time.isoformat(),
            "copies": p.copies,
            "net_price": dec_str(p.net_price),
        }
    )


def sale_response(conn: sqlite3.Connection, sale_id: str) -> dict:
    s = conn.execute("SELECT * FROM sales WHERE sale_id=?", (sale_id,)).fetchone()
    if s is None:
        raise _not_found(f"销售流水 {sale_id} 不存在")
    segs = conn.execute(
        "SELECT * FROM sale_segments WHERE sale_id=? ORDER BY idx", (sale_id,)
    ).fetchall()
    splits = conn.execute(
        "SELECT author_id, amount_cents FROM sale_splits WHERE sale_id=?", (sale_id,)
    ).fetchall()
    return {
        "type": "sale",
        "sale_id": s["sale_id"],
        "work_id": s["work_id"],
        "business_time": s["business_time"],
        "copies": s["copies"],
        "net_price": s["net_price"],
        "entry_seq": s["entry_seq"],
        "cum_before": s["cum_before"],
        "cum_after": s["cum_after"],
        "split_version": s["split_version"],
        "segments": [
            {
                "segment_index": g["idx"],
                "tier_index": g["tier_idx"],
                "rate": g["rate"],
                "copies": g["copies"],
                "per_copy_royalty_cents": g["per_copy_cents"],
                "amount_cents": g["amount_cents"],
                "refunded_copies": g["refunded_copies"],
            }
            for g in segs
        ],
        "total_royalty_cents": s["total_royalty_cents"],
        "splits": [
            {"author_id": x["author_id"], "amount_cents": x["amount_cents"]}
            for x in sorted(splits, key=lambda x: author_order_key(x["author_id"]))
        ],
        "refunded_copies": sum(g["refunded_copies"] for g in segs),
        "close_id": s["close_id"],
        "created_at": s["created_at"],
    }


def record_sale(p: SaleIn) -> tuple[dict, bool]:
    fp = _sale_fingerprint(p)
    with LOCK:
        conn = get_conn()
        row = conn.execute(
            "SELECT fingerprint FROM sales WHERE sale_id=?", (p.sale_id,)
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"销售流水 {p.sale_id} 已存在且内容不一致")
            return sale_response(conn, p.sale_id), False
        work = _load_work(conn, p.work_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cum_before = conn.execute(
                "SELECT COALESCE(MAX(cum_after),0) AS c FROM sales WHERE work_id=?",
                (p.work_id,),
            ).fetchone()["c"]
            cum_after = cum_before + p.copies
            total = 0
            seg_rows = []
            for seg in tier_segments(work["tiers"], cum_before, p.copies):
                ppc = per_copy_cents(p.net_price, seg["rate"])
                amt = ppc * seg["copies"]
                total += amt
                seg_rows.append((seg["tier_idx"], seg["rate_str"], seg["copies"], ppc, amt))
            splits = split_cents(total, work["authors"])
            entry_seq = conn.execute(
                "INSERT INTO entries(kind, ref_id) VALUES('sale', ?)", (p.sale_id,)
            ).lastrowid
            conn.execute(
                "INSERT INTO sales(sale_id, work_id, business_time, copies, net_price,"
                " entry_seq, cum_before, cum_after, split_version, total_royalty_cents,"
                " fingerprint, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p.sale_id,
                    p.work_id,
                    p.business_time.isoformat(),
                    p.copies,
                    dec_str(p.net_price),
                    entry_seq,
                    cum_before,
                    cum_after,
                    work["current_version"],
                    total,
                    fp,
                    _utcnow(),
                ),
            )
            for idx, (tier_idx, rate_str, n, ppc, amt) in enumerate(seg_rows):
                conn.execute(
                    "INSERT INTO sale_segments(sale_id, idx, tier_idx, rate, copies,"
                    " per_copy_cents, amount_cents) VALUES(?,?,?,?,?,?,?)",
                    (p.sale_id, idx, tier_idx, rate_str, n, ppc, amt),
                )
            for aid, amt in splits:
                conn.execute(
                    "INSERT INTO sale_splits(sale_id, author_id, amount_cents) VALUES(?,?,?)",
                    (p.sale_id, aid, amt),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return sale_response(conn, p.sale_id), True


def get_sale(sale_id: str) -> dict:
    with LOCK:
        return sale_response(get_conn(), sale_id)


# ---------------------------------------------------------------- 退款


def _refund_fingerprint(p: RefundIn) -> str:
    return _fingerprint(
        {"refund_id": p.refund_id, "sale_id": p.sale_id, "copies": p.copies}
    )


def reversal_response(conn: sqlite3.Connection, refund_id: str) -> dict | None:
    """退款附带的"已生效价差反向流水"；不存在（旧退款/无价差）时返回 None。"""
    r = conn.execute(
        "SELECT * FROM price_delta_reversals WHERE refund_id=?", (refund_id,)
    ).fetchone()
    if r is None:
        return None
    sale = conn.execute(
        "SELECT work_id FROM sales WHERE sale_id=?", (r["sale_id"],)
    ).fetchone()
    segs = conn.execute(
        "SELECT * FROM price_delta_reversal_segments WHERE refund_id=? ORDER BY idx",
        (refund_id,),
    ).fetchall()
    splits = conn.execute(
        "SELECT author_id, amount_cents FROM price_delta_reversal_splits"
        " WHERE refund_id=?",
        (refund_id,),
    ).fetchall()
    return {
        "type": "price_delta_reversal",
        "refund_id": r["refund_id"],
        "sale_id": r["sale_id"],
        "work_id": sale["work_id"],
        "entry_seq": r["entry_seq"],
        "split_version": r["split_version"],
        "segments": [
            {
                "segment_index": g["idx"],
                "sale_segment_index": g["sale_segment_idx"],
                "tier_index": g["tier_idx"],
                "rate": g["rate"],
                "copies": g["refunded_copies"],
                "current_per_copy_royalty_cents": g["current_per_copy_cents"],
                "original_per_copy_royalty_cents": g["original_per_copy_cents"],
                "delta_per_copy_cents": g["delta_per_copy_cents"],
                "amount_cents": g["amount_cents"],
            }
            for g in segs
        ],
        "splits": [
            {"author_id": x["author_id"], "amount_cents": x["amount_cents"]}
            for x in sorted(splits, key=lambda x: author_order_key(x["author_id"]))
        ],
        "total_reversal_cents": r["total_reversal_cents"],
        "close_id": r["close_id"],
        "created_at": r["created_at"],
    }


def refund_response(conn: sqlite3.Connection, refund_id: str) -> dict:
    r = conn.execute("SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone()
    if r is None:
        raise _not_found(f"退款流水 {refund_id} 不存在")
    sale = conn.execute(
        "SELECT work_id, split_version FROM sales WHERE sale_id=?", (r["sale_id"],)
    ).fetchone()
    segs = conn.execute(
        "SELECT rs.idx, rs.sale_segment_idx, rs.copies, rs.per_copy_cents, rs.amount_cents,"
        " ss.tier_idx, ss.rate FROM refund_segments rs"
        " JOIN sale_segments ss ON ss.sale_id=? AND ss.idx=rs.sale_segment_idx"
        " WHERE rs.refund_id=? ORDER BY rs.idx",
        (r["sale_id"], refund_id),
    ).fetchall()
    splits = conn.execute(
        "SELECT author_id, amount_cents FROM refund_splits WHERE refund_id=?", (refund_id,)
    ).fetchall()
    return {
        "type": "refund",
        "refund_id": r["refund_id"],
        "sale_id": r["sale_id"],
        "work_id": sale["work_id"],
        "copies": r["copies"],
        "entry_seq": r["entry_seq"],
        "split_version": sale["split_version"],  # 按原销售入账时的分成版本分账
        "segments": [
            {
                "segment_index": g["idx"],
                "sale_segment_index": g["sale_segment_idx"],
                "tier_index": g["tier_idx"],
                "rate": g["rate"],
                "copies": g["copies"],
                "per_copy_royalty_cents": g["per_copy_cents"],
                "amount_cents": g["amount_cents"],
            }
            for g in segs
        ],
        "total_refund_cents": r["total_refund_cents"],
        "splits": [
            {"author_id": x["author_id"], "amount_cents": x["amount_cents"]}
            for x in sorted(splits, key=lambda x: author_order_key(x["author_id"]))
        ],
        "close_id": r["close_id"],
        "created_at": r["created_at"],
        # 被退册数已生效价差的反向流水（旧退款或无价差时为 null）；
        # 仅为展示与关账快照用途，原有退款字段及金额均不变
        "price_delta_reversal": reversal_response(conn, refund_id),
    }


def record_refund(p: RefundIn) -> tuple[dict, bool]:
    fp = _refund_fingerprint(p)
    with LOCK:
        conn = get_conn()
        row = conn.execute(
            "SELECT fingerprint FROM refunds WHERE refund_id=?", (p.refund_id,)
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"退款流水 {p.refund_id} 已存在且内容不一致")
            return refund_response(conn, p.refund_id), False
        sale = conn.execute("SELECT * FROM sales WHERE sale_id=?", (p.sale_id,)).fetchone()
        if sale is None:
            raise _not_found(f"原销售 {p.sale_id} 不存在")
        # 旧销售的后续退款仍按原销售入账时的分成版本分账（与当前版本无关）
        authors = _version_authors(conn, sale["work_id"], sale["split_version"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            already = conn.execute(
                "SELECT COALESCE(SUM(copies),0) AS c FROM refunds WHERE sale_id=?",
                (p.sale_id,),
            ).fetchone()["c"]
            if already + p.copies > sale["copies"]:
                raise _bad_request(
                    f"累计退款册数({already + p.copies})超过原销售册数({sale['copies']})"
                )
            segs = conn.execute(
                "SELECT * FROM sale_segments WHERE sale_id=? ORDER BY idx", (p.sale_id,)
            ).fetchall()
            # 从原销售最后一册起冲回：按分段倒序消耗
            to_go = p.copies
            portions = []
            for seg in reversed(segs):
                if to_go == 0:
                    break
                take = min(seg["copies"] - seg["refunded_copies"], to_go)
                if take > 0:
                    portions.append((seg, take))
                    to_go -= take
            portions.reverse()  # 展示时按分段正序
            total = sum(take * seg["per_copy_cents"] for seg, take in portions)
            splits = split_cents(total, authors)

            # 被退册数的"已生效价差反向流水"（仅当该销售存在已生效更正时）：
            # 各档未退册数在 UPDATE 前后的快照，交给望远镜式反向分账计算；
            # 全退后逐作者累计价差为 0。旧退款（无更正）不产生反向流水。
            takes_by_idx = {seg["idx"]: take for seg, take in portions}
            u_before = {
                seg["idx"]: seg["copies"] - seg["refunded_copies"] for seg in segs
            }
            u_after = {idx: n - takes_by_idx.get(idx, 0) for idx, n in u_before.items()}
            current_price = _latest_corrected_price(conn, p.sale_id)
            reversal = None
            if current_price is not None:
                rev_segs, rev_splits, rev_total = _reversal_splits(
                    conn, sale, authors, current_price, u_before, u_after
                )
                if any(amt != 0 for _, amt in rev_splits):
                    reversal = (rev_segs, rev_splits, rev_total)

            entry_seq = conn.execute(
                "INSERT INTO entries(kind, ref_id) VALUES('refund', ?)", (p.refund_id,)
            ).lastrowid
            conn.execute(
                "INSERT INTO refunds(refund_id, sale_id, copies, entry_seq,"
                " total_refund_cents, fingerprint, created_at) VALUES(?,?,?,?,?,?,?)",
                (p.refund_id, p.sale_id, p.copies, entry_seq, total, fp, _utcnow()),
            )
            for idx, (seg, take) in enumerate(portions):
                conn.execute(
                    "INSERT INTO refund_segments(refund_id, idx, sale_segment_idx, copies,"
                    " per_copy_cents, amount_cents) VALUES(?,?,?,?,?,?)",
                    (p.refund_id, idx, seg["idx"], take, seg["per_copy_cents"],
                     take * seg["per_copy_cents"]),
                )
                conn.execute(
                    "UPDATE sale_segments SET refunded_copies=refunded_copies+?"
                    " WHERE sale_id=? AND idx=?",
                    (take, p.sale_id, seg["idx"]),
                )
            for aid, amt in splits:
                conn.execute(
                    "INSERT INTO refund_splits(refund_id, author_id, amount_cents)"
                    " VALUES(?,?,?)",
                    (p.refund_id, aid, amt),
                )
            if reversal is not None:
                # 反向流水独立入账序号、与退款同一事务 ⇒ 随退款归入同一关账批次
                rev_segs, rev_splits, rev_total = reversal
                rev_entry_seq = conn.execute(
                    "INSERT INTO entries(kind, ref_id) VALUES('price_delta_reversal', ?)",
                    (p.refund_id,),
                ).lastrowid
                conn.execute(
                    "INSERT INTO price_delta_reversals(refund_id, sale_id, entry_seq,"
                    " split_version, total_reversal_cents, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (p.refund_id, p.sale_id, rev_entry_seq, sale["split_version"],
                     rev_total, _utcnow()),
                )
                for ridx, g in enumerate(rev_segs):
                    conn.execute(
                        "INSERT INTO price_delta_reversal_segments(refund_id, idx,"
                        " sale_segment_idx, tier_idx, rate, refunded_copies,"
                        " current_per_copy_cents, original_per_copy_cents,"
                        " delta_per_copy_cents, amount_cents) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (p.refund_id, ridx, g["idx"], g["tier_idx"], g["rate_str"],
                         g["refunded"], g["current_per_copy_cents"],
                         g["original_per_copy_cents"], g["delta_per_copy_cents"],
                         g["amount_cents"]),
                    )
                for aid, amt in rev_splits:
                    conn.execute(
                        "INSERT INTO price_delta_reversal_splits(refund_id, author_id,"
                        " amount_cents) VALUES(?,?,?)",
                        (p.refund_id, aid, amt),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return refund_response(conn, p.refund_id), True


def get_refund(refund_id: str) -> dict:
    with LOCK:
        return refund_response(get_conn(), refund_id)


# ---------------------------------------------------------------- 净价更正


def _price_correction_fingerprint(p: PriceCorrectionIn) -> str:
    return _fingerprint(
        {
            "correction_id": p.correction_id,
            "sale_id": p.sale_id,
            "new_net_price": dec_str(p.new_net_price),
        }
    )


def correction_response(conn: sqlite3.Connection, correction_id: str) -> dict:
    c = conn.execute(
        "SELECT * FROM price_corrections WHERE correction_id=?", (correction_id,)
    ).fetchone()
    if c is None:
        raise _not_found(f"净价更正 {correction_id} 不存在")
    sale = conn.execute(
        "SELECT work_id, entry_seq FROM sales WHERE sale_id=?", (c["sale_id"],)
    ).fetchone()
    segs = conn.execute(
        "SELECT * FROM price_correction_segments WHERE correction_id=? ORDER BY idx",
        (correction_id,),
    ).fetchall()
    splits = conn.execute(
        "SELECT author_id, amount_cents FROM price_correction_splits"
        " WHERE correction_id=?",
        (correction_id,),
    ).fetchall()
    return {
        "type": "price_correction",
        "correction_id": c["correction_id"],
        "sale_id": c["sale_id"],
        "work_id": sale["work_id"],
        "sale_entry_seq": sale["entry_seq"],  # 关联原销售入账序号
        "entry_seq": c["entry_seq"],
        "old_net_price": c["old_net_price"],
        "new_net_price": c["new_net_price"],
        "split_version": c["split_version"],  # 原销售入账时的分成版本
        "segments": [
            {
                "segment_index": g["idx"],
                "sale_segment_index": g["sale_segment_idx"],
                "tier_index": g["tier_idx"],
                "rate": g["rate"],
                "unrefunded_copies": g["unrefunded_copies"],
                "old_per_copy_royalty_cents": g["old_per_copy_cents"],
                "new_per_copy_royalty_cents": g["new_per_copy_cents"],
                "delta_per_copy_cents": g["delta_per_copy_cents"],
                "amount_cents": g["amount_cents"],
            }
            for g in segs
        ],
        "splits": [
            {"author_id": x["author_id"], "amount_cents": x["amount_cents"]}
            for x in sorted(splits, key=lambda x: author_order_key(x["author_id"]))
        ],
        "total_delta_cents": c["total_delta_cents"],
        "close_id": c["close_id"],
        "created_at": c["created_at"],
    }


def submit_price_correction(p: PriceCorrectionIn) -> tuple[dict, bool]:
    """提交销售净价更正。返回 (更正结果, 是否新建)。"""
    fp = _price_correction_fingerprint(p)
    with LOCK:
        conn = get_conn()
        # 更正编号全局唯一（跨销售复用也算冲突）：同号同内容 → 原结果，异内容 → 409
        row = conn.execute(
            "SELECT fingerprint FROM price_corrections WHERE correction_id=?",
            (p.correction_id,),
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"更正编号 {p.correction_id} 已存在且内容不一致")
            return correction_response(conn, p.correction_id), False
        sale = conn.execute("SELECT * FROM sales WHERE sale_id=?", (p.sale_id,)).fetchone()
        if sale is None:
            raise _not_found(f"原销售 {p.sale_id} 不存在")
        # 旧价 = 当前更正价（首次更正即原销售原价）
        old_price = _latest_corrected_price(conn, p.sale_id)
        if old_price is None:
            old_price = Decimal(sale["net_price"])
        if p.new_net_price == old_price:
            raise _conflict(
                f"新净价 {dec_str(p.new_net_price)} 等于销售 {p.sale_id} 的当前更正价"
            )
        # 差额始终按原销售入账时的分成版本分账（与当前版本无关）
        authors = _version_authors(conn, sale["work_id"], sale["split_version"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            segs = conn.execute(
                "SELECT * FROM sale_segments WHERE sale_id=? ORDER BY idx", (p.sale_id,)
            ).fetchall()
            unrefunded = {g["idx"]: g["copies"] - g["refunded_copies"] for g in segs}
            if all(n == 0 for n in unrefunded.values()):
                raise _bad_request(f"销售 {p.sale_id} 已全部退款，无法更正净价")
            old_total, old_seg_rows, old_splits = _split_unrefunded_at_price(
                conn, sale, authors, old_price, unrefunded
            )
            new_total, new_seg_rows, new_splits = _split_unrefunded_at_price(
                conn, sale, authors, p.new_net_price, unrefunded
            )
            old_split_map = dict(old_splits)
            delta_splits = [
                (aid, dict(new_splits).get(aid, 0) - old_split_map.get(aid, 0))
                for aid, _ in old_splits
            ]
            seg_rows: list[tuple] = []
            for ng, og in zip(new_seg_rows, old_seg_rows):
                n = ng["unrefunded"]
                if n == 0:
                    continue
                seg_rows.append(
                    (
                        ng["idx"], ng["tier_idx"], ng["rate_str"], n,
                        og["per_copy_cents"], ng["per_copy_cents"],
                        ng["per_copy_cents"] - og["per_copy_cents"],
                        (ng["per_copy_cents"] - og["per_copy_cents"]) * n,
                    )
                )
            total_delta = new_total - old_total
            entry_seq = conn.execute(
                "INSERT INTO entries(kind, ref_id) VALUES('price_correction', ?)",
                (p.correction_id,),
            ).lastrowid
            conn.execute(
                "INSERT INTO price_corrections(correction_id, sale_id, entry_seq,"
                " old_net_price, new_net_price, split_version, total_delta_cents,"
                " fingerprint, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    p.correction_id, p.sale_id, entry_seq,
                    dec_str(old_price), dec_str(p.new_net_price),
                    sale["split_version"], total_delta, fp, _utcnow(),
                ),
            )
            for idx, (sidx, tier_idx, rate_str, n, old_ppc, new_ppc, d_ppc,
                      amt) in enumerate(seg_rows):
                conn.execute(
                    "INSERT INTO price_correction_segments(correction_id, idx,"
                    " sale_segment_idx, tier_idx, rate, unrefunded_copies,"
                    " old_per_copy_cents, new_per_copy_cents, delta_per_copy_cents,"
                    " amount_cents) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (p.correction_id, idx, sidx, tier_idx, rate_str, n,
                     old_ppc, new_ppc, d_ppc, amt),
                )
            for aid, amt in delta_splits:
                conn.execute(
                    "INSERT INTO price_correction_splits(correction_id, author_id,"
                    " amount_cents) VALUES(?,?,?)",
                    (p.correction_id, aid, amt),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return correction_response(conn, p.correction_id), True


def get_price_correction(correction_id: str) -> dict:
    with LOCK:
        return correction_response(get_conn(), correction_id)


def list_price_corrections(sale_id: str | None = None) -> dict:
    """按更正编号列出更正流水；可按原销售编号过滤。"""
    with LOCK:
        conn = get_conn()
        if sale_id is None:
            rows = conn.execute(
                "SELECT correction_id FROM price_corrections ORDER BY entry_seq"
            ).fetchall()
        else:
            if conn.execute(
                "SELECT 1 FROM sales WHERE sale_id=?", (sale_id,)
            ).fetchone() is None:
                raise _not_found(f"原销售 {sale_id} 不存在")
            rows = conn.execute(
                "SELECT correction_id FROM price_corrections WHERE sale_id=?"
                " ORDER BY entry_seq",
                (sale_id,),
            ).fetchall()
        return {"corrections": [correction_response(conn, r["correction_id"]) for r in rows]}


# ---------------------------------------------------------------- 关账


def close_batch(request_id: str | None) -> tuple[dict, bool]:
    """关账：汇总上一批次之后到当前的全部流水，生成不可改报表。"""
    with LOCK:
        conn = get_conn()
        if request_id is not None:
            row = conn.execute(
                "SELECT close_id FROM closes WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                return _close_report(conn, row["close_id"]), False
        conn.execute("BEGIN IMMEDIATE")
        try:
            prev = conn.execute(
                "SELECT COALESCE(MAX(last_entry_seq),0) AS c FROM closes"
            ).fetchone()["c"]
            cur_seq = conn.execute(
                "SELECT COALESCE(MAX(entry_seq),0) AS c FROM entries"
            ).fetchone()["c"]
            close_id = conn.execute(
                "INSERT INTO closes(request_id, from_entry_seq, last_entry_seq, created_at)"
                " VALUES(?,?,?,?)",
                (request_id, prev, cur_seq, _utcnow()),
            ).lastrowid
            entries = conn.execute(
                "SELECT entry_seq, kind, ref_id FROM entries"
                " WHERE entry_seq>? AND entry_seq<=? ORDER BY entry_seq",
                (prev, cur_seq),
            ).fetchall()
            totals: dict[str, int] = {}
            for e in entries:
                if e["kind"] == "sale":
                    payload = sale_response(conn, e["ref_id"])
                    sign = 1
                elif e["kind"] == "refund":
                    payload = refund_response(conn, e["ref_id"])
                    sign = -1
                elif e["kind"] == "price_correction":
                    # 更正为独立价差流水：补提为正、冲回为负
                    payload = correction_response(conn, e["ref_id"])
                    sign = 1
                else:
                    # 价差反向流水：金额带符号，关账时统一按负贡献
                    # （减价差 amount>0 → 负贡献；补价差 amount<0 → 正贡献）
                    payload = reversal_response(conn, e["ref_id"])
                    sign = -1
                payload["close_id"] = close_id
                conn.execute(
                    "INSERT INTO close_lines(close_id, entry_seq, line_type, ref_id, payload)"
                    " VALUES(?,?,?,?,?)",
                    (close_id, e["entry_seq"], e["kind"], e["ref_id"],
                     json.dumps(payload, ensure_ascii=False)),
                )
                for s in payload["splits"]:
                    totals[s["author_id"]] = (
                        totals.get(s["author_id"], 0) + sign * s["amount_cents"]
                    )
            for aid, amt in totals.items():
                conn.execute(
                    "INSERT INTO close_author_totals(close_id, author_id, amount_cents)"
                    " VALUES(?,?,?)",
                    (close_id, aid, amt),
                )
            conn.execute(
                "UPDATE sales SET close_id=? WHERE entry_seq>? AND entry_seq<=?",
                (close_id, prev, cur_seq),
            )
            conn.execute(
                "UPDATE refunds SET close_id=? WHERE entry_seq>? AND entry_seq<=?",
                (close_id, prev, cur_seq),
            )
            conn.execute(
                "UPDATE price_corrections SET close_id=? WHERE entry_seq>? AND entry_seq<=?",
                (close_id, prev, cur_seq),
            )
            conn.execute(
                "UPDATE price_delta_reversals SET close_id=? WHERE entry_seq>? AND entry_seq<=?",
                (close_id, prev, cur_seq),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _close_report(conn, close_id), True


def _close_report(conn: sqlite3.Connection, close_id: int) -> dict:
    c = conn.execute("SELECT * FROM closes WHERE close_id=?", (close_id,)).fetchone()
    if c is None:
        raise _not_found(f"关账批次 {close_id} 不存在")
    lines = [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM close_lines WHERE close_id=? ORDER BY entry_seq",
            (close_id,),
        )
    ]
    totals = conn.execute(
        "SELECT author_id, amount_cents FROM close_author_totals WHERE close_id=?",
        (close_id,),
    ).fetchall()
    sales_total = sum(l["total_royalty_cents"] for l in lines if l["type"] == "sale")
    refund_total = sum(l["total_refund_cents"] for l in lines if l["type"] == "refund")
    correction_total = sum(
        l["total_delta_cents"] for l in lines if l["type"] == "price_correction"
    )
    reversal_total = sum(
        l["total_reversal_cents"]
        for l in lines
        if l["type"] == "price_delta_reversal"
    )
    return {
        "close_id": c["close_id"],
        "request_id": c["request_id"],
        "created_at": c["created_at"],
        "entry_seq_from": c["from_entry_seq"] + 1,
        "entry_seq_to": c["last_entry_seq"],
        "sales_count": sum(1 for l in lines if l["type"] == "sale"),
        "refunds_count": sum(1 for l in lines if l["type"] == "refund"),
        "price_corrections_count": sum(
            1 for l in lines if l["type"] == "price_correction"
        ),
        "price_delta_reversals_count": sum(
            1 for l in lines if l["type"] == "price_delta_reversal"
        ),
        "lines": lines,
        "author_totals": [
            {"author_id": t["author_id"], "amount_cents": t["amount_cents"]}
            for t in sorted(totals, key=lambda t: author_order_key(t["author_id"]))
        ],
        "total_sales_royalty_cents": sales_total,
        "total_refund_cents": refund_total,
        # 净价更正补提（正）/冲回（负）合计，及价差反向流水的带符号合计
        "total_price_correction_cents": correction_total,
        "total_price_delta_reversal_cents": reversal_total,
        "net_royalty_cents": sales_total - refund_total,
        # 含更正与反向流水的作者净分账合计（= author_totals 之和）
        "total_author_net_cents": sales_total - refund_total + correction_total - reversal_total,
    }


def get_close(close_id: int) -> dict:
    with LOCK:
        return _close_report(get_conn(), close_id)


def list_closes() -> dict:
    with LOCK:
        conn = get_conn()
        rows = conn.execute(
            "SELECT c.close_id, c.request_id, c.created_at, c.from_entry_seq,"
            " c.last_entry_seq, COUNT(l.entry_seq) AS line_count"
            " FROM closes c LEFT JOIN close_lines l ON l.close_id=c.close_id"
            " GROUP BY c.close_id ORDER BY c.close_id"
        ).fetchall()
        return {
            "closes": [
                {
                    "close_id": r["close_id"],
                    "request_id": r["request_id"],
                    "created_at": r["created_at"],
                    "entry_seq_from": r["from_entry_seq"] + 1,
                    "entry_seq_to": r["last_entry_seq"],
                    "line_count": r["line_count"],
                }
                for r in rows
            ]
        }


# ---------------------------------------------------------------- 付款结算


def _author_exists(conn: sqlite3.Connection, author_id: str) -> bool:
    # 任一分成版本（含历史版本）中出现过的作者均视为存在：
    # 退出分成的作者仍可查询并结算既有余额，新作者可参与后续关账与结算
    return (
        conn.execute(
            "SELECT 1 FROM work_version_authors WHERE author_id=? LIMIT 1", (author_id,)
        ).fetchone()
        is not None
    )


def _latest_close_id(conn: sqlite3.Connection) -> int | None:
    r = conn.execute("SELECT MAX(close_id) AS c FROM closes").fetchone()
    return r["c"]


def _accrued_cents(conn: sqlite3.Connection, author_id: str, through_close_id: int) -> int:
    """截至指定批次，所有不可改关账快照中该作者净额之和。"""
    r = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS c FROM close_author_totals"
        " WHERE author_id=? AND close_id<=?",
        (author_id, through_close_id),
    ).fetchone()
    return r["c"]


def _paid_cents(conn: sqlite3.Connection, author_id: str) -> int:
    r = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS c FROM payments WHERE author_id=?",
        (author_id,),
    ).fetchone()
    return r["c"]


def _payment_fingerprint(p: PaymentIn) -> str:
    return _fingerprint(
        {
            "payment_id": p.payment_id,
            "author_id": p.author_id,
            "close_id": p.close_id,
            "amount_cents": p.amount_cents,
        }
    )


def _payment_response_from_row(row: sqlite3.Row) -> dict:
    accrued = row["accrued_cents"]
    paid_after = row["paid_before_cents"] + row["amount_cents"]
    return {
        "payment_id": row["payment_id"],
        "author_id": row["author_id"],
        "close_id": row["close_id"],
        "amount_cents": row["amount_cents"],
        "accrued_cents": accrued,
        "paid_before_cents": row["paid_before_cents"],
        "payable_cents": row["payable_cents"],
        "paid_after_cents": paid_after,
        "negative_balance_cents": max(paid_after - accrued, 0),
        "created_at": row["created_at"],
    }


def submit_payment(p: PaymentIn) -> tuple[dict, bool]:
    """提交作者付款结算。返回 (结算结果, 是否新建)。"""
    fp = _payment_fingerprint(p)
    with LOCK:
        conn = get_conn()
        # 幂等优先：同付款编号同内容即使批次已不再是最新，也返回原结算结果
        row = conn.execute(
            "SELECT * FROM payments WHERE payment_id=?", (p.payment_id,)
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fp:
                raise _conflict(f"付款编号 {p.payment_id} 已存在且内容不一致")
            return _payment_response_from_row(row), False
        if not _author_exists(conn, p.author_id):
            raise _not_found(f"作者 {p.author_id} 不存在")
        if conn.execute(
            "SELECT 1 FROM closes WHERE close_id=?", (p.close_id,)
        ).fetchone() is None:
            raise _not_found(f"关账批次 {p.close_id} 不存在")
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = _latest_close_id(conn)
            if p.close_id != latest:
                raise _conflict(
                    f"关账批次 {p.close_id} 不是当前最新批次（最新为 {latest}），拒绝结算"
                )
            accrued = _accrued_cents(conn, p.author_id, p.close_id)
            paid_before = _paid_cents(conn, p.author_id)
            payable = max(accrued - paid_before, 0)
            if p.amount_cents > payable:
                raise _bad_request(
                    f"支付金额 {p.amount_cents} 分超过可付额 {payable} 分"
                    f"（应计 {accrued} - 已付 {paid_before}）"
                )
            cur = conn.execute(
                "INSERT INTO payments(payment_id, author_id, close_id, amount_cents,"
                " accrued_cents, paid_before_cents, payable_cents, fingerprint, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    p.payment_id,
                    p.author_id,
                    p.close_id,
                    p.amount_cents,
                    accrued,
                    paid_before,
                    payable,
                    fp,
                    _utcnow(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM payments WHERE rowid=?", (cur.lastrowid,)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _payment_response_from_row(row), True


def get_payment(payment_id: str) -> dict:
    with LOCK:
        row = get_conn().execute(
            "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if row is None:
            raise _not_found(f"付款 {payment_id} 不存在")
        return _payment_response_from_row(row)


def author_settlement(author_id: str) -> dict:
    """按作者查询：分批应计、累计应计、已付额、负余额、可付额及全部付款记录。"""
    with LOCK:
        conn = get_conn()
        if not _author_exists(conn, author_id):
            raise _not_found(f"作者 {author_id} 不存在")
        batches = conn.execute(
            "SELECT c.close_id, c.created_at, COALESCE(t.amount_cents,0) AS amount_cents"
            " FROM closes c LEFT JOIN close_author_totals t"
            " ON t.close_id=c.close_id AND t.author_id=?"
            " ORDER BY c.close_id",
            (author_id,),
        ).fetchall()
        per_batch = []
        cumulative = 0
        for b in batches:
            cumulative += b["amount_cents"]
            per_batch.append(
                {
                    "close_id": b["close_id"],
                    "created_at": b["created_at"],
                    "batch_net_cents": b["amount_cents"],
                    "accrued_cents": cumulative,
                }
            )
        paid = _paid_cents(conn, author_id)
        latest = _latest_close_id(conn)
        accrued = cumulative  # 截至最新批次的应计额；无批次时为 0
        return {
            "author_id": author_id,
            "latest_close_id": latest,
            "accrued_cents": accrued,
            "paid_cents": paid,
            "negative_balance_cents": max(paid - accrued, 0),
            "payable_cents": max(accrued - paid, 0),
            "batches": per_batch,
            "payments": [
                {
                    "payment_id": r["payment_id"],
                    "close_id": r["close_id"],
                    "amount_cents": r["amount_cents"],
                    "accrued_cents": r["accrued_cents"],
                    "paid_before_cents": r["paid_before_cents"],
                    "payable_cents": r["payable_cents"],
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM payments WHERE author_id=?"
                    " ORDER BY created_at, payment_id",
                    (author_id,),
                )
            ],
        }
