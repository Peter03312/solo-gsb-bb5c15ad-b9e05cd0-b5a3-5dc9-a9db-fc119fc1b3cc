"""SQLite 连接管理与 schema 初始化。

设计要点：
- 单连接 + 全局写锁：SQLite 本身单写者，配合进程内 RLock，
  所有入账/关账写操作串行化，提交顺序即数据库提交顺序。
- isolation_level=None（自动提交），写操作显式使用 BEGIN IMMEDIATE ... COMMIT，
  保证"检查 + 写入"在同一事务中原子完成。
"""
from __future__ import annotations

import os
import sqlite3
import threading

LOCK = threading.RLock()

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    work_id     TEXT PRIMARY KEY,
    title       TEXT,
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_tiers (
    work_id   TEXT NOT NULL REFERENCES works(work_id),
    idx       INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    rate      TEXT NOT NULL,
    PRIMARY KEY (work_id, idx)
);
-- 作者分成版本：创建作品时的初始分成为版本 1（change_id 为 NULL）；
-- 之后每次变更追加一个版本，change_id 全局唯一，版本号按提交顺序递增
CREATE TABLE IF NOT EXISTS work_versions (
    work_id     TEXT NOT NULL REFERENCES works(work_id),
    version     INTEGER NOT NULL,
    change_id   TEXT UNIQUE,
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (work_id, version)
);
CREATE TABLE IF NOT EXISTS work_version_authors (
    work_id   TEXT NOT NULL,
    version   INTEGER NOT NULL,
    author_id TEXT NOT NULL,
    ratio     TEXT NOT NULL,
    PRIMARY KEY (work_id, version, author_id),
    FOREIGN KEY (work_id, version) REFERENCES work_versions(work_id, version)
);

