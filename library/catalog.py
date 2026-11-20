"""带修订的书目别名目录。

不变量（每次 submit_revision 在一个事务内整体校验，任一不过则整批拒绝）：
1. 修订是全量快照：给出本修订全部书目、全部单向别名边、全部副本的原始身份。
2. 边的两个终点必须都在本修订书目中；每本书目至多一条出边（多对一、多跳允许）。
3. 边集合不得成环（含自环）；有环则整批修订拒绝，库内不留半截。
4. 副本原始书目必须存在；同一 copy_id 跨修订稳定，属性可被后续修订更正。
5. 修订一经写入不可变；错误别名只能由后续修订“不再声明该边”来拆除。
目录里不出现读者、票据或任何顺序概念。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .errors import Rejection


class CatalogService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 写入

    def submit_revision(
        self,
        revision: str,
        bibs: list[str],
        aliases: list[dict[str, str]],
        copies: list[dict[str, str]],
        op_id: str,
    ) -> dict[str, Any]:
        """提交一个目录修订（全量快照）。失败落 rejected 回执，不部分占位。"""
        payload = {
            "revision": revision,
            "bibs": sorted(bibs),
            "aliases": sorted(aliases, key=lambda e: (e["from"], e["to"])),
            "copies": sorted(copies, key=lambda c: c["id"]),
        }
        fp = db.fingerprint("submit_revision", payload)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = db.check_idempotent(conn, op_id, "submit_revision", fp)
            if prior is not None:
                conn.commit()
                return prior
            self._validate(revision, bibs, aliases, copies)
            ts = db.now_iso()
            conn.execute(
                "INSERT INTO revisions(revision, created_at) VALUES(?, ?)",
                (revision, ts),
            )
            conn.executemany(
                "INSERT INTO revision_bibs(revision, bib_id) VALUES(?, ?)",
                [(revision, b) for b in bibs],
            )
            conn.executemany(
                "INSERT INTO alias_edges(revision, from_bib, to_bib) "
                "VALUES(?, ?, ?)",
                [(revision, e["from"], e["to"]) for e in aliases],
            )
            conn.executemany(
                "INSERT INTO revision_copies"
                "(revision, copy_id, original_bib, branch, format) "
                "VALUES(?, ?, ?, ?, ?)",
                [
                    (revision, c["id"], c["book"], c["branch"], c["format"])
                    for c in copies
                ],
            )
            # 首个修订自动成为当前目录；之后由 activate_revision 显式切换
            if not db.get_meta(conn, "head_revision"):
                db.set_meta(conn, "head_revision", revision)
            db.record_operation(conn, op_id, "submit_revision", fp)
            result = {
                "status": "ok",
                "revision": revision,
                "bibs": list(bibs),
                "canonical": self.canonical_map(revision),
                "copies": list(copies),
            }
            db.add_receipt(conn, "submit_revision", "ok", result, op_id)
            conn.commit()
            return result
        except Rejection as rej:
            conn.rollback()
            return self._reject_revision(op_id, revision, fp, rej)
        except Exception:
            conn.rollback()
            raise

    def _reject_revision(
        self,
        op_id: str,
        revision: str,
        fp: str,
        rej: Rejection,
    ) -> dict[str, Any]:
        """拒绝路径用独立事务落回执：操作 ID 以真实指纹消耗，不留部分目录数据。

        同样的失败请求以后重放，会因指纹命中而原样返回这条拒绝回执；
        异文重放仍由 check_idempotent 判为冲突。
        """
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.record_operation(conn, op_id, "submit_revision", fp)
            result = {
                "status": "rejected",
                "revision": revision,
                "error": rej.as_dict(),
            }
            db.add_receipt(conn, "submit_revision", "rejected", result, op_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result

    def _validate(
        self,
        revision: str,
        bibs: list[str],
        aliases: list[dict[str, str]],
        copies: list[dict[str, str]],
    ) -> None:
        conn = self.conn
        if conn.execute(
            "SELECT 1 FROM revisions WHERE revision=?", (revision,)
        ).fetchone():
            raise Rejection("revision_exists", revision=revision)
        if not bibs:
            raise Rejection("empty_revision", revision=revision)
        if len(set(bibs)) != len(bibs):
            raise Rejection("duplicate_bib_in_revision", revision=revision)
        bib_set = set(bibs)

        edge_map: dict[str, str] = {}
        for edge in aliases:
            src, dst = edge["from"], edge["to"]
            if src not in bib_set or dst not in bib_set:
                raise Rejection(
                    "edge_endpoint_missing", edge=edge, revision=revision
                )
            if src in edge_map:
                raise Rejection(
                    "duplicate_out_edge", bib=src, revision=revision
                )
            edge_map[src] = dst
        self._assert_acyclic(edge_map, revision)

        seen_copies: set[str] = set()
        for copy in copies:
            cid = copy["id"]
            if cid in seen_copies:
                raise Rejection("duplicate_copy_in_revision", copy=cid)
            seen_copies.add(cid)
            if copy["book"] not in bib_set:
                raise Rejection(
                    "copy_original_bib_missing",
                    copy=cid,
                    original_bib=copy["book"],
                )
            if not copy.get("branch") or not copy.get("format"):
                raise Rejection("copy_attribute_missing", copy=cid)

    @staticmethod
    def _assert_acyclic(edge_map: dict[str, str], revision: str) -> None:
        """迭代三色 DFS；自环与任意环都拒绝整批。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {b: WHITE for b in edge_map}
        for start in edge_map:
            if color.get(start, BLACK) != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            while stack:
                node, idx = stack[-1]
                color[node] = GRAY
                nxt = edge_map.get(node)
                if idx == 0:
                    if nxt is None:
                        color[node] = BLACK
                        stack.pop()
                        continue
                    if color.get(nxt, BLACK) == GRAY:
                        raise Rejection(
                            "alias_cycle", revision=revision, at=node
                        )
                    if color.get(nxt, BLACK) == WHITE:
                        stack[-1] = (node, 1)
                        stack.append((nxt, 0))
                        continue
                    color[node] = BLACK
                    stack.pop()
                else:
                    color[node] = BLACK
                    stack.pop()

    def activate_revision(self, revision: str) -> dict[str, Any]:
        """切换台账读取的当前目录（无操作 ID 的内部运维动作，同样校验存在性）。"""
        conn = self.conn
        if not conn.execute(
            "SELECT 1 FROM revisions WHERE revision=?", (revision,)
        ).fetchone():
            raise Rejection("unknown_revision", revision=revision)
        conn.execute("BEGIN IMMEDIATE")
        try:
            db.set_meta(conn, "head_revision", revision)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "ok", "head_revision": revision}

    # ------------------------------------------------------------------ 读取

    @property
    def head_revision(self) -> str:
        return db.get_meta(self.conn, "head_revision")

    def list_revisions(self) -> list[str]:
        return [
            r["revision"]
            for r in self.conn.execute(
                "SELECT revision FROM revisions ORDER BY created_at, revision"
            )
        ]

    def get_revision(self, revision: str) -> dict[str, Any]:
        """任一旧目录仍可查：返回完整快照且显式保留原始身份。"""
        conn = self.conn
        if not conn.execute(
            "SELECT 1 FROM revisions WHERE revision=?", (revision,)
        ).fetchone():
            raise Rejection("unknown_revision", revision=revision)
        bibs = [
            r["bib_id"]
            for r in conn.execute(
                "SELECT bib_id FROM revision_bibs WHERE revision=? ORDER BY bib_id",
                (revision,),
            )
        ]
        edges = [
            {"from": r["from_bib"], "to": r["to_bib"]}
            for r in conn.execute(
                "SELECT from_bib, to_bib FROM alias_edges "
                "WHERE revision=? ORDER BY from_bib",
                (revision,),
            )
        ]
        copies = [
            {
                "id": r["copy_id"],
                "book": r["original_bib"],
                "branch": r["branch"],
                "format": r["format"],
            }
            for r in conn.execute(
                "SELECT copy_id, original_bib, branch, format "
                "FROM revision_copies WHERE revision=? ORDER BY copy_id",
                (revision,),
            )
        ]
        return {
            "revision": revision,
            "bibs": bibs,
            "aliases": edges,
            "copies": copies,
            "canonical": self.canonical_map(revision),
        }

    def canonical_map(self, revision: str) -> dict[str, str]:
        """读时现算：每个书目沿单向边多跳到终点（规范书目）。

        写入时已保证无环，这里用带 visited 的路径解析兜底，
        理论上不会遇到环；遇到即数据损坏，直接报错而不是静默截断。
        """
        conn = self.conn
        edge_map = {
            r["from_bib"]: r["to_bib"]
            for r in conn.execute(
                "SELECT from_bib, to_bib FROM alias_edges WHERE revision=?",
                (revision,),
            )
        }
        bibs = [
            r["bib_id"]
            for r in conn.execute(
                "SELECT bib_id FROM revision_bibs WHERE revision=?", (revision,)
            )
        ]
        result: dict[str, str] = {}
        for bib in bibs:
            path: list[str] = []
            cur = bib
            while cur in edge_map:
                if cur in path:  # 防御：写入侧已拒绝环
                    raise Rejection(
                        "alias_cycle", revision=revision, at=cur
                    )
                path.append(cur)
                cur = edge_map[cur]
            result[bib] = cur
        return result

    def canonical_catalog(self, revision: str | None = None) -> dict[str, Any]:
        """公开查询：当前（或指定）规范目录，并保留成员原 ID 与每副本原始身份。"""
        revision = revision or self.head_revision
        if not revision:
            raise Rejection("no_catalog")
        snap = self.get_revision(revision)
        canonical = snap["canonical"]
        works: dict[str, dict[str, Any]] = {}
        for member, canon in canonical.items():
            works.setdefault(
                canon, {"canonical_bib": canon, "members": [], "copies": []}
            )
            works[canon]["members"].append(member)
        for copy in snap["copies"]:
            canon = canonical[copy["book"]]
            works[canon]["copies"].append(
                {
                    "id": copy["id"],
                    "original_bib": copy["book"],  # 原始身份不揉掉
                    "branch": copy["branch"],
                    "format": copy["format"],
                }
            )
        return {
            "revision": revision,
            "works": sorted(works.values(), key=lambda w: w["canonical_bib"]),
        }
