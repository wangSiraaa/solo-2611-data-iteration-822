"""SQLite 连接、schema、元数据与幂等基础设施。

设计要点：
- 目录修订是全量快照，写入即不可变；alias_edges 是单跳、单向边，
  多跳规范书目的解析在读时进行（见 catalog.CatalogService.canonical_map）。
- 副本身份（copy_id）跨修订稳定；每修订单独保存其原始书目 ID、取件馆、装帧，
  后续修订拆别名也不会揉掉原始身份。
- 台账与目录物理上分表，台账不定义读者顺序，只保存票据上声明的到达序号；
  保留表用部分唯一索引保证“一票据/一副本同时至多一条未释放保留”。
- 所有写操作带操作 ID，operations 表保存请求指纹，重放/异文在事务开头识别。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 单行元数据：当前头修订与全局计数器
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 目录修订（不可变快照）
CREATE TABLE IF NOT EXISTS revisions (
    revision   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 修订内声明的全部书目（稳定书目 ID）
CREATE TABLE IF NOT EXISTS revision_bibs (
    revision TEXT NOT NULL,
    bib_id   TEXT NOT NULL,
    PRIMARY KEY (revision, bib_id),
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);

-- 单向别名边：单跳，from -> to；同一修订内每个 from 只允许一条出边
CREATE TABLE IF NOT EXISTS alias_edges (
    revision TEXT NOT NULL,
    from_bib TEXT NOT NULL,
    to_bib   TEXT NOT NULL,
    PRIMARY KEY (revision, from_bib),
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);

-- 各副本在该修订下的原始身份与属性（同一副本跨修订保留同一 copy_id）
CREATE TABLE IF NOT EXISTS revision_copies (
    revision      TEXT NOT NULL,
    copy_id       TEXT NOT NULL,
    original_bib  TEXT NOT NULL,
    branch        TEXT NOT NULL,
    format        TEXT NOT NULL,
    PRIMARY KEY (revision, copy_id),
    FOREIGN KEY (revision) REFERENCES revisions(revision)
);

-- 等待票据：原书目身份、到达序号、允许的馆/装帧集合（JSON 数组）
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        TEXT PRIMARY KEY,
    reader_id        TEXT NOT NULL,
    original_bib     TEXT NOT NULL,
    arrival_seq      INTEGER NOT NULL,
    allowed_branches TEXT NOT NULL,
    allowed_formats  TEXT NOT NULL,
    status           TEXT NOT NULL
        CHECK (status IN ('waiting', 'held', 'fulfilled', 'cancelled')),
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_waiting
    ON tickets(arrival_seq, ticket_id) WHERE status = 'waiting';

-- 保留（含已兑现的历史保留，永不物理删除）。
-- 锁定当时的票据、读者、馆、副本与目录修订；之后目录更正不影响已保留副本。
CREATE TABLE IF NOT EXISTS reservations (
    hold_id           TEXT PRIMARY KEY,
    revision          TEXT NOT NULL,
    ticket_id         TEXT NOT NULL,
    reader_id         TEXT NOT NULL,
    copy_id           TEXT NOT NULL,
    branch            TEXT NOT NULL,
    status            TEXT NOT NULL
        CHECK (status IN ('held', 'fulfilled', 'expired', 'cancelled')),
    created_at        TEXT NOT NULL,
    fulfilled_at      TEXT,
    released_at       TEXT,
    release_reason    TEXT
);
-- 未释放（held/fulfilled）的保留对票据、副本分别唯一
CREATE UNIQUE INDEX IF NOT EXISTS idx_resv_open_ticket
    ON reservations(ticket_id) WHERE status IN ('held', 'fulfilled');
CREATE UNIQUE INDEX IF NOT EXISTS idx_resv_open_copy
    ON reservations(copy_id) WHERE status IN ('held', 'fulfilled');

-- 柜台准备（两步式 prepare/commit），过期准备保留为记录可审计
CREATE TABLE IF NOT EXISTS preparations (
    prep_id        TEXT PRIMARY KEY,
    op_id          TEXT,
    revision       TEXT NOT NULL,
    copy_id        TEXT NOT NULL,
    wait_version   INTEGER NOT NULL,
    selected       TEXT,
    reader_id      TEXT,
    branch         TEXT,
    state          TEXT NOT NULL
        CHECK (state IN ('prepared', 'committed', 'rejected', 'stale')),
    created_at     TEXT NOT NULL
);

-- 操作幂等记录：操作 ID 唯一，保存请求指纹
CREATE TABLE IF NOT EXISTS operations (
    op_id      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 全部回执（成功与拒绝都留痕；准备也产生回执）
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id       TEXT,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'rejected', 'conflict')),
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_op ON receipts(op_id);
"""


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def connect(db_path: str) -> sqlite3.Connection:
    """打开（或新建）数据库并确保 schema 就位。

    db_path=":memory:" 时每次调用是独立内存库，测试中由系统对象持有同一连接。
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # isolation_level=None：事务完全由服务层显式 BEGIN/COMMIT 控制，
    # 避免驱动隐式事务与 BEGIN IMMEDIATE 叠加
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('head_revision', '')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('wait_version', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('hold_counter', '0')"
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 时间 / 指纹 / 元数据
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def fingerprint(kind: str, payload: dict[str, Any]) -> str:
    """请求内容指纹：同一操作 ID 同内容可重放，异文拒绝。

    sort_keys 保证字典键序不影响指纹；分隔符固定，避免字段拼接歧义。
    """
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{kind}|{body}".encode("utf-8")).hexdigest()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def bump_wait_version(conn: sqlite3.Connection) -> int:
    """等待状态变化（新票据/提交/释放）时自增，使旧准备整体失效。"""
    nxt = int(get_meta(conn, "wait_version", "0")) + 1
    set_meta(conn, "wait_version", str(nxt))
    return nxt


def next_hold_id(conn: sqlite3.Connection) -> str:
    nxt = int(get_meta(conn, "hold_counter", "0")) + 1
    set_meta(conn, "hold_counter", str(nxt))
    return f"H{nxt}"


# ---------------------------------------------------------------------------
# 幂等与回执
# ---------------------------------------------------------------------------

def check_idempotent(
    conn: sqlite3.Connection,
    op_id: str,
    kind: str,
    fp: str,
) -> dict | None:
    """事务开头调用。

    - 操作 ID 不存在：返回 None，继续正常处理（调用方随后登记操作）。
    - 同 ID 同指纹：返回上次回执 result（重放，调用方原样返回，不再写库）。
    - 同 ID 异指纹：抛 IdempotencyConflict，事务回滚，不留任何占位。
    """
    row = conn.execute(
        "SELECT fingerprint FROM operations WHERE op_id=?", (op_id,)
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fp:
        from .errors import IdempotencyConflict

        raise IdempotencyConflict(op_id, kind)
    receipt = conn.execute(
        "SELECT result FROM receipts WHERE op_id=? ORDER BY receipt_id LIMIT 1",
        (op_id,),
    ).fetchone()
    return json.loads(receipt["result"]) if receipt is not None else {}


def record_operation(
    conn: sqlite3.Connection, op_id: str, kind: str, fp: str
) -> None:
    conn.execute(
        "INSERT INTO operations(op_id, kind, fingerprint, created_at) "
        "VALUES(?, ?, ?, ?)",
        (op_id, kind, fp, now_iso()),
    )


def add_receipt(
    conn: sqlite3.Connection,
    kind: str,
    status: str,
    result: dict[str, Any],
    op_id: str | None = None,
) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO receipts(op_id, kind, status, result, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (op_id, kind, status, json.dumps(result, ensure_ascii=False), now_iso()),
    )
    return result