-- 全局入账序号：销售、退款、净价更正、价差反向统一排队，决定关账批次归属
CREATE TABLE IF NOT EXISTS entries (
    entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL CHECK (kind IN ('sale', 'refund', 'price_correction', 'price_delta_reversal')),
    ref_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    sale_id             TEXT PRIMARY KEY,
    work_id             TEXT NOT NULL REFERENCES works(work_id),
    business_time       TEXT NOT NULL,
    copies              INTEGER NOT NULL CHECK (copies > 0),
    net_price           TEXT NOT NULL,
    entry_seq           INTEGER NOT NULL UNIQUE REFERENCES entries(entry_seq),
    cum_before          INTEGER NOT NULL,
    cum_after           INTEGER NOT NULL,
    split_version       INTEGER NOT NULL DEFAULT 1,  -- 入账时使用的作者分成版本
    total_royalty_cents INTEGER NOT NULL,
    close_id            INTEGER REFERENCES closes(close_id),
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_work ON sales(work_id);

CREATE TABLE IF NOT EXISTS sale_segments (
    sale_id         TEXT NOT NULL REFERENCES sales(sale_id),
    idx             INTEGER NOT NULL,
    tier_idx        INTEGER NOT NULL,
    rate            TEXT NOT NULL,
    copies          INTEGER NOT NULL,
    per_copy_cents  INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    refunded_copies INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sale_id, idx)
);
CREATE TABLE IF NOT EXISTS sale_splits (
    sale_id      TEXT NOT NULL REFERENCES sales(sale_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (sale_id, author_id)
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id          TEXT PRIMARY KEY,
    sale_id            TEXT NOT NULL REFERENCES sales(sale_id),
    copies             INTEGER NOT NULL CHECK (copies > 0),
    entry_seq          INTEGER NOT NULL UNIQUE REFERENCES entries(entry_seq),
    total_refund_cents INTEGER NOT NULL,
    close_id           INTEGER REFERENCES closes(close_id),
    fingerprint        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refunds_sale ON refunds(sale_id);

CREATE TABLE IF NOT EXISTS refund_segments (
    refund_id        TEXT NOT NULL REFERENCES refunds(refund_id),
    idx              INTEGER NOT NULL,
    sale_segment_idx INTEGER NOT NULL,
    copies           INTEGER NOT NULL,
    per_copy_cents   INTEGER NOT NULL,
    amount_cents     INTEGER NOT NULL,
    PRIMARY KEY (refund_id, idx)
);
CREATE TABLE IF NOT EXISTS refund_splits (
    refund_id    TEXT NOT NULL REFERENCES refunds(refund_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (refund_id, author_id)
);

-- 关账批次与不可改报表（报表内容在关账时快照落库，之后只读）
CREATE TABLE IF NOT EXISTS closes (
    close_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT UNIQUE,
    from_entry_seq INTEGER NOT NULL,
    last_entry_seq INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS close_lines (
    close_id  INTEGER NOT NULL REFERENCES closes(close_id),
    entry_seq INTEGER NOT NULL,
    line_type TEXT NOT NULL,
    ref_id    TEXT NOT NULL,
    payload   TEXT NOT NULL,
    PRIMARY KEY (close_id, entry_seq)
);
CREATE TABLE IF NOT EXISTS close_author_totals (
    close_id     INTEGER NOT NULL REFERENCES closes(close_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (close_id, author_id)
);

-- 作者付款结算：仅可针对当前最新关账批次提交；
-- accrued/paid_before/payable 为提交时刻的计算快照，幂等重放时原样返回，不随后续关账变化
CREATE TABLE IF NOT EXISTS payments (
    payment_id        TEXT PRIMARY KEY,
    author_id         TEXT NOT NULL,
    close_id          INTEGER NOT NULL REFERENCES closes(close_id),
    amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
    accrued_cents     INTEGER NOT NULL,
    paid_before_cents INTEGER NOT NULL,
    payable_cents     INTEGER NOT NULL,
    fingerprint       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_author ON payments(author_id);

-- 净价更正：以独立流水补提/冲回各档未退册数的新旧净价版税差额，
-- 不改写历史销售；old_net_price 为该销售的"当前更正价"（首次即原价）
CREATE TABLE IF NOT EXISTS price_corrections (
    correction_id       TEXT PRIMARY KEY,
    sale_id             TEXT NOT NULL REFERENCES sales(sale_id),
    entry_seq           INTEGER NOT NULL UNIQUE REFERENCES entries(entry_seq),
    old_net_price       TEXT NOT NULL,
    new_net_price       TEXT NOT NULL,
    split_version       INTEGER NOT NULL,  -- 原销售入账时的作者分成版本
    total_delta_cents   INTEGER NOT NULL,  -- 各档差额合计（可为负）
    close_id            INTEGER REFERENCES closes(close_id),
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_corrections_sale ON price_corrections(sale_id);
CREATE TABLE IF NOT EXISTS price_correction_segments (
    correction_id       TEXT NOT NULL REFERENCES price_corrections(correction_id),
    idx                 INTEGER NOT NULL,
    sale_segment_idx    INTEGER NOT NULL,
    tier_idx            INTEGER NOT NULL,
    rate                TEXT NOT NULL,
    unrefunded_copies   INTEGER NOT NULL,  -- 更正时该档未退册数
    old_per_copy_cents  INTEGER NOT NULL,
    new_per_copy_cents  INTEGER NOT NULL,
    delta_per_copy_cents INTEGER NOT NULL,  -- 新 - 旧（可为负）
    amount_cents        INTEGER NOT NULL,  -- 每册差额 × 未退册数（可为负）
    PRIMARY KEY (correction_id, idx)
);
CREATE TABLE IF NOT EXISTS price_correction_splits (
    correction_id TEXT NOT NULL REFERENCES price_corrections(correction_id),
    author_id     TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,  -- 新价分账 - 旧价分账（可为负）
    PRIMARY KEY (correction_id, author_id)
);

-- 退款附带的"已生效价差反向流水"：把被退册数此前已生效的价差冲回，
-- 与退款同一事务（各占一个入账序号），随退款归入同一关账批次；
-- 旧退款不追溯，无已生效价差的退款不产生反向流水（无此表行）。
-- amount_cents 为带符号反向金额：减价差为正、补价差为负，关账时按负贡献入账。
CREATE TABLE IF NOT EXISTS price_delta_reversals (
    refund_id           TEXT PRIMARY KEY REFERENCES refunds(refund_id),
    sale_id             TEXT NOT NULL REFERENCES sales(sale_id),
    entry_seq           INTEGER NOT NULL UNIQUE REFERENCES entries(entry_seq),
    split_version       INTEGER NOT NULL,
    total_reversal_cents INTEGER NOT NULL,
    close_id            INTEGER REFERENCES closes(close_id),
    created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_delta_reversal_segments (
    refund_id           TEXT NOT NULL REFERENCES price_delta_reversals(refund_id),
    idx                 INTEGER NOT NULL,
    sale_segment_idx    INTEGER NOT NULL,
    tier_idx            INTEGER NOT NULL,
    rate                TEXT NOT NULL,
    refunded_copies     INTEGER NOT NULL,  -- 本次退款在该档退掉的册数
    current_per_copy_cents INTEGER NOT NULL,  -- 退款时"当前更正价"下的每册版税
    original_per_copy_cents INTEGER NOT NULL, -- 原销售原价每册版税
    delta_per_copy_cents INTEGER NOT NULL,    -- 当前 - 原始（可为负）
    amount_cents        INTEGER NOT NULL,     -- 每册有效价差 × 被退册数（可为负）
    PRIMARY KEY (refund_id, idx)
);
CREATE TABLE IF NOT EXISTS price_delta_reversal_splits (
    refund_id    TEXT NOT NULL REFERENCES price_delta_reversals(refund_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,  -- 带符号反向金额（可为负）
    PRIMARY KEY (refund_id, author_id)
);
"""


def open_database(path: str | None = None) -> sqlite3.Connection:
    """打开（并切换）数据库连接。path 缺省取环境变量 DATABASE_PATH。"""
    global _conn
    with LOCK:
        if _conn is not None:
            _conn.close()
            _conn = None
        db_path = path or os.environ.get("DATABASE_PATH", "royalties.db")
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # 自动提交；写操作显式 BEGIN IMMEDIATE
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _conn = conn
        return conn


def get_conn() -> sqlite3.Connection:
    with LOCK:
        if _conn is None:
            open_database()
        return _conn


def init_db() -> None:
    """初始化数据库 schema（幂等，容器启动时自动执行）。"""
    with LOCK:
        conn = get_conn()
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库迁移（幂等）。

    - 为 sales 补 split_version 列；把旧 work_authors 表迁为版本 1；
    - entries 的 CHECK 约束加入 net 价更正与价差反向两种新流水类型
      （SQLite 不能直接改 CHECK，按官方推荐方式重建表；在 init_db 持锁、
      且新表均以 IF NOT EXISTS 创建的前提下，外键引用安全）。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sales)")}
    if "split_version" not in cols:
        # 版本机制之前的销售均按初始分成计提，回填版本 1
        conn.execute(
            "ALTER TABLE sales ADD COLUMN split_version INTEGER NOT NULL DEFAULT 1"
        )
    # entries CHECK 约束迁移：旧约束只允许 sale/refund，重建为含新流水类型。
    # 不用 RENAME（SQLite 会连带改写 sales/refunds 等表的外键引用），而是：
    # 建 entries_new → 拷贝 → DROP entries → RENAME entries_new AS entries，
    # 引用方始终按名字 "entries" 解析，重建后自然指向新表。
    entries_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
    ).fetchone()
    if entries_sql is not None and "price_correction" not in (entries_sql["sql"] or ""):
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute(
                "CREATE TABLE entries_new ("
                " entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " kind TEXT NOT NULL CHECK (kind IN"
                " ('sale','refund','price_correction','price_delta_reversal')),"
                " ref_id TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO entries_new(entry_seq, kind, ref_id)"
                " SELECT entry_seq, kind, ref_id FROM entries"
            )
            conn.execute("DROP TABLE entries")
            conn.execute("ALTER TABLE entries_new RENAME TO entries")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    has_legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_authors'"
    ).fetchone()
    if has_legacy:
        works = conn.execute(
            "SELECT w.work_id, w.fingerprint, w.created_at FROM works w"
            " WHERE NOT EXISTS(SELECT 1 FROM work_versions v WHERE v.work_id=w.work_id)"
        ).fetchall()
        for w in works:
            conn.execute(
                "INSERT INTO work_versions(work_id, version, change_id, fingerprint,"
                " created_at) VALUES(?,1,NULL,?,?)",
                (w["work_id"], w["fingerprint"], w["created_at"]),
            )
            conn.execute(
                "INSERT INTO work_version_authors(work_id, version, author_id, ratio)"
                " SELECT work_id, 1, author_id, ratio FROM work_authors WHERE work_id=?",
                (w["work_id"],),
            )
