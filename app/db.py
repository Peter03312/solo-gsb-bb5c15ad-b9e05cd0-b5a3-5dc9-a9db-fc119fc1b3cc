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

-- 全局入账序号：销售、退款、净价更正、退款价差反向流水统一排队，
-- 决定累计册数与关账批次归属（提交顺序即数据库提交顺序）
CREATE TABLE IF NOT EXISTS entries (
    entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL CHECK (kind IN
                  ('sale', 'refund', 'correction', 'price_diff_reversal')),
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

-- 净价更正：以全局唯一更正编号提交，只计算更正时点各档未退册数的新旧净价差额。
-- 更正以独立流水进入下一关账批次，不改写历史销售/退款/已关账报表/付款；
-- 同一销售可多次更正，每笔相对上一生效净价（首笔相对原净价）。
CREATE TABLE IF NOT EXISTS corrections (
    correction_id       TEXT PRIMARY KEY,
    sale_id             TEXT NOT NULL REFERENCES sales(sale_id),
    old_net_price       TEXT NOT NULL,  -- 更正前生效净价（元）：上一笔更正的新价或原净价
    new_net_price       TEXT NOT NULL,  -- 非负新每册净价（元）
    entry_seq           INTEGER NOT NULL UNIQUE REFERENCES entries(entry_seq),
    split_version       INTEGER NOT NULL,  -- 原销售入账时的分成版本（差额据此分账）
    old_total_cents     INTEGER NOT NULL,  -- 旧净价下未退册数的应计总额（分）
    new_total_cents     INTEGER NOT NULL,  -- 新净价下未退册数的应计总额（分）
    total_diff_cents    INTEGER NOT NULL,  -- 各档差额合计（正为补提、负为冲减）
    close_id            INTEGER REFERENCES closes(close_id),
    fingerprint         TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_sale ON corrections(sale_id);

CREATE TABLE IF NOT EXISTS correction_segments (
    correction_id    TEXT NOT NULL REFERENCES corrections(correction_id),
    idx              INTEGER NOT NULL,
    sale_segment_idx INTEGER NOT NULL,
    tier_idx         INTEGER NOT NULL,
    rate             TEXT NOT NULL,
    copies           INTEGER NOT NULL,  -- 更正时点该档未退册数
    old_per_copy_cents INTEGER NOT NULL,
    new_per_copy_cents INTEGER NOT NULL,
    diff_cents       INTEGER NOT NULL,
    PRIMARY KEY (correction_id, idx)
);
CREATE TABLE IF NOT EXISTS correction_splits (
    correction_id TEXT NOT NULL REFERENCES corrections(correction_id),
    author_id     TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    PRIMARY KEY (correction_id, author_id)
);
-- 价差池：每笔更正把逐作者价差分摊到各销售分档；退款按档回拨，
-- 某档退净时该档池为 0，全退后该销售逐作者累计价差严格归零
CREATE TABLE IF NOT EXISTS correction_segment_pools (
    sale_id      TEXT NOT NULL REFERENCES sales(sale_id),
    segment_idx  INTEGER NOT NULL,
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,  -- 已生效未反向的价差（带符号）
    PRIMARY KEY (sale_id, segment_idx, author_id)
);

-- 退款价差反向流水：对退款册数另记已生效价差的反向流水，
-- 与退款同事务、入账序号相邻，随退款归入同一关账批次。
-- 仅当原销售曾有生效更正时产生；金额按价差池回拨，旧退款不追溯。
CREATE TABLE IF NOT EXISTS refund_diff_reversals (
    refund_id        TEXT PRIMARY KEY REFERENCES refunds(refund_id),
    sale_id          TEXT NOT NULL REFERENCES sales(sale_id),
    entry_seq        INTEGER NOT NULL UNIQUE REFERENCES entries(entry_seq),
    total_diff_cents INTEGER NOT NULL,  -- 带符号：与已生效价差相反
    close_id         INTEGER REFERENCES closes(close_id),
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refund_diff_reversal_segments (
    refund_id        TEXT NOT NULL REFERENCES refund_diff_reversals(refund_id),
    idx              INTEGER NOT NULL,
    sale_segment_idx INTEGER NOT NULL,
    copies           INTEGER NOT NULL,
    diff_cents       INTEGER NOT NULL,
    PRIMARY KEY (refund_id, idx)
);
CREATE TABLE IF NOT EXISTS refund_diff_reversal_splits (
    refund_id    TEXT NOT NULL REFERENCES refund_diff_reversals(refund_id),
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
    - 旧库 entries 的 CHECK 仅允许 ('sale','refund')，重建表放开
      'correction' 与 'price_diff_reversal' 两类净价更正相关流水。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sales)")}
    if "split_version" not in cols:
        # 版本机制之前的销售均按初始分成计提，回填版本 1
        conn.execute(
            "ALTER TABLE sales ADD COLUMN split_version INTEGER NOT NULL DEFAULT 1"
        )
    # 净价更正在旧库为新表（CREATE IF NOT EXISTS 自动建），仅需为极少数
    # 开发期中间态库补列；正常旧库无 corrections 表，直接跳过。
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corrections'"
    ).fetchone():
        ccols = {r["name"] for r in conn.execute("PRAGMA table_info(corrections)")}
        if "old_total_cents" not in ccols:
            conn.execute(
                "ALTER TABLE corrections ADD COLUMN old_total_cents INTEGER NOT NULL DEFAULT 0"
            )
        if "new_total_cents" not in ccols:
            conn.execute(
                "ALTER TABLE corrections ADD COLUMN new_total_cents INTEGER NOT NULL DEFAULT 0"
            )
    # 旧库 entries 约束不含新流水类型：按 SQLite 官方步骤重建表（保留原序号）。
    # 重建父表须先关闭外键强制（sales/refunds 均以 entry_seq 引用 entries）。
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
    ).fetchone()["sql"]
    if "price_diff_reversal" not in ddl:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            CREATE TABLE entries_new (
                entry_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind      TEXT NOT NULL CHECK (kind IN
                              ('sale', 'refund', 'correction', 'price_diff_reversal')),
                ref_id    TEXT NOT NULL
            );
            INSERT INTO entries_new(entry_seq, kind, ref_id)
                SELECT entry_seq, kind, ref_id FROM entries;
            DROP TABLE entries;
            ALTER TABLE entries_new RENAME TO entries;
            """
        )
        conn.execute("PRAGMA foreign_keys=ON")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:  # 迁移后存在悬挂引用则报错，不静默吞掉
            raise RuntimeError(f"entries 迁移后外键检查失败: {bad!r}")
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
