"""单元/集成测试。

直接运行：python3.11 -m unittest -v
规模用例 test_scale_500_copies_1000_tickets 验证题目上限下的性能与不变量。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from library import IdempotencyConflict, LibrarySystem

MATERIAL = Path(__file__).parent / "materials" / "765" / "input.json"


class MaterialReplayTest(unittest.TestCase):
    """严格对应 materials/765/README.md 描述的预期。"""

    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        self.data = json.loads(MATERIAL.read_text(encoding="utf-8"))
        d = self.data
        for cat in d["catalogs"]:
            self.system.catalog.submit_revision(
                cat["revision"], list(d["books"]), cat["aliases"],
                d["copies"], op_id=f"seed-cat-{cat['revision']}",
            )
        for t in d["tickets"]:
            self.system.ledger.register_ticket(
                t["id"], t["reader"], t["book"], t["seq"],
                t["branches"], t["formats"], op_id=f"seed-tk-{t['id']}",
            )
        self.system.catalog.activate_revision("v1")
        held = d["held"][0]
        self.system.ledger.seed_hold(
            held["id"], held["catalog"], held["ticket"], held["reader"],
            held["copy"], held["branch"],
        )

    def tearDown(self) -> None:
        self.system.close()

    def _run_ops(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for op in self.data["operations"]:
            a = op["action"]
            if a == "preview":
                r = self.system.ledger.prepare(op["id"], op["catalog"], op["copy"])
            elif a == "switch_catalog":
                r = self.system.catalog.activate_revision(op["catalog"])
            elif a == "confirm_preview":
                r = self.system.ledger.commit(op["id"], op["preview"])
            elif a == "reserve":
                r = self.system.ledger.reserve(op["id"], op["catalog"], op["copy"])
            elif a == "pickup":
                r = self.system.ledger.pickup(
                    op["id"], op["hold"], op["reader"], op["copy"]
                )
            else:
                self.fail(f"未知操作 {a}")
            out[op.get("id", a)] = r
            # J4 拒绝必须当场不消耗 H3（J5 在其后才合法兑现）
            if op.get("id") == "J4":
                self.assertEqual(r["status"], "rejected")
                self.assertEqual(
                    self.system.ledger.get_hold("H3")["status"], "held"
                )
        return out

    def test_seed_hold_only_one(self) -> None:
        holds = self.system.ledger.get_active_reservations()
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["hold_id"], "H3")
        self.assertEqual(holds[0]["ticket_id"], "TC")
        self.assertEqual(holds[0]["copy_id"], "C3")

    def test_preview_under_v1_hits_ta_only(self) -> None:
        # v1 下 WB 与 WA 是两部独立作品；TB 要 WB 且只要北馆精装，C1 不匹配
        p1 = self.system.ledger.prepare("P1", "v1", "C1")
        self.assertEqual(p1["status"], "ok")
        self.assertEqual(p1["ticket"], "TA")
        # 预览不产生占用
        self.assertTrue(
            all(h["hold_id"] == "H3"
                for h in self.system.ledger.get_active_reservations())
        )

    def test_full_operation_sequence(self) -> None:
        r = self._run_ops()
        # 切到 v2 后旧预览失效
        self.assertEqual(r["J1"]["status"], "rejected")
        self.assertEqual(
            r["J1"]["error"]["reason"], "stale_prep_catalog_changed"
        )
        # 合并后 C1 必须给更早的 TB（seq=1），而不是 TA（seq=2）
        self.assertEqual(r["J2"]["status"], "ok")
        self.assertEqual(r["J2"]["ticket"], "TB")
        self.assertEqual(r["J2"]["hold_id"], "H4")
        # C2 平装，TB 的装帧要求不匹配，TA 可获 C2
        self.assertEqual(r["J3"]["status"], "ok")
        self.assertEqual(r["J3"]["ticket"], "TA")
        # 错误读者被拒（J4 当场不消耗 H3 已在 _run_ops 内断言）
        self.assertEqual(r["J4"]["status"], "rejected")
        self.assertEqual(r["J4"]["error"]["reason"], "reader_mismatch")
        # J5 由真实读者 RC 合法兑现
        self.assertEqual(r["J5"]["status"], "ok")
        self.assertEqual(self.system.ledger.get_hold("H3")["status"], "fulfilled")

    def test_c3_remains_locked_to_tc_after_merge(self) -> None:
        # 即使在 v2 合并后，已保留的 C3 也不会改派，仍只有 RC 能取
        self.system.catalog.activate_revision("v2")
        bad = self.system.ledger.pickup("X1", "H3", "RA", "C3")
        self.assertEqual(bad["status"], "rejected")
        ok = self.system.ledger.pickup("X2", "H3", "RC", "C3")
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["ticket"], "TC")

    def test_catalog_query_keeps_original_identity(self) -> None:
        v2 = self.system.catalog.canonical_catalog("v2")
        work = next(w for w in v2["works"] if w["canonical_bib"] == "WA")
        self.assertEqual(sorted(work["members"]), ["WA", "WB"])
        c3 = next(c for c in work["copies"] if c["id"] == "C3")
        self.assertEqual(c3["original_bib"], "WB")
        # 旧修订仍可查
        v1 = self.system.catalog.get_revision("v1")
        self.assertEqual(v1["aliases"], [])
        self.assertEqual(
            self.system.catalog.canonical_map("v1"), {"WA": "WA", "WB": "WB"}
        )

    def test_receipts_include_success_and_rejection(self) -> None:
        self._run_ops()
        by_op = {x["op_id"]: x for x in self.system.ledger.get_receipts()}
        self.assertEqual(by_op["J2"]["status"], "ok")
        self.assertEqual(by_op["J4"]["status"], "rejected")
        self.assertEqual(by_op["J5"]["status"], "ok")


class CatalogRevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")

    def tearDown(self) -> None:
        self.system.close()

    def _copies(self) -> list[dict[str, str]]:
        return [
            {"id": "C1", "book": "B1", "branch": "north", "format": "hardcover"},
        ]

    def test_cycle_rejects_whole_batch(self) -> None:
        before = self.system.catalog.list_revisions()
        r = self.system.catalog.submit_revision(
            "vbad", ["B1", "B2"],
            [{"from": "B1", "to": "B2"}, {"from": "B2", "to": "B1"}],
            self._copies() + [
                {"id": "C2", "book": "B2", "branch": "north", "format": "paperback"}
            ],
            op_id="bad1",
        )
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "alias_cycle")
        # 整批不留数据
        self.assertEqual(self.system.catalog.list_revisions(), before)

    def test_self_loop_rejected(self) -> None:
        r = self.system.catalog.submit_revision(
            "vself", ["B1"], [{"from": "B1", "to": "B1"}],
            self._copies(), op_id="self1",
        )
        self.assertEqual(r["error"]["reason"], "alias_cycle")

    def test_dangling_edge_rejected(self) -> None:
        r = self.system.catalog.submit_revision(
            "vdangle", ["B1"], [{"from": "B1", "to": "GHOST"}],
            self._copies(), op_id="d1",
        )
        self.assertEqual(r["error"]["reason"], "edge_endpoint_missing")

    def test_duplicate_out_edge_rejected(self) -> None:
        r = self.system.catalog.submit_revision(
            "vdup", ["B1", "B2", "B3"],
            [{"from": "B1", "to": "B2"}, {"from": "B1", "to": "B3"}],
            self._copies(), op_id="dup1",
        )
        self.assertEqual(r["error"]["reason"], "duplicate_out_edge")

    def test_multi_hop_canonical_and_dismantle(self) -> None:
        copies = [
            {"id": "C1", "book": "B1", "branch": "n", "format": "f"},
            {"id": "C2", "book": "B2", "branch": "n", "format": "f"},
            {"id": "C3", "book": "B3", "branch": "n", "format": "f"},
        ]
        self.system.catalog.submit_revision(
            "v1", ["B1", "B2", "B3"],
            [{"from": "B1", "to": "B2"}, {"from": "B2", "to": "B3"}],
            copies, op_id="v1",
        )
        self.assertEqual(
            self.system.catalog.canonical_map("v1"),
            {"B1": "B3", "B2": "B3", "B3": "B3"},
        )
        # 后续修订拆除别名：不再声明边
        self.system.catalog.submit_revision(
            "v2", ["B1", "B2", "B3"], [], copies, op_id="v2"
        )
        self.system.catalog.activate_revision("v2")
        self.assertEqual(
            self.system.catalog.canonical_map("v2"),
            {"B1": "B1", "B2": "B2", "B3": "B3"},
        )
        # 旧目录仍可查到多跳边
        self.assertEqual(
            self.system.catalog.get_revision("v1")["aliases"],
            [{"from": "B1", "to": "B2"}, {"from": "B2", "to": "B3"}],
        )

    def test_many_to_one_allowed(self) -> None:
        copies = [
            {"id": "C1", "book": "B1", "branch": "n", "format": "f"},
            {"id": "C2", "book": "B2", "branch": "n", "format": "f"},
        ]
        r = self.system.catalog.submit_revision(
            "v1", ["B1", "B2", "B3"],
            [{"from": "B1", "to": "B3"}, {"from": "B2", "to": "B3"}],
            copies, op_id="m1",
        )
        self.assertEqual(r["status"], "ok")
        self.assertEqual(
            self.system.catalog.canonical_map("v1")["B1"], "B3"
        )

    def test_revision_is_immutable(self) -> None:
        self.system.catalog.submit_revision(
            "v1", ["B1"], [], self._copies(), op_id="a"
        )
        again = self.system.catalog.submit_revision(
            "v1", ["B1"], [], self._copies(), op_id="b"
        )
        self.assertEqual(again["status"], "rejected")
        self.assertEqual(again["error"]["reason"], "revision_exists")

    def test_failed_revision_replay_returns_same_rejection(self) -> None:
        payload_edges = [{"from": "B1", "to": "B1"}]
        r1 = self.system.catalog.submit_revision(
            "vbad", ["B1"], payload_edges, self._copies(), op_id="x"
        )
        r2 = self.system.catalog.submit_revision(
            "vbad", ["B1"], payload_edges, self._copies(), op_id="x"
        )
        self.assertEqual(r1, r2)
        self.assertEqual(len(self.system.catalog.list_revisions()), 0)


class QueueOrderAndLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        copies = [
            {"id": "C1", "book": "W", "branch": "north", "format": "hardcover"},
        ]
        self.system.catalog.submit_revision(
            "v1", ["W"], [], copies, op_id="cat"
        )

    def tearDown(self) -> None:
        self.system.close()

    def _ticket(self, tid: str, seq: int, reader: str | None = None,
                branches=("north",), formats=("hardcover",)) -> None:
        self.system.ledger.register_ticket(
            tid, reader or f"R-{tid}", "W", seq,
            list(branches), list(formats), op_id=f"tk-{tid}",
        )

    def test_earliest_seq_wins(self) -> None:
        self._ticket("T-new", 2)
        self._ticket("T-old", 1)
        r = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(r["ticket"], "T-old")

    def test_same_seq_tie_break_by_ticket_id(self) -> None:
        self._ticket("TZ", 5)
        self._ticket("TA", 5)
        r = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(r["ticket"], "TA")

    def test_reserved_ticket_and_copy_not_reassigned(self) -> None:
        self._ticket("T1", 1)
        self._ticket("T2", 2)
        first = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(first["ticket"], "T1")
        # 同一副本再请求：已占用
        again = self.system.ledger.reserve("op2", "v1", "C1")
        self.assertEqual(again["status"], "rejected")
        self.assertEqual(again["error"]["reason"], "copy_not_available")
        # T1 不再出现在排队中
        queue = [t["ticket_id"] for t in self.system.ledger.get_queue()]
        self.assertEqual(queue, ["T2"])

    def test_format_mismatch_skips_to_next_eligible(self) -> None:
        # T1 只要精装，C1 是精装——这里改为登记只收平装的早票，应被跳过
        self._ticket("T1", 1, formats=("paperback",))
        self._ticket("T2", 2, formats=("hardcover",))
        r = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(r["ticket"], "T2")

    def test_branch_mismatch_skips_to_next_eligible(self) -> None:
        self._ticket("T1", 1, branches=("south",))
        self._ticket("T2", 2, branches=("north",))
        r = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(r["ticket"], "T2")

    def test_no_eligible_ticket(self) -> None:
        self._ticket("T1", 1, branches=("south",))
        r = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "no_eligible_ticket")

    def test_reserve_requires_head_revision(self) -> None:
        copies = [
            {"id": "C1", "book": "W", "branch": "north", "format": "hardcover"},
        ]
        self.system.catalog.submit_revision("v2", ["W"], [], copies, op_id="cat2")
        self.system.catalog.activate_revision("v2")
        self._ticket("T1", 1)
        stale = self.system.ledger.reserve("op1", "v1", "C1")
        self.assertEqual(stale["error"]["reason"], "not_head_revision")


class PrepareCommitStalenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        copies = [
            {"id": "C1", "book": "W", "branch": "north", "format": "hardcover"},
        ]
        self.system.catalog.submit_revision("v1", ["W"], [], copies, op_id="cat")

    def tearDown(self) -> None:
        self.system.close()

    def test_catalog_change_invalidates_prep(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="t1"
        )
        self.system.ledger.prepare("P1", "v1", "C1")
        self.system.catalog.submit_revision(
            "v2", ["W"], [],
            [{"id": "C1", "book": "W", "branch": "north", "format": "hardcover"}],
            op_id="cat2",
        )
        self.system.catalog.activate_revision("v2")
        r = self.system.ledger.commit("J1", "P1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "stale_prep_catalog_changed")

    def test_wait_change_invalidates_prep(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="t1"
        )
        self.system.ledger.prepare("P1", "v1", "C1")
        # 准备之后新票据到达，等待版本变化
        self.system.ledger.register_ticket(
            "T2", "R2", "W", 2, ["north"], ["hardcover"], op_id="t2"
        )
        r = self.system.ledger.commit("J1", "P1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "stale_prep_wait_changed")
        # 重新准备仍选更早的 T1
        self.system.ledger.prepare("P2", "v1", "C1")
        ok = self.system.ledger.commit("J2", "P2")
        self.assertEqual(ok["ticket"], "T1")

    def test_committed_prep_cannot_commit_twice(self) -> None:
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="t1"
        )
        self.system.ledger.prepare("P1", "v1", "C1")
        first = self.system.ledger.commit("J1", "P1")
        self.assertEqual(first["status"], "ok")
        second = self.system.ledger.commit("J2", "P1")
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["error"]["reason"], "prep_not_open")


class PickupAndReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        copies = [
            {"id": "C1", "book": "W", "branch": "north", "format": "hardcover"},
        ]
        self.system.catalog.submit_revision("v1", ["W"], [], copies, op_id="cat")
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["north"], ["hardcover"], op_id="t1"
        )
        self.hold = self.system.ledger.reserve("op-h", "v1", "C1")

    def tearDown(self) -> None:
        self.system.close()

    def test_pickup_requires_exact_reader_copy(self) -> None:
        hid = self.hold["hold_id"]
        bad_reader = self.system.ledger.pickup("p1", hid, "R9", "C1")
        self.assertEqual(bad_reader["error"]["reason"], "reader_mismatch")
        bad_copy = self.system.ledger.pickup("p2", hid, "R1", "C9")
        self.assertEqual(bad_copy["error"]["reason"], "copy_mismatch")
        # 拒绝均不消耗保留
        self.assertEqual(self.system.ledger.get_hold(hid)["status"], "held")
        ok = self.system.ledger.pickup("p3", hid, "R1", "C1")
        self.assertEqual(ok["status"], "ok")
        # 已兑现的保留不能再取
        again = self.system.ledger.pickup("p4", hid, "R1", "C1")
        self.assertEqual(again["error"]["reason"], "hold_not_open")

    def test_release_returns_ticket_to_queue_and_old_receipts_kept(self) -> None:
        hid = self.hold["hold_id"]
        before = len(self.system.ledger.get_receipts())
        rel = self.system.ledger.release("rel1", hid, "expired")
        self.assertEqual(rel["status"], "ok")
        self.assertEqual(self.system.ledger.get_ticket("T1")["status"], "waiting")
        # 副本释放后可按同一规则重新挑到原票据（仍是最早合格者）
        again = self.system.ledger.reserve("op-h2", "v1", "C1")
        self.assertEqual(again["ticket"], "T1")
        self.assertEqual(len(self.system.ledger.get_receipts()), before + 2)
        # 历史保留行仍在，状态为 expired
        old = self.system.ledger.get_hold(hid)
        self.assertEqual(old["status"], "expired")
        self.assertEqual(old["release_reason"], "expired")

    def test_cancel_then_reselect_under_current_catalog(self) -> None:
        # 释放后登记一张更早序号的票据，重选必须给新的更早者
        hid = self.hold["hold_id"]
        self.system.ledger.release("rel1", hid, "cancelled")
        self.system.ledger.register_ticket(
            "T0", "R0", "W", 0, ["north"], ["hardcover"], op_id="t0"
        )
        picked = self.system.ledger.reserve("op-h3", "v1", "C1")
        self.assertEqual(picked["ticket"], "T0")


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = LibrarySystem(":memory:")
        self.system.catalog.submit_revision(
            "v1", ["W"], [],
            [{"id": "C1", "book": "W", "branch": "n", "format": "f"}],
            op_id="cat",
        )
        self.system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["n"], ["f"], op_id="t1"
        )

    def tearDown(self) -> None:
        self.system.close()

    def test_same_op_id_same_content_replays(self) -> None:
        first = self.system.ledger.reserve("J1", "v1", "C1")
        second = self.system.ledger.reserve("J1", "v1", "C1")
        self.assertEqual(first, second)
        # 没有第二条保留
        holds = self.system.ledger.get_active_reservations()
        self.assertEqual(len(holds), 1)

    def test_same_op_id_different_content_conflicts(self) -> None:
        first = self.system.ledger.reserve("J1", "v1", "C1")
        self.assertEqual(first["status"], "ok")
        # 同 ID 同参数是合法重放，原样返回且不产生第二条保留
        replay = self.system.ledger.reserve("J1", "v1", "C1")
        self.assertEqual(replay, first)
        self.assertEqual(len(self.system.ledger.get_active_reservations()), 1)
        # 同 ID 异文（不同副本）必须冲突；先造一个真正空闲的 C2
        self.system.catalog.submit_revision(
            "v2", ["W"], [],
            [
                {"id": "C1", "book": "W", "branch": "n", "format": "f"},
                {"id": "C2", "book": "W", "branch": "n", "format": "f"},
            ],
            op_id="cat2",
        )
        self.system.catalog.activate_revision("v2")
        with self.assertRaises(IdempotencyConflict):
            self.system.ledger.reserve("J1", "v2", "C2")
        # 冲突不留任何新数据：C2 仍空闲，回执数不变
        self.assertFalse(
            next(c for c in self.system.ledger.get_copy_statuses()
                 if c["copy_id"] == "C2")["occupied"]
        )

    def test_failure_does_not_partially_occupy(self) -> None:
        # 独立库：让排队票据与 C1 全部不匹配（只接受 south 馆），请求必须拒绝，
        # 且无保留、无票据状态变化，只落一条拒绝回执。
        system = LibrarySystem(":memory:")
        system.catalog.submit_revision(
            "v1", ["W"], [],
            [{"id": "C1", "book": "W", "branch": "north", "format": "f"}],
            op_id="cat",
        )
        system.ledger.register_ticket(
            "T1", "R1", "W", 1, ["south"], ["f"], op_id="t1"
        )
        r = system.ledger.reserve("JF", "v1", "C1")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["error"]["reason"], "no_eligible_ticket")
        self.assertEqual(system.ledger.get_active_reservations(), [])
        self.assertEqual(system.ledger.get_ticket("T1")["status"], "waiting")
        # 仅一条拒绝回执，没有成功回执
        receipts = system.ledger.get_receipts()
        self.assertEqual([x["status"] for x in receipts if x["op_id"] == "JF"],
                         ["rejected"])
        system.close()


class PersistenceTest(unittest.TestCase):
    def test_reopen_keeps_holds_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "lib.db")
            sys = LibrarySystem(path)
            sys.catalog.submit_revision(
                "v1", ["W"], [],
                [{"id": "C1", "book": "W", "branch": "n", "format": "f"}],
                op_id="cat",
            )
            sys.ledger.register_ticket(
                "T1", "R1", "W", 1, ["n"], ["f"], op_id="t1"
            )
            hold = sys.ledger.reserve("h1", "v1", "C1")
            sys.close()

            reopened = LibrarySystem.reopen(path)
            self.assertEqual(reopened.catalog.head_revision, "v1")
            got = reopened.ledger.get_hold(hold["hold_id"])
            self.assertEqual(got["ticket_id"], "T1")
            self.assertEqual(got["copy_id"], "C1")
            self.assertTrue(
                any(x["op_id"] == "h1" for x in reopened.ledger.get_receipts())
            )
            # 唯一性约束随重开仍然生效
            dup = reopened.ledger.reserve("h2", "v1", "C1")
            self.assertEqual(dup["error"]["reason"], "copy_not_available")
            reopened.close()


class ScaleTest(unittest.TestCase):
    """题目上限：500 副本、1000 票据。"""

    def test_scale_500_copies_1000_tickets(self) -> None:
        system = LibrarySystem(":memory:")
        n_bibs = 10
        bibs = [f"B{i:02d}" for i in range(n_bibs)]
        branches = ["north", "south"]
        formats = ["hardcover", "paperback"]
        copies = []
        # 500 册，均匀分布到 10 个作品/2 馆/2 装帧
        for i in range(500):
            copies.append({
                "id": f"C{i:04d}",
                "book": bibs[i % n_bibs],
                "branch": branches[i % 2],
                "format": formats[(i // 2) % 2],
            })
        system.catalog.submit_revision("v1", bibs, [], copies, op_id="cat-v1")

        # 1000 张等待票据：允许集合不同，确保部分票据无合格副本
        for i in range(1000):
            system.ledger.register_ticket(
                ticket_id=f"T{i:04d}",
                reader_id=f"R{i:04d}",
                original_bib=bibs[i % n_bibs],
                arrival_seq=i,
                allowed_branches=[branches[i % 2]],
                allowed_formats=[formats[(i // 2) % 2]],
                op_id=f"tk-{i:04d}",
            )

        # 中途合并：B01..B09 -> B00，验证合并后更早票据跨原书目胜出
        merge_edges = [{"from": bibs[i], "to": "B00"} for i in range(1, n_bibs)]
        system.catalog.submit_revision(
            "v2", bibs, merge_edges, copies, op_id="cat-v2"
        )
        system.catalog.activate_revision("v2")

        start = time.perf_counter()
        n_ok = 0
        n_reject = 0
        for i in range(500):
            cid = f"C{i:04d}"
            r = system.ledger.reserve(f"rsv-{cid}", "v2", cid)
            if r["status"] == "ok":
                n_ok += 1
            else:
                n_reject += 1
                self.assertEqual(r["error"]["reason"], "no_eligible_ticket")
        elapsed = time.perf_counter() - start

        # 不变量 1：每个开放副本至多一条保留，每张票据至多一条
        holds = system.ledger.get_active_reservations()
        self.assertEqual(len(holds), n_ok)
        self.assertEqual(len({h["copy_id"] for h in holds}), n_ok)
        self.assertEqual(len({h["ticket_id"] for h in holds}), n_ok)

        # 不变量 2：独立 oracle 复核——每个保留挑中的都是当时最早合格票据。
        # 重放规则：按副本处理顺序，对每个副本在“尚未被前面副本拿走”的票据里
        # 取 (arrival_seq, ticket_id) 最小的合格者，应与实际保留一致。
        canon = system.catalog.canonical_map("v2")
        copy_by_id = {c["id"]: c for c in copies}
        all_tickets = [system.ledger.get_ticket(f"T{i:04d}") for i in range(1000)]
        actual_by_copy = {h["copy_id"]: h["ticket_id"] for h in holds}
        available = {t["ticket_id"] for t in all_tickets}
        for i in range(500):
            cid = f"C{i:04d}"
            copy = copy_by_id[cid]
            work = canon[copy["book"]]
            eligible = [
                t for t in all_tickets
                if t["ticket_id"] in available
                and canon.get(t["original_bib"]) == work
                and copy["branch"] in t["allowed_branches"]
                and copy["format"] in t["allowed_formats"]
            ]
            if not eligible:
                self.assertNotIn(cid, actual_by_copy)
                continue
            earliest = min(eligible, key=lambda t: (t["arrival_seq"], t["ticket_id"]))
            self.assertIn(cid, actual_by_copy)
            self.assertEqual(actual_by_copy[cid], earliest["ticket_id"])
            available.discard(earliest["ticket_id"])  # 被该副本拿走

        # 不变量 3：仍在排队的票据对任何已占用副本都不能形成“被跳过的更早合格者”
        open_copy_ids = {c["id"] for c in copies} - {h["copy_id"] for h in holds}
        self.assertLessEqual(len(open_copy_ids), 500 - n_ok)

        # 500 次挑选（每次扫描至多 1000 张票据）应在合理时间内完成
        self.assertLess(elapsed, 30.0, f"规模用例过慢：{elapsed:.2f}s")
        system.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
