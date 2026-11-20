"""两版检索排名可证差值的端到端复评演示（纯标准库）。

运行：python3 demo_relevancy.py
全部断言通过且退出码为 0，即说明下列场景的预期全部兑现：
  S0 题目自带三算例（必胜 / 必平 / 可平可负）
  S1 同一作品重复命中 + 别名合并 + 撤销后原始证据各自恢复
  S2 三类互不相容证据的标出（严格自指 / 区间冲突 / 联合无解）
  S3 差值跨零却无法平局（关系版与纯区间版各一例）
  S4 与 SQLite 版 CatalogService 修订快照对接
"""

from __future__ import annotations

from library.relevancy import (
    IncompatibleEvidence,
    PartitionCatalog,
    QueryJudgments,
    RELATION_TEXT,
    analyze_diff,
    build_constraints,
    prepare_ranking,
)


def banner(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def show_ranking(prep) -> None:
    parts = [f"位置{row['position']}={row['work']}"
             f"(原始命中 {row['bib']}@{row['original_position']})"
             for row in prep.rows]
    print(f"  [{prep.name}] 紧排截断后（cut={prep.cut}，权重{prep.weights}）："
          + "，".join(parts))
    for d in prep.dropped_repeats:
        print(f"        重复命中丢弃：{d['bib']}@{d['original_position']}"
              f" -> 作品 {d['work']} 已保留首次出现")


def show_diff(result) -> None:
    print(f"  目录修订：{result.revision}；合法完整赋值数：{result.feasible_count}")
    coef = ", ".join(f"{w}:{c}" for w, c in result.coefficients.items())
    print(f"  差量线性式 score_A-score_B = Σ 系数×相关度，系数 {{{coef}}}")
    lo, hi = result.min_witness, result.max_witness
    print(f"  最小值 {result.min_diff}：赋值 {lo.assignment}，"
          f"(A分 {lo.score_a}, B分 {lo.score_b})")
    print(f"  最大值 {result.max_diff}：赋值 {hi.assignment}，"
          f"(A分 {hi.score_a}, B分 {hi.score_b})")
    labels = {"A_win": "A 胜", "tie": "平", "B_win": "B 胜（负）"}
    can = "、".join(name for k, name in labels.items() if result.outcomes[k])
    cannot = "、".join(name for k, name in labels.items()
                      if not result.outcomes[k])
    print(f"  可发生：{can or '（无）'}；不可发生：{cannot or '（无）'}")
    if result.crosses_zero and not result.outcomes["tie"]:
        print("  注意：min≤0≤max 差值跨零，但枚举中不存在 diff=0 的合法赋值，"
              "不能推定可打平！")
    if result.tie_assignments:
        print(f"  可打平赋值（共 {len(result.tie_assignments)} 组）："
              f"{result.tie_assignments}")


def expect_incompat(judgments: QueryJudgments, catalog: PartitionCatalog,
                    kind: str) -> IncompatibleEvidence:
    try:
        build_constraints(judgments, catalog)
    except IncompatibleEvidence as err:
        assert err.kind == kind, (err.kind, kind)
        print(f"  已标出不相容 [{kind}]，涉事证据：{err.evidence_ids}")
        for k, v in err.detail.items():
            print(f"    {k}: {v}")
        return err
    raise AssertionError(f"预期 {kind} 未发生")


# --------------------------------------------------------------- S0 题面三算例


def demo_prompt_examples() -> None:
    banner("S0 题面算例：A 排 X、Y；B 排 Y、X")
    catalog = PartitionCatalog(["X", "Y"])
    hits_a, hits_b = ["X", "Y"], ["Y", "X"]

    j = QueryJudgments().add_interval("IX", "X", 2, 2) \
                        .add_interval("IY", "Y", 0, 1)
    r = analyze_diff(j, catalog, hits_a, hits_b, weights=[2, 1], cut=2)
    show_ranking(prepare_ranking("A", hits_a, catalog, [2, 1], 2))
    show_ranking(prepare_ranking("B", hits_b, catalog, [2, 1], 2))
    show_diff(r)
    assert (r.min_diff, r.max_diff) == (1, 2)
    assert r.outcomes == {"A_win": True, "tie": False, "B_win": False}

    r2 = analyze_diff(j, catalog, hits_a, hits_b, weights=[1, 1], cut=2)
    show_diff(r2)
    assert r2.min_diff == r2.max_diff == 0 and r2.outcomes["tie"]
    print("  -> 固定判断、权重换 (1,1)：系数全 0，必平")

    j2 = QueryJudgments().add_interval("IX", "X", 2, 2) \
                         .add_interval("IY2", "Y", 2, 3)
    r3 = analyze_diff(j2, catalog, hits_a, hits_b, weights=[2, 1], cut=2)
    show_diff(r3)
    assert (r3.min_diff, r3.max_diff) == (-1, 0)
    assert r3.outcomes == {"A_win": False, "tie": True, "B_win": True}
    print("  -> Y 的有效标注改为 2..3：可能平或负，A 不可能胜")


# ------------------------------------------------- S1 重复命中 + 合并 + 撤销


def demo_merge_repeat_undo() -> None:
    banner("S1 别名合并、重复命中紧排，以及撤销后原始证据各自恢复")
    catalog = PartitionCatalog(["x1", "x2", "y"], revision="r0")
    catalog.merge_alias("x2", "x1", revision="r1-merge-x2->x1")
    print(f"  修订轨迹：{catalog.revisions}")
    print(f"  r1 规范作品视图：{catalog.works()}")

    judgments = (
        QueryJudgments()
        .add_interval("I1", "x1", 2, 3)   # x1 相关度 2..3
        .add_interval("I2", "x2", 0, 3)   # 宽松证据；合并交集仍为 2..3
        .add_interval("I3", "y", 0, 1)
        .add_relation("R1", "x1", "y", ">")  # 甲严格高于乙
    )
    print("  原始证据（对象始终是原始记录，不随合并改写）：")
    for ev in judgments.all_evidence():
        if hasattr(ev, "low"):
            print(f"    {ev.evidence_id}: {ev.bib} ∈ [{ev.low},{ev.high}]")
        else:
            print(f"    {ev.evidence_id}: {ev.left_bib} {ev.relation} "
                  f"{ev.right_bib}（{RELATION_TEXT[ev.relation]}）")

    cs = build_constraints(judgments, catalog)
    print("  归并到作品级的区间交集："
          + ", ".join(f"{w} 来源{ids}" for w, ids in
                      cs.interval_sources.items()))

    hits_a = ["x1", "x2", "y"]   # x2 与 x1 同作品，重复命中
    hits_b = ["y", "x2", "x1"]   # 两侧重复位置不同
    weights = [2, 1]
    show_ranking(prepare_ranking("A", hits_a, catalog, weights, 2))
    show_ranking(prepare_ranking("B", hits_b, catalog, weights, 2))
    r = analyze_diff(judgments, catalog, hits_a, hits_b, weights, cut=2)
    show_diff(r)
    # diff = x - y，x∈2..3、y∈0..1 且 x>y
    assert (r.min_diff, r.max_diff) == (1, 3)
    assert r.min_witness.assignment == {"x1": 2, "y": 1}
    assert r.max_witness.assignment == {"x1": 3, "y": 0}
    assert r.outcomes == {"A_win": True, "tie": False, "B_win": False}

    # 撤销别名：作品重新拆开，原始证据对象与各自归属原样恢复
    catalog.undo_last()
    print(f"  撤销后修订：{catalog.revision}；作品视图：{catalog.works()}")
    cs0 = build_constraints(judgments, catalog)
    assert cs0.works == ["x1", "x2", "y"]
    recovered = {eid: judgments.evidence_by_id(eid).bib
                 for eid in ("I1", "I2", "I3")}
    print(f"  原始证据对象恢复核对：{recovered}")
    assert recovered == {"I1": "x1", "I2": "x2", "I3": "y"}
    r0 = analyze_diff(judgments, catalog, hits_a, hits_b,
                      weights=[2, 1, 1], cut=3)
    show_diff(r0)
    # diff = x1 - y（x2 系数 0）；x1>y 恒成立
    assert (r0.min_diff, r0.max_diff) == (1, 3)
    assert r0.min_witness.assignment == {"x1": 2, "x2": 0, "y": 1}
    assert r0.max_witness.assignment == {"x1": 3, "x2": 0, "y": 0}


# ------------------------------------------------------- S2 三类不相容证据


def demo_conflicts() -> None:
    banner("S2 互不相容证据的标出（三类）")

    print("  (a) 上下界冲突：a1 标 0..1、a2 标 2..3，合并后区间交集为空")
    cat = PartitionCatalog(["a1", "a2"])
    cat.merge_alias("a2", "a1", "r1")
    j = QueryJudgments().add_interval("Ia1", "a1", 0, 1) \
                        .add_interval("Ia2", "a2", 2, 3)
    err = expect_incompat(j, cat, "interval_conflict")
    assert set(err.evidence_ids) == {"Ia1", "Ia2"}

    print("  (b) 严格自指：a1 ≠ a2 在各自独立时合法，合并后两端同一作品")
    cat2 = PartitionCatalog(["a1", "a2"])
    j2 = QueryJudgments().add_relation("Rneq", "a1", "a2", "!=")
    assert build_constraints(j2, cat2).is_feasible()       # 未合并：合法
    cat2.merge_alias("a2", "a1", "r1")
    err2 = expect_incompat(j2, cat2, "strict_self_relation")
    assert err2.evidence_ids == ["Rneq"]

    print("  (c) 联合无解：P∈[2,3]、Q∈[0,1] 各自非空，但要求 P < Q")
    cat3 = PartitionCatalog(["P", "Q"])
    j3 = QueryJudgments().add_interval("IP", "P", 2, 3) \
                         .add_interval("IQ", "Q", 0, 1) \
                         .add_relation("Rlt", "P", "Q", "<")
    err3 = expect_incompat(j3, cat3, "joint_unsatisfiable")
    assert set(err3.evidence_ids) == {"IP", "IQ", "Rlt"}
    print("      极小核心去掉任一约束即有解，证明三条证据缺一不可地冲突")


# ------------------------------------------------- S3 跨零却无法平局（关键）


def demo_cross_zero_no_tie() -> None:
    banner("S3 差值跨零却无法平局")

    print("  (a) 关系版：x ≠ y，A 排 [x,z]、B 排 [y,z]，权重 (2,1)")
    cat = PartitionCatalog(["x", "y", "z"])
    j = QueryJudgments().add_relation("Rne", "x", "y", "!=")
    r = analyze_diff(j, cat, ["x", "z"], ["y", "z"],
                     weights=[2, 1], cut=2)
    show_diff(r)
    # diff = 2(x-y)，x≠y => 差只可能是 ±2,±4,±6，永不取 0
    assert r.crosses_zero and not r.outcomes["tie"]
    assert (r.min_diff, r.max_diff) == (-6, 6)
    assert r.min_witness.assignment["x"] == 0
    assert r.min_witness.assignment["y"] == 3
    assert r.max_witness.assignment == {"x": 3, "y": 0, "z": 0}
    assert r.outcomes == {"A_win": True, "tie": False, "B_win": True}
    assert r.tie_assignments == []

    print("  (b) 纯区间版：p∈[0,1]、q∈[2,3]，填充作品 z 固定为 0，权重(4,1)")
    cat2 = PartitionCatalog(["p", "q", "z"])
    j2 = (QueryJudgments()
          .add_interval("Ip", "p", 0, 1)
          .add_interval("Iq", "q", 2, 3)
          .add_interval("Iz", "z", 0, 0))   # z 恒为 0，只用来占位次
    # A=[p,z]、B=[z,q]：z 的系数 1-4=-3 但其值恒 0，差量只剩 4p - q
    r2 = analyze_diff(j2, cat2, ["p", "z"], ["z", "q"],
                      weights=[4, 1], cut=2)
    show_diff(r2)
    # diff = 4p - q：p=0 时为 -2/-3，p=1 时为 +1/+2，跳过 0
    assert r2.coefficients == {"p": 4, "q": -1, "z": -3}
    assert r2.crosses_zero and not r2.outcomes["tie"]
    assert (r2.min_diff, r2.max_diff) == (-3, 2)
    assert r2.min_witness.assignment == {"p": 0, "q": 3, "z": 0}
    assert r2.max_witness.assignment == {"p": 1, "q": 2, "z": 0}


# ------------------------------------------------- S4 对接 SQLite 目录修订


def demo_sqlite_catalog_adapter() -> None:
    banner("S4 与 library.catalog.CatalogService 的不可变修订对接")
    from library import LibrarySystem

    system = LibrarySystem(":memory:")
    res = system.catalog.submit_revision(
        revision="v1",
        bibs=["b1", "b2", "b3"],
        aliases=[{"from": "b2", "to": "b1"}],   # b2 是 b1 的别名
        copies=[
            {"id": "c1", "book": "b1", "branch": "北馆", "format": "精装"},
            {"id": "c2", "book": "b2", "branch": "北馆", "format": "平装"},
            {"id": "c3", "book": "b3", "branch": "南馆", "format": "平装"},
        ],
        op_id="seed-v1",
    )
    assert res["status"] == "ok"
    canonical = system.catalog.canonical_map("v1")
    print(f"  SQLite 修订 v1 的规范映射：{canonical}")

    catalog = PartitionCatalog.from_mapping(
        ["b1", "b2", "b3"], canonical, revision="v1")
    print(f"  复评引擎看到的作品视图：{catalog.works()}")
    judgments = (
        QueryJudgments()
        .add_interval("J1", "b1", 1, 2)
        .add_interval("J2", "b2", 1, 1)   # 并入作品 b1 后交集为 1..2
        .add_interval("J3", "b3", 0, 2)
    )
    r = analyze_diff(judgments, catalog,
                     hits_a=["b2", "b3"], hits_b=["b3", "b2"],
                     weights=[3, 1], cut=2)
    show_diff(r)
    # diff = 2(x_b1 - x_b3)；b1 交集为 1..2、b3 ∈ 0..2
    assert (r.min_diff, r.max_diff) == (-2, 2)
    assert {"b1": 1, "b3": 1} in r.tie_assignments
    system.close()


def main() -> None:
    demo_prompt_examples()
    demo_merge_repeat_undo()
    demo_conflicts()
    demo_cross_zero_no_tie()
    demo_sqlite_catalog_adapter()
    banner("全部复评场景断言通过")


if __name__ == "__main__":
    main()
