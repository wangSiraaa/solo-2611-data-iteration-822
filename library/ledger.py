"""独立的实物图书预约、取件台账。

与目录的边界：
- 台账保存票据的“原书目 ID”，从不改写它；作品归属在每次挑选时向当前目录查询。
- 台账不定义读者顺序，只记录到达序号；同序号按票据 ID 固定排序。
- 保留一旦建立即锁定当时票据、读者、馆、副本与目录修订；
  后续目录更正对已保留副本不发生改派。

挑选规则（柜台请求一册副本时）：
- 仅在 waiting 票据中挑选；
- 票据原书目经“指定的当前目录”解析到的规范作品，与该副本所属规范作品一致；
- 票据允许的馆集合含副本馆、允许装帧集合含副本装帧；
- 按 (arrival_seq, ticket_id) 取最早者；运营方无法为较新合格票跳过较早者；
- 该副本必须未占用（无 held/fulfilled 保留）且未借出。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db
from .catalog import CatalogService
from .errors import Rejection


class LedgerService:
    def __init__(self, conn: sqlite3.Connection, catalog: CatalogService) -> None:
        self.conn = conn
        self.catalog = catalog

    # ================================================================ 票据

    def register_ticket(
        self,
        ticket_id: str,
        reader_id: str,
        original_bib: str,
        arrival_seq: int,
        allowed_branches: list[str],
        allowed_formats: list[str],
        op_id: str,
    ) -> dict[str, Any]:
        payload = {
            "ticket_id": ticket_id,
            "reader_id": reader_id,
            "original_bib": original_bib,
            "arrival_seq": arrival_seq,
            "allowed_branches": sorted(allowed_branches),
            "allowed_formats": sorted(allowed_formats),
        }
        fp = db.fingerprint("register_ticket", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "register_ticket", fp)
            if prior is not None:
                conn.commit()
                return prior
            if conn.execute(
                "SELECT 1 FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone():
                raise Rejection("ticket_exists", ticket=ticket_id)
            if arrival_seq < 0:
                raise Rejection("bad_arrival_seq", seq=arrival_seq)
            if not allowed_branches or not allowed_formats:
                raise Rejection("empty_allowed_set", ticket=ticket_id)
            conn.execute(
                "INSERT INTO tickets(ticket_id, reader_id, original_bib, "
                "arrival_seq, allowed_branches, allowed_formats, status, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, 'waiting', ?)",
                (
                    ticket_id,
                    reader_id,
                    original_bib,
                    arrival_seq,
                    json.dumps(sorted(allowed_branches)),
                    json.dumps(sorted(allowed_formats)),
                    db.now_iso(),
                ),
            )
            db.bump_wait_version(conn)
            result = {"status": "ok", **payload}
            db.record_operation(conn, op_id, "register_ticket", fp)
            db.add_receipt(conn, "register_ticket", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("register_ticket", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_ticket", ticket=ticket_id)
        return self._ticket_row(row)

    # ================================================================ 保留

    def prepare(
        self, op_id: str, revision: str, copy_id: str
    ) -> dict[str, Any]:
        """柜台准备：按指定目录为该副本选出最早合格等待票据，不产生占用。"""
        payload = {"revision": revision, "copy_id": copy_id}
        fp = db.fingerprint("prepare", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "prepare", fp)
            if prior is not None:
                conn.commit()
                return prior
            copy = self._require_open_copy(revision, copy_id)
            canon = self.catalog.canonical_map(revision)
            picked = self._select_ticket(copy, canon)
            if picked is None:
                raise Rejection(
                    "no_eligible_ticket",
                    revision=revision,
                    copy=copy_id,
                )
            wait_version = int(db.get_meta(conn, "wait_version", "0"))
            conn.execute(
                "INSERT INTO preparations(prep_id, op_id, revision, copy_id, "
                "wait_version, selected, reader_id, branch, state, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)",
                (
                    op_id,
                    op_id,
                    revision,
                    copy_id,
                    wait_version,
                    picked["ticket_id"],
                    picked["reader_id"],
                    copy["branch"],
                    db.now_iso(),
                ),
            )
            result = {
                "status": "ok",
                "prep_id": op_id,
                "revision": revision,
                "copy": copy_id,
                "ticket": picked["ticket_id"],
                "reader": picked["reader_id"],
                "branch": copy["branch"],
                "wait_version": wait_version,
            }
            db.record_operation(conn, op_id, "prepare", fp)
            db.add_receipt(conn, "prepare", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("prepare", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def commit(self, op_id: str, prep_id: str) -> dict[str, Any]:
        """提交准备：目录或等待状态已变则拒绝旧准备；成功即锁定保留。"""
        payload = {"prep_id": prep_id}
        fp = db.fingerprint("commit", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "commit", fp)
            if prior is not None:
                conn.commit()
                return prior
            prep = conn.execute(
                "SELECT * FROM preparations WHERE prep_id=?", (prep_id,)
            ).fetchone()
            if prep is None:
                raise Rejection("unknown_prep", prep=prep_id)
            if prep["state"] != "prepared":
                raise Rejection(
                    "prep_not_open", prep=prep_id, state=prep["state"]
                )
            # 准备后目录更正：头修订变化则旧准备失效
            if self.catalog.head_revision != prep["revision"]:
                self._mark_prep(prep_id, "stale")
                raise Rejection(
                    "stale_prep_catalog_changed",
                    prep=prep_id,
                    prepared_revision=prep["revision"],
                    head_revision=self.catalog.head_revision,
                )
            # 准备后等待状态变化（新票/提交/释放）则旧准备失效
            cur_wait = int(db.get_meta(conn, "wait_version", "0"))
            if cur_wait != prep["wait_version"]:
                self._mark_prep(prep_id, "stale")
                raise Rejection(
                    "stale_prep_wait_changed",
                    prep=prep_id,
                    prepared_version=prep["wait_version"],
                    current_version=cur_wait,
                )
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (prep["selected"],),
            ).fetchone()
            if ticket is None or ticket["status"] != "waiting":
                self._mark_prep(prep_id, "stale")
                raise Rejection(
                    "ticket_no_longer_waiting",
                    prep=prep_id,
                    ticket=prep["selected"],
                )
            # 提交瞬间再次确认副本仍未占用、仍未借出
            self._require_open_copy(prep["revision"], prep["copy_id"])
            # 提交瞬间按当前目录重新确认该票据仍合格（目录未变时结果一致）
            canon = self.catalog.canonical_map(prep["revision"])
            copy = self._copy_snapshot(prep["revision"], prep["copy_id"])
            if not self._ticket_matches(copy, ticket, canon):
                self._mark_prep(prep_id, "stale")
                raise Rejection(
                    "ticket_no_longer_eligible",
                    prep=prep_id,
                    ticket=ticket["ticket_id"],
                )
            hold_id = db.next_hold_id(conn)
            conn.execute(
                "INSERT INTO reservations(hold_id, revision, ticket_id, "
                "reader_id, copy_id, branch, status, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, 'held', ?)",
                (
                    hold_id,
                    prep["revision"],
                    ticket["ticket_id"],
                    ticket["reader_id"],
                    prep["copy_id"],
                    prep["branch"],
                    db.now_iso(),
                ),
            )
            conn.execute(
                "UPDATE tickets SET status='held' WHERE ticket_id=?",
                (ticket["ticket_id"],),
            )
            conn.execute(
                "UPDATE preparations SET state='committed' WHERE prep_id=?",
                (prep_id,),
            )
            db.bump_wait_version(conn)
            result = {
                "status": "ok",
                "hold_id": hold_id,
                "prep_id": prep_id,
                "revision": prep["revision"],
                "ticket": ticket["ticket_id"],
                "reader": ticket["reader_id"],
                "copy": prep["copy_id"],
                "branch": prep["branch"],
            }
            db.record_operation(conn, op_id, "commit", fp)
            db.add_receipt(conn, "commit", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("commit", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def reserve(
        self, op_id: str, revision: str, copy_id: str
    ) -> dict[str, Any]:
        """一步式保留：内部与 prepare+commit 同规则。材料中的 J2/J3 走这条路径。

        只允许针对当前头修订；挑选与锁定规则同两步式完全一致。
        """
        payload = {"revision": revision, "copy_id": copy_id}
        fp = db.fingerprint("reserve", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "reserve", fp)
            if prior is not None:
                conn.commit()
                return prior
            if self.catalog.head_revision != revision:
                raise Rejection(
                    "not_head_revision",
                    revision=revision,
                    head_revision=self.catalog.head_revision,
                )
            copy = self._require_open_copy(revision, copy_id)
            canon = self.catalog.canonical_map(revision)
            picked = self._select_ticket(copy, canon)
            if picked is None:
                raise Rejection(
                    "no_eligible_ticket",
                    revision=revision,
                    copy=copy_id,
                )
            hold_id = db.next_hold_id(conn)
            conn.execute(
                "INSERT INTO reservations(hold_id, revision, ticket_id, "
                "reader_id, copy_id, branch, status, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, 'held', ?)",
                (
                    hold_id,
                    revision,
                    picked["ticket_id"],
                    picked["reader_id"],
                    copy_id,
                    copy["branch"],
                    db.now_iso(),
                ),
            )
            conn.execute(
                "UPDATE tickets SET status='held' WHERE ticket_id=?",
                (picked["ticket_id"],),
            )
            db.bump_wait_version(conn)
            result = {
                "status": "ok",
                "hold_id": hold_id,
                "revision": revision,
                "ticket": picked["ticket_id"],
                "reader": picked["reader_id"],
                "copy": copy_id,
                "branch": copy["branch"],
            }
            db.record_operation(conn, op_id, "reserve", fp)
            db.add_receipt(conn, "reserve", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("reserve", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def pickup(
        self, op_id: str, hold_id: str, reader_id: str, copy_id: str
    ) -> dict[str, Any]:
        """取件只兑现该保留给定的票据、读者与副本，三者不符一律拒绝。"""
        payload = {"hold_id": hold_id, "reader_id": reader_id, "copy_id": copy_id}
        fp = db.fingerprint("pickup", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "pickup", fp)
            if prior is not None:
                conn.commit()
                return prior
            hold = conn.execute(
                "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise Rejection("unknown_hold", hold=hold_id)
            if hold["status"] != "held":
                raise Rejection(
                    "hold_not_open", hold=hold_id, status=hold["status"]
                )
            if reader_id != hold["reader_id"]:
                # 错误读者拒绝，不消耗保留：H3 仍可由真实读者兑现
                raise Rejection(
                    "reader_mismatch",
                    hold=hold_id,
                    expected=hold["reader_id"],
                    given=reader_id,
                )
            if copy_id != hold["copy_id"]:
                raise Rejection(
                    "copy_mismatch",
                    hold=hold_id,
                    expected=hold["copy_id"],
                    given=copy_id,
                )
            ts = db.now_iso()
            conn.execute(
                "UPDATE reservations SET status='fulfilled', fulfilled_at=? "
                "WHERE hold_id=?",
                (ts, hold_id),
            )
            conn.execute(
                "UPDATE tickets SET status='fulfilled' WHERE ticket_id=?",
                (hold["ticket_id"],),
            )
            result = {
                "status": "ok",
                "hold_id": hold_id,
                "ticket": hold["ticket_id"],
                "reader": reader_id,
                "copy": copy_id,
                "branch": hold["branch"],
                "fulfilled_at": ts,
            }
            db.record_operation(conn, op_id, "pickup", fp)
            db.add_receipt(conn, "pickup", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("pickup", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def release(
        self, op_id: str, hold_id: str, reason: str
    ) -> dict[str, Any]:
        """过期/取消：先解除保留（票据回 waiting、副本释放），再由后续柜台请求
        按当前目录重新挑人。旧回执全部保留，不删除、不改写。

        reason 仅接受 expired/cancelled。
        """
        if reason not in ("expired", "cancelled"):
            raise ValueError("reason must be 'expired' or 'cancelled'")
        payload = {"hold_id": hold_id, "reason": reason}
        fp = db.fingerprint("release", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "release", fp)
            if prior is not None:
                conn.commit()
                return prior
            hold = conn.execute(
                "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone()
            if hold is None:
                raise Rejection("unknown_hold", hold=hold_id)
            if hold["status"] != "held":
                raise Rejection(
                    "hold_not_open", hold=hold_id, status=hold["status"]
                )
            ts = db.now_iso()
            conn.execute(
                "UPDATE reservations SET status=?, released_at=?, "
                "release_reason=? WHERE hold_id=?",
                (reason, ts, reason, hold_id),
            )
            # 票据带着原身份与原到达序号回到排队——
            # 实际位置仍只由 (arrival_seq, ticket_id) 决定
            conn.execute(
                "UPDATE tickets SET status='waiting' WHERE ticket_id=?",
                (hold["ticket_id"],),
            )
            db.bump_wait_version(conn)
            result = {
                "status": "ok",
                "hold_id": hold_id,
                "reason": reason,
                "released_ticket": hold["ticket_id"],
                "released_copy": hold["copy_id"],
                "released_at": ts,
            }
            db.record_operation(conn, op_id, "release", fp)
            db.add_receipt(conn, "release", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject("release", op_id, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def seed_hold(
        self,
        hold_id: str,
        revision: str,
        ticket_id: str,
        reader_id: str,
        copy_id: str,
        branch: str,
    ) -> dict[str, Any]:
        """载入合成初态中已存在的保留（材料中的 H3）。

        无操作 ID、无回执，属于系统初始状态而非运营操作；
        同样写 reservations 并把票据置为 held，之后受全部唯一性约束保护。
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute(
                "SELECT 1 FROM reservations WHERE hold_id=?", (hold_id,)
            ).fetchone():
                raise Rejection("hold_exists", hold=hold_id)
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if ticket is None:
                raise Rejection("unknown_ticket", ticket=ticket_id)
            if ticket["reader_id"] != reader_id:
                raise Rejection(
                    "reader_mismatch",
                    expected=ticket["reader_id"],
                    given=reader_id,
                )
            copy = self._copy_snapshot(revision, copy_id)
            if copy["branch"] != branch:
                raise Rejection(
                    "branch_mismatch",
                    expected=copy["branch"],
                    given=branch,
                )
            conn.execute(
                "INSERT INTO reservations(hold_id, revision, ticket_id, "
                "reader_id, copy_id, branch, status, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, 'held', ?)",
                (
                    hold_id,
                    revision,
                    ticket_id,
                    reader_id,
                    copy_id,
                    branch,
                    db.now_iso(),
                ),
            )
            conn.execute(
                "UPDATE tickets SET status='held' WHERE ticket_id=?",
                (ticket_id,),
            )
            # 让后续自动生成的保留 ID 从 H4 起，不与种子 H3 撞号
            existing = (
                int(hold_id[1:])
                if hold_id.startswith("H") and hold_id[1:].isdigit()
                else 0
            )
            counter = int(db.get_meta(conn, "hold_counter", "0"))
            if existing > counter:
                db.set_meta(conn, "hold_counter", str(existing))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "status": "ok",
            "hold_id": hold_id,
            "revision": revision,
            "ticket": ticket_id,
            "reader": reader_id,
            "copy": copy_id,
            "branch": branch,
            "seeded": True,
        }

    # ================================================================ 查询

    def get_queue(self, revision: str | None = None) -> list[dict[str, Any]]:
        """公开查询：当前排队（waiting 票据），按 (到达序号, 票据 ID) 固定排序，
        并现算每张票据在指定（默认当前）目录下的规范作品。"""
        revision = revision or self.catalog.head_revision
        canon = self.catalog.canonical_map(revision)
        rows = self.conn.execute(
            "SELECT * FROM tickets WHERE status='waiting' "
            "ORDER BY arrival_seq, ticket_id"
        ).fetchall()
        out = []
        for row in rows:
            t = self._ticket_row(row)
            t["canonical_bib"] = canon.get(t["original_bib"])
            out.append(t)
        return out

    def get_active_reservations(self) -> list[dict[str, Any]]:
        """当前保留（held）与已兑现（fulfilled）凭据，按建立顺序返回。"""
        rows = self.conn.execute(
            "SELECT * FROM reservations WHERE status IN ('held','fulfilled') "
            "ORDER BY rowid"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_hold(self, hold_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM reservations WHERE hold_id=?", (hold_id,)
        ).fetchone()
        if row is None:
            raise Rejection("unknown_hold", hold=hold_id)
        return dict(row)

    def get_receipts(self) -> list[dict[str, Any]]:
        """成功与拒绝回执全量公开（异文冲突不落回执，直接抛异常）。"""
        rows = self.conn.execute(
            "SELECT receipt_id, op_id, kind, status, result, created_at "
            "FROM receipts ORDER BY receipt_id"
        ).fetchall()
        return [
            {
                "receipt_id": r["receipt_id"],
                "op_id": r["op_id"],
                "kind": r["kind"],
                "status": r["status"],
                "result": json.loads(r["result"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_copy_statuses(self, revision: str | None = None) -> list[dict[str, Any]]:
        """每册副本的占用/借出状态与关联凭据，供重开后核对。"""
        revision = revision or self.catalog.head_revision
        copies = [
            dict(r)
            for r in self.conn.execute(
                "SELECT copy_id, original_bib, branch, format "
                "FROM revision_copies WHERE revision=? ORDER BY copy_id",
                (revision,),
            )
        ]
        out = []
        for copy in copies:
            hold = self.conn.execute(
                "SELECT hold_id, ticket_id, reader_id, status FROM reservations "
                "WHERE copy_id=? AND status IN ('held','fulfilled')",
                (copy["copy_id"],),
            ).fetchone()
            out.append(
                {
                    **copy,
                    "occupied": hold is not None,
                    "hold": dict(hold) if hold is not None else None,
                }
            )
        return out

    # ================================================================ 内部

    def _reject(self, kind: str, op_id: str, fp: str, rej: Rejection) -> dict[str, Any]:
        """回滚后在新事务里登记拒绝回执：操作 ID 以真实指纹消耗，不留半截状态。

        同样的失败请求以后重放，会因指纹命中原样返回这条拒绝回执；
        异文重放仍由 check_idempotent 判为冲突。
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, kind, fp)
            result = {"status": "rejected", "error": rej.as_dict()}
            db.add_receipt(conn, kind, "rejected", result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result

    def _mark_prep(self, prep_id: str, state: str) -> None:
        self.conn.execute(
            "UPDATE preparations SET state=? WHERE prep_id=?", (state, prep_id)
        )

    def _copy_snapshot(self, revision: str, copy_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT copy_id, original_bib, branch, format "
            "FROM revision_copies WHERE revision=? AND copy_id=?",
            (revision, copy_id),
        ).fetchone()
        if row is None:
            raise Rejection("unknown_copy", revision=revision, copy=copy_id)
        return dict(row)

    def _require_open_copy(self, revision: str, copy_id: str) -> dict[str, Any]:
        """副本必须存在于指定修订、且当前无 held/fulfilled 保留（即未占用、未借出）。"""
        if self.catalog.head_revision != revision:
            raise Rejection(
                "not_head_revision",
                revision=revision,
                head_revision=self.catalog.head_revision,
            )
        copy = self._copy_snapshot(revision, copy_id)
        occupied = self.conn.execute(
            "SELECT 1 FROM reservations WHERE copy_id=? "
            "AND status IN ('held','fulfilled')",
            (copy_id,),
        ).fetchone()
        if occupied:
            raise Rejection("copy_not_available", copy=copy_id)
        return copy

    def _select_ticket(
        self, copy: dict[str, Any], canon: dict[str, str]
    ) -> dict[str, Any] | None:
        """按 (arrival_seq, ticket_id) 顺序找第一张合格 waiting 票据。

        只查 waiting：已保留票据由部分唯一索引与状态双重保证不会再次分配。
        """
        work = canon[copy["original_bib"]]
        rows = self.conn.execute(
            "SELECT * FROM tickets WHERE status='waiting' "
            "ORDER BY arrival_seq, ticket_id"
        ).fetchall()
        for row in rows:
            if self._ticket_matches(copy, row, canon, work=work):
                return self._ticket_row(row)
        return None

    def _ticket_matches(
        self,
        copy: dict[str, Any],
        ticket: sqlite3.Row,
        canon: dict[str, str],
        work: str | None = None,
    ) -> bool:
        ticket_work = canon.get(ticket["original_bib"])
        if ticket_work is None:
            return False  # 票据原书目不在该修订目录内，无法判定为同一作品
        if work is not None and ticket_work != work:
            return False
        if ticket_work != canon[copy["original_bib"]]:
            return False
        if copy["branch"] not in json.loads(ticket["allowed_branches"]):
            return False
        if copy["format"] not in json.loads(ticket["allowed_formats"]):
            return False
        return True

    @staticmethod
    def _ticket_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ticket_id": row["ticket_id"],
            "reader_id": row["reader_id"],
            "original_bib": row["original_bib"],
            "arrival_seq": row["arrival_seq"],
            "allowed_branches": json.loads(row["allowed_branches"]),
            "allowed_formats": json.loads(row["allowed_formats"]),
            "status": row["status"],
        }
