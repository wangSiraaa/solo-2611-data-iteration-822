"""检索排名可证差值子系统的单元测试。

直接运行：python3 -m unittest test_relevancy -v
"""

from __future__ import annotations

import unittest

from library.relevancy import (
    IncompatibleEvidence,
    PartitionCatalog,
    QueryJudgments,
    RelevancyError,
    analyze_diff,
    build_constraints,
    prepare_ranking,
)


class EvidenceModelTest(unittest.TestCase):
    def test_interval_bounds(self):
        QueryJudgments().add_interval("i", "b", 0, 3)
        with self.assertRaises(RelevancyError):
            QueryJudgments().add_interval("i", "b", 2, 1)
        with self.assertRaises(RelevancyError):
            QueryJudgments().add_interval("i", "b", 0, 4)

    def test_strict_self_relation_rejected_at_source(self):
        with self.assertRaises(RelevancyError):
            QueryJudgments().add_relation("r", "b", "b", ">")
        # 非严格自指平凡成立，允许录入
        j = QueryJudgments().add_relation("r", "b", "b", "=")
        cat = PartitionCatalog(["b"])
        self.assertTrue(build_constraints(j, cat).is_feasible())


class CatalogTest(unittest.TestCase):
    def test_merge_and_undo_keeps_original_identity(self):
        cat = PartitionCatalog(["a", "b", "c"], "r0")
        cat.merge_alias("b", "a", "r1")
        cat.merge_alias("c", "a", "r2")
        self.assertEqual(cat.works(), {"a": ["a", "b", "c"]})
        # 旧修订仍可完整查询
        self.assertEqual(cat.works("r1"), {"a": ["a", "b"], "c": ["c"]})
        cat.undo_last()
        self.assertEqual(cat.revision, "r1")
        self.assertEqual(cat.works("r1"), {"a": ["a", "b"], "c": ["c"]})
        cat.undo_last()
        self.assertEqual(cat.works(), {"a": ["a"], "b": ["b"], "c": ["c"]})
        with self.assertRaises(RelevancyError):
            cat.undo_last()

    def test_self_merge_rejected(self):
        cat = PartitionCatalog(["a", "b"])
        cat.merge_alias("b", "a", "r1")
        with self.assertRaises(RelevancyError):
            cat.merge_alias("a", "b", "r2")

    def test_mapping_must_cover_all_bibs(self):
        with self.assertRaises(RelevancyError):
            PartitionCatalog.from_mapping(["a", "b"], {"a": "a"})


class ConflictTest(unittest.TestCase):
    def test_interval_conflict(self):
        cat = PartitionCatalog(["a", "b"])
        cat.merge_alias("b", "a", "r1")
        j = QueryJudgments().add_interval("i1", "a", 0, 1) \
                            .add_interval("i2", "b", 2, 2)
        with self.assertRaises(IncompatibleEvidence) as ctx:
            build_constraints(j, cat)
        self.assertEqual(ctx.exception.kind, "interval_conflict")
        self.assertEqual(set(ctx.exception.evidence_ids), {"i1", "i2"})

    def test_strict_self_relation_via_merge(self):
        cat = PartitionCatalog(["a", "b"])
        j = QueryJudgments().add_relation("r1", "a", "b", "<")
        self.assertTrue(build_constraints(j, cat).is_feasible())
        cat.merge_alias("b", "a", "r1")
        with self.assertRaises(IncompatibleEvidence) as ctx:
            build_constraints(j, cat)
        self.assertEqual(ctx.exception.kind, "strict_self_relation")
        self.assertEqual(ctx.exception.evidence_ids, ["r1"])

    def test_joint_unsatisfiable_minimal_core(self):
        cat = PartitionCatalog(["p", "q"])
        j = (QueryJudgments()
             .add_interval("ip", "p", 2, 3)
             .add_interval("iq", "q", 0, 1)
             .add_relation("r", "p", "q", "<"))
        with self.assertRaises(IncompatibleEvidence) as ctx:
            build_constraints(j, cat)
        err = ctx.exception
        self.assertEqual(err.kind, "joint_unsatisfiable")
        self.assertEqual(set(err.evidence_ids), {"ip", "iq", "r"})
        # 极小核心：去掉任一证据都应变得可解
        for drop in ("ip", "iq", "r"):
            j2 = QueryJudgments(
                intervals=[e for e in j.intervals
                           if e.evidence_id != drop],
                relations=[e for e in j.relations
                           if e.evidence_id != drop])
            self.assertTrue(build_constraints(j2, cat).is_feasible())

    def test_merge_resolves_after_undo(self):
        # 合并时冲突，撤销后同一批证据恢复为可解
        cat = PartitionCatalog(["a", "b"])
        j = QueryJudgments().add_interval("i1", "a", 0, 1) \
                            .add_interval("i2", "b", 2, 3)
        cat.merge_alias("b", "a", "r1")
        with self.assertRaises(IncompatibleEvidence):
            build_constraints(j, cat)
        cat.undo_last()
        cs = build_constraints(j, cat)
        self.assertEqual(cs.works, ["a", "b"])
        self.assertTrue(cs.is_feasible())


class RankingPreparationTest(unittest.TestCase):
    def setUp(self):
        self.cat = PartitionCatalog(["a", "b", "c"])
        self.cat.merge_alias("b", "a", "r1")  # 作品 a、c

    def test_first_occurrence_kept_and_compacted(self):
        prep = prepare_ranking("A", ["b", "c", "a", "c"], self.cat,
                               weights=[5, 3, 1], cut=2)
        self.assertEqual([r["work"] for r in prep.rows], ["a", "c"])
        self.assertEqual(prep.rows[0]["bib"], "b")          # 保留首次出现
        self.assertEqual(prep.rows[0]["original_position"], 1)
        self.assertEqual(prep.work_position, {"a": 1, "c": 2})
        self.assertEqual([d["bib"] for d in prep.dropped_repeats],
                         ["a", "c"])
        self.assertEqual(prep.weights, (5, 3))

    def test_cut_uses_min_of_cut_and_weights(self):
        prep = prepare_ranking("A", ["a", "c"], self.cat,
                               weights=[5], cut=3)
        self.assertEqual(prep.cut, 1)
        self.assertEqual(len(prep.rows), 1)

    def test_negative_weight_rejected(self):
        with self.assertRaises(RelevancyError):
            prepare_ranking("A", ["a"], self.cat, weights=[-1])

    def test_unknown_hit_rejected(self):
        with self.assertRaises(RelevancyError):
            prepare_ranking("A", ["zzz"], self.cat, weights=[1])


class DiffAnalysisTest(unittest.TestCase):
    def test_prompt_example_must_win(self):
        cat = PartitionCatalog(["X", "Y"])
        j = QueryJudgments().add_interval("ix", "X", 2, 2) \
                            .add_interval("iy", "Y", 0, 1)
        r = analyze_diff(j, cat, ["X", "Y"], ["Y", "X"],
                         weights=[2, 1], cut=2)
        self.assertEqual((r.min_diff, r.max_diff), (1, 2))
        self.assertTrue(r.outcomes["A_win"])
        self.assertFalse(r.outcomes["tie"] or r.outcomes["B_win"])
        self.assertEqual(r.min_witness.assignment, {"X": 2, "Y": 1})
        self.assertEqual(r.max_witness.assignment, {"X": 2, "Y": 0})

    def test_prompt_example_must_tie(self):
        cat = PartitionCatalog(["X", "Y"])
        j = QueryJudgments().add_interval("ix", "X", 2, 2) \
                            .add_interval("iy", "Y", 0, 1)
        r = analyze_diff(j, cat, ["X", "Y"], ["Y", "X"],
                         weights=[1, 1], cut=2)
        self.assertEqual(r.min_diff, 0)
        self.assertEqual(r.max_diff, 0)
        self.assertTrue(r.outcomes["tie"])
        self.assertEqual(r.feasible_count, 2)

    def test_prompt_example_tie_or_loss(self):
        cat = PartitionCatalog(["X", "Y"])
        j = QueryJudgments().add_interval("ix", "X", 2, 2) \
                            .add_interval("iy", "Y", 2, 3)
        r = analyze_diff(j, cat, ["X", "Y"], ["Y", "X"],
                         weights=[2, 1], cut=2)
        self.assertEqual((r.min_diff, r.max_diff), (-1, 0))
        self.assertEqual(r.outcomes,
                         {"A_win": False, "tie": True, "B_win": True})

    def test_shared_assignment_not_two_independent_labelings(self):
        # x=y 关系把两个排名绑定：若误当独立标注会得到错误区间
        cat = PartitionCatalog(["x", "y"])
        j = QueryJudgments().add_relation("req", "x", "y", "=")
        r = analyze_diff(j, cat, ["x", "y"], ["y", "x"],
                         weights=[2, 1], cut=2)
        # diff = 2x+y - (2y+x) = x-y，而 x=y 恒成立 => 恒 0
        self.assertEqual((r.min_diff, r.max_diff), (0, 0))
        self.assertTrue(r.outcomes["tie"])
        self.assertEqual(r.feasible_count, 4)

    def test_relation_predicates_all_supported(self):
        cat = PartitionCatalog(["a", "b"])
        for rel in ("=", "!=", "<", ">", "<=", ">="):
            j = QueryJudgments().add_relation("r", "a", "b", rel)
            r = analyze_diff(j, cat, ["a"], ["b"],
                             weights=[1, 1], cut_a=1, cut_b=1)
            diffs = {r.min_diff, r.max_diff}
            if rel == "=":
                self.assertEqual(diffs, {0})
            elif rel == "!=":
                self.assertEqual(diffs, {-3, 3})
                self.assertFalse(r.outcomes["tie"])
            elif rel == "<":
                self.assertEqual(r.max_diff, -1)
            elif rel == ">":
                self.assertEqual(r.min_diff, 1)
            elif rel == "<=":
                self.assertEqual(r.max_diff, 0)
                self.assertTrue(r.outcomes["tie"])
            else:  # >=
                self.assertEqual(r.min_diff, 0)
                self.assertTrue(r.outcomes["tie"])

    def test_cross_zero_without_tie_relation_version(self):
        cat = PartitionCatalog(["x", "y", "z"])
        j = QueryJudgments().add_relation("rne", "x", "y", "!=")
        r = analyze_diff(j, cat, ["x", "z"], ["y", "z"],
                         weights=[2, 1], cut=2)
        self.assertTrue(r.crosses_zero)
        self.assertFalse(r.outcomes["tie"])
        self.assertTrue(r.outcomes["A_win"] and r.outcomes["B_win"])
        self.assertEqual(r.tie_assignments, [])
        self.assertEqual((r.min_diff, r.max_diff), (-6, 6))

    def test_cross_zero_without_tie_interval_version(self):
        # 纯区间、无关系也能跨零无平局：填充作品 z 固定为 0 占位次，
        # 差量化为 4p - q（同一份非负权重、同一截断长度）
        cat = PartitionCatalog(["p", "q", "z"])
        j = (QueryJudgments()
             .add_interval("ip", "p", 0, 1)
             .add_interval("iq", "q", 2, 3)
             .add_interval("iz", "z", 0, 0))
        r = analyze_diff(j, cat, ["p", "z"], ["z", "q"],
                         weights=[4, 1], cut=2)
        self.assertEqual(r.coefficients, {"p": 4, "q": -1, "z": -3})
        # 4p-q 取值 {-3,-2,+1,+2}，恰好跳过 0
        self.assertEqual((r.min_diff, r.max_diff), (-3, 2))
        self.assertTrue(r.crosses_zero and not r.outcomes["tie"])

    def test_non_ranked_work_still_in_full_assignment(self):
        cat = PartitionCatalog(["a", "b", "extra"])
        j = QueryJudgments().add_interval("ie", "extra", 3, 3)
        r = analyze_diff(j, cat, ["a"], ["b"], weights=[2, 1],
                         cut_a=1, cut_b=1)
        self.assertEqual(set(r.works), {"a", "b", "extra"})
        self.assertEqual(r.min_witness.assignment["extra"], 3)
        self.assertEqual(r.max_witness.assignment["extra"], 3)
        # extra 不上榜，系数为 0，不影响边界
        self.assertEqual(r.coefficients["extra"], 0)

    def test_zero_weight_positions_are_neutral(self):
        cat = PartitionCatalog(["x", "y"])
        j = QueryJudgments().add_interval("ix", "x", 1, 3)
        # 唯一保留位权重为 0：两版系数全为 0，差值恒 0
        r = analyze_diff(j, cat, ["x", "y"], ["y", "x"],
                         weights=[0], cut=1)
        self.assertEqual(r.min_diff, 0)
        self.assertEqual(r.max_diff, 0)
        self.assertEqual(set(r.coefficients.values()), {0})

    def test_eight_works_enumeration_budget(self):
        bibs = [f"w{i}" for i in range(8)]
        cat = PartitionCatalog(bibs)
        r = analyze_diff(QueryJudgments(), cat, bibs[:4], bibs[4:],
                         weights=[1, 1, 1, 1], cut=4)
        self.assertEqual(r.feasible_count, 4 ** 8)      # 65536 组完整赋值

    def test_ninth_work_rejected(self):
        cat = PartitionCatalog([f"w{i}" for i in range(9)])
        with self.assertRaises(RelevancyError):
            build_constraints(QueryJudgments(), cat)

    def test_evidence_objects_survive_revision_changes(self):
        cat = PartitionCatalog(["a", "b"])
        j = QueryJudgments().add_interval("i1", "a", 1, 2) \
                            .add_interval("i2", "b", 2, 3)
        cat.merge_alias("b", "a", "r1")
        merged = build_constraints(j, cat)
        self.assertEqual(merged.interval_sources, {"a": ["i1", "i2"]})
        cat.undo_last()
        split = build_constraints(j, cat)
        self.assertEqual(split.interval_sources,
                         {"a": ["i1"], "b": ["i2"]})
        # 原始证据未被修改
        self.assertEqual(j.evidence_by_id("i1").bib, "a")
        self.assertEqual(j.evidence_by_id("i2").low, 2)


if __name__ == "__main__":
    unittest.main()
