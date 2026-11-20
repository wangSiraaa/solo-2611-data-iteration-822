"""合并书目下两版检索排名的可证差值（纯标准库，无持久化依赖）。

子系统边界：
- 证据（人工判断）永远挂在**原始书目记录**上：每条记录一个 0..3 的整数
  相关度区间，记录之间可有相对关系（=、≠、<、>、≤、≥）。
- 目录修订只决定“原始记录 -> 规范作品”的归并视图，不复制、不篡改证据；
  撤销别名后，原始证据对象原样可取回。
- 两版检索器的命中清单各自按同一目录修订做“保留每个规范作品的第一次
  出现 -> 紧排 -> 截断”，分数 = Σ 位次权重 × 该作品相关度。
- A、B 的分差必须在**同一组**合法人工判断赋值下计算；作品数 ≤ 8，故直接
  枚举 4^n 个整数赋值求精确最小/最大值，平局只能由枚举中真实出现差 0
  来认定，不能从 min≤0≤max 跨零推定。

三类不相容（题目要求全部标出涉事证据）：
1. strict_self_relation ：合并造成严格关系（≠/< />）的两端落到同一作品；
2. interval_conflict    ：同一作品上的若干区间交集为空（上下界冲突）；
3. joint_unsatisfiable  ：单看区间都非空，但区间与关系联合起来没有任何
                          合法整数赋值；返回一个极小不相容证据核心。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

# 相关度只允许 0..3 的整数
LEVELS = (0, 1, 2, 3)
MAX_WORKS = 8

# 关系符号 -> (a, b) 需满足的谓词
RELATIONS = ("=", "!=", "<", ">", "<=", ">=")
RELATION_TEXT = {
    "=": "两者相同",
    "!=": "两者不同",
    "<": "甲严格低于乙",
    ">": "甲严格高于乙",
    "<=": "甲不高于乙",
    ">=": "甲不低于乙",
}
_REL_PRED = {
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
}
_STRICT_RELATIONS = {"!=", "<", ">"}


class RelevancyError(Exception):
    """本子系统所有错误的基类。"""


class IncompatibleEvidence(RelevancyError):
    """人工判断在当前目录修订下互不相容。

    kind 取 strict_self_relation / interval_conflict / joint_unsatisfiable；
    evidence_ids 给出全部涉事原始证据 ID，便于馆员回退核对。
    """

    def __init__(self, kind: str, message: str, evidence_ids: list[str],
                 detail: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.evidence_ids = list(evidence_ids)
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "evidence_ids": list(self.evidence_ids),
            "detail": self.detail,
        }


# ------------------------------------------------------------------ 证据模型


@dataclass(frozen=True)
class IntervalEvidence:
    """原始书目记录的相关度区间：low <= 相关度 <= high，整数 0..3。

    无证据的记录默认 0..3，不产生任何 IntervalEvidence。
    """

    evidence_id: str
    bib: str
    low: int
    high: int

    def __post_init__(self) -> None:
        if not (self.low in LEVELS and self.high in LEVELS):
            raise RelevancyError(f"相关度越界: {self.low}..{self.high}")
        if self.low > self.high:
            raise RelevancyError(f"区间上下界倒挂: {self.low}..{self.high}")


@dataclass(frozen=True)
class RelationEvidence:
    """两条原始书目记录之间的相对判断，left 为“甲”、right 为“乙”。"""

    evidence_id: str
    left_bib: str
    right_bib: str
    relation: str

    def __post_init__(self) -> None:
        if self.relation not in _REL_PRED:
            raise RelevancyError(f"未知关系: {self.relation}")
        if self.left_bib == self.right_bib:
            # 原始层自指：= / <= / >= 平凡成立，严格关系当场非法
            if self.relation in _STRICT_RELATIONS:
                raise RelevancyError("原始证据上的严格自指不合法")


@dataclass
class QueryJudgments:
    """一次查询下、挂在原始书目记录上的全部人工判断。

    目录修订可以换，判断对象（原始 bib）不变；这正是“拆开后须能恢复
    原始证据各自对象”的落点。
    """

    intervals: list[IntervalEvidence] = field(default_factory=list)
    relations: list[RelationEvidence] = field(default_factory=list)

    def add_interval(self, evidence_id: str, bib: str, low: int,
                     high: int) -> "QueryJudgments":
        self.intervals.append(IntervalEvidence(evidence_id, bib, low, high))
        return self

    def add_relation(self, evidence_id: str, left_bib: str, right_bib: str,
                     relation: str) -> "QueryJudgments":
        self.relations.append(
            RelationEvidence(evidence_id, left_bib, right_bib, relation)
        )
        return self

    def all_evidence(self) -> list:
        return list(self.intervals) + list(self.relations)

    def evidence_by_id(self, evidence_id: str):
        for ev in self.all_evidence():
            if ev.evidence_id == evidence_id:
                return ev
        return None


# ------------------------------------------------------- 目录修订（合并/撤销）


class PartitionCatalog:
    """内存版带修订目录：维护原始记录到规范作品的划分。

    - 初始修订里每条记录自成一个规范作品（稳定作品 ID 取记录名）。
    - merge_alias 把两个规范作品并成一个，生成新修订；旧修订不可变。
    - undo_last 回到上一修订，别名即被拆除，成员原 ID 全部还在。
    - 任意“原始 bib -> 规范作品”的外部映射（例如 SQLite 版 CatalogService
      的 canonical_map）都可用 from_mapping 包成同一接口。
    """

    def __init__(self, bibs: list[str], revision: str = "r0") -> None:
        if len(set(bibs)) != len(bibs):
            raise RelevancyError("原始书目记录重复")
        self._bibs = list(bibs)
        # 历史栈：每项是一份完整的 bib -> canonical 映射（不可变快照语义）
        self._history: list[dict[str, str]] = [
            {b: b for b in bibs}
        ]
        self._names: list[str] = [revision]

    @classmethod
    def from_mapping(cls, bibs: list[str], mapping: dict[str, str],
                     revision: str = "external") -> "PartitionCatalog":
        """用外部 canonical_map 构造（适配 library.catalog.CatalogService）。"""
        cat = cls(bibs, revision)
        for bib in bibs:
            if bib not in mapping:
                raise RelevancyError(f"规范映射缺少书目 {bib}")
        cat._history[-1] = dict(mapping)
        return cat

    @property
    def revision(self) -> str:
        return self._names[-1]

    @property
    def revisions(self) -> list[str]:
        return list(self._names)

    def canonical_map(self, revision: str | None = None) -> dict[str, str]:
        idx = self._names.index(revision) if revision is not None \
            else len(self._names) - 1
        return dict(self._history[idx])

    def works(self, revision: str | None = None) -> dict[str, list[str]]:
        """规范作品 -> 成员原始记录列表（保留原始身份，不揉掉）。"""
        mapping = self.canonical_map(revision)
        out: dict[str, list[str]] = {}
        for bib, canon in mapping.items():
            out.setdefault(canon, []).append(bib)
        for members in out.values():
            members.sort()
        return dict(sorted(out.items()))

    def merge_alias(self, alias_bib: str, canonical_bib: str,
                    revision: str | None = None) -> str:
        """声明 alias_bib 是 canonical_bib 的别名，合并两者所在作品。

        返回新修订名。自合并（两者已是同一作品）拒绝，避免制造无意义修订。
        """
        mapping = self.canonical_map()
        if alias_bib not in mapping or canonical_bib not in mapping:
            raise RelevancyError("别名终点不在目录内")
        src, dst = mapping[alias_bib], mapping[canonical_bib]
        if src == dst:
            raise RelevancyError("两个记录已属同一规范作品，拒绝自合并")
        new_map = {b: (dst if c == src else c) for b, c in mapping.items()}
        name = revision or f"r{len(self._history)}"
        self._history.append(new_map)
        self._names.append(name)
        return name

    def undo_last(self) -> str:
        """撤销最近一次合并，回到上一修订；初始修订不可再撤。"""
        if len(self._history) <= 1:
            raise RelevancyError("初始修订无法撤销")
        self._history.pop()
        self._names.pop()
        return self.revision


# ------------------------------------------------------------------ 约束求解


@dataclass(frozen=True)
class Constraint:
    """作品级约束，provenance 记录它由哪些原始证据归并而来。"""

    kind: str                      # interval / relation
    evidence_ids: tuple[str, ...]
    work: str | None = None        # interval 约束的作品
    low: int | None = None
    high: int | None = None
    left_work: str | None = None   # relation 约束的两个作品
    right_work: str | None = None
    relation: str | None = None

    def holds(self, assignment: dict[str, int]) -> bool:
        if self.kind == "interval":
            v = assignment[self.work]
            return self.low <= v <= self.high
        a, b = assignment[self.left_work], assignment[self.right_work]
        return _REL_PRED[self.relation](a, b)


@dataclass
class ConstraintSet:
    works: list[str]
    constraints: list[Constraint]
    # 作品 -> 归并到它身上的区间证据 ID（区间取交集时用）
    interval_sources: dict[str, list[str]]

    def is_feasible(self, constraints: list[Constraint] | None = None) -> bool:
        return next(self.feasible_assignments(constraints), None) is not None

    def feasible_assignments(
        self, constraints: list[Constraint] | None = None
    ):
        """枚举满足全部约束的完整赋值（每个作品一个 0..3 整数）。"""
        cs = self.constraints if constraints is None else constraints
        for values in product(LEVELS, repeat=len(self.works)):
            assignment = dict(zip(self.works, values))
            if all(c.holds(assignment) for c in cs):
                yield assignment


def build_constraints(judgments: QueryJudgments,
                      catalog: PartitionCatalog,
                      revision: str | None = None) -> ConstraintSet:
    """把原始证据按指定修订归并到规范作品，并做三类不相容检查。"""
    mapping = catalog.canonical_map(revision)
    for ev in judgments.all_evidence():
        targets = (ev.bib,) if isinstance(ev, IntervalEvidence) else (
            ev.left_bib, ev.right_bib)
        for bib in targets:
            if bib not in mapping:
                raise RelevancyError(f"证据 {ev.evidence_id} 指向目录外记录 {bib}")

    works = sorted(set(mapping.values()))
    if len(works) > MAX_WORKS:
        raise RelevancyError(
            f"每次查询至多 {MAX_WORKS} 个规范作品，当前 {len(works)} 个")

    # 1) 区间归并：同一作品的全部区间同时成立 -> 取交集
    raw: dict[str, list[IntervalEvidence]] = {}
    for ev in judgments.intervals:
        raw.setdefault(mapping[ev.bib], []).append(ev)
    interval_sources = {w: [e.evidence_id for e in evs]
                        for w, evs in raw.items()}
    constraints: list[Constraint] = []
    for work, evs in raw.items():
        low = max(e.low for e in evs)
        high = min(e.high for e in evs)
        ids = [e.evidence_id for e in evs]
        if low > high:
            bounds = ", ".join(
                f"{e.evidence_id}({e.bib}:{e.low}..{e.high})" for e in evs)
            raise IncompatibleEvidence(
                "interval_conflict",
                f"作品 {work} 上区间交集为空：{bounds}",
                ids,
                {"work": work, "intersection": [low, high]},
            )
        constraints.append(Constraint(
            "interval", tuple(sorted(ids)), work=work, low=low, high=high))

    # 2) 关系归并到作品级；严格关系落到同一作品即严格自指
    self_rel: list[RelationEvidence] = []
    for ev in judgments.relations:
        lw, rw = mapping[ev.left_bib], mapping[ev.right_bib]
        if lw == rw and ev.relation in _STRICT_RELATIONS:
            self_rel.append(ev)
            continue
        constraints.append(Constraint(
            "relation", (ev.evidence_id,), left_work=lw, right_work=rw,
            relation=ev.relation))
    if self_rel:
        ids = [e.evidence_id for e in self_rel]
        detail = [
            {"evidence_id": e.evidence_id, "relation": e.relation,
             "left_bib": e.left_bib, "right_bib": e.right_bib,
             "work": mapping[e.left_bib]}
            for e in self_rel
        ]
        raise IncompatibleEvidence(
            "strict_self_relation",
            "合并后严格关系两端落在同一规范作品（严格自指）",
            ids, {"conflicts": detail})

    cs = ConstraintSet(works, constraints, interval_sources)

    # 3) 联合可满足性；无解时求一个极小不相容证据核心
    if not cs.is_feasible():
        core = _minimal_unsat_core(cs)
        ids = sorted({eid for c in core for eid in c.evidence_ids})
        raise IncompatibleEvidence(
            "joint_unsatisfiable",
            "区间与相对关系联合无解，极小不相容证据核心：" + ", ".join(ids),
            ids,
            {"core_constraints": [
                {"evidence_ids": list(c.evidence_ids), "kind": c.kind,
                 "relation": c.relation, "work": c.work,
                 "low": c.low, "high": c.high}
                for c in core]})
    return cs


def _minimal_unsat_core(cs: ConstraintSet) -> list[Constraint]:
    """删除式极小化：返回去掉任一约束即可解的一个不相容子集。

    规模上限 8 作品、证据量小，直接反复调用可行性枚举即可；
    先按证据数最少的不可满足子集方向贪心，拿到的核心便于馆员核对。
    """
    core = list(cs.constraints)
    changed = True
    while changed:
        changed = False
        for c in list(core):
            trial = [x for x in core if x is not c]
            if not trial or not cs.is_feasible(trial):
                core = trial
                changed = True
                break
    return core


# ------------------------------------------------------- 命中清单：紧排与截断


@dataclass
class PreparedRanking:
    """一份命中清单按目录修订处理后的样子（含可审计的逐位轨迹）。"""

    name: str
    rows: list[dict]            # 紧排后的位次：位置、作品、原始 bib、原始位次
    dropped_repeats: list[dict] # 因作品已出现而被丢的重复命中
    cut: int                    # 实际截断位置数
    weights: tuple[int, ...]    # 实际使用的逐位权重
    work_position: dict[str, int]  # 作品 -> 紧排后位置（1 起）


def prepare_ranking(name: str, hits: list[str], catalog: PartitionCatalog,
                    weights: list[int], cut: int | None = None,
                    revision: str | None = None) -> PreparedRanking:
    """保留每个规范作品的第一次出现，紧排后按 cut 截断。

    hits 是原始书目记录 ID 的有序命中清单；同一作品重复命中只保留最早
    一次，其余进 dropped_repeats 留痕。权重为逐位非负整数，位次 1 起。
    """
    if any(w < 0 for w in weights):
        raise RelevancyError("位次权重必须非负")
    mapping = catalog.canonical_map(revision)
    for bib in hits:
        if bib not in mapping:
            raise RelevancyError(f"命中记录 {bib} 不在目录内")

    limit = len(weights) if cut is None else min(cut, len(weights))
    # 先对完整清单去重紧排：每个规范作品只保留第一次出现，
    # 后续重复命中（哪怕落在截断点之后）全部留痕
    seen: set[str] = set()
    compacted: list[dict] = []
    dropped: list[dict] = []
    for original_pos, bib in enumerate(hits, start=1):
        work = mapping[bib]
        if work in seen:
            dropped.append({"original_position": original_pos, "bib": bib,
                            "work": work})
            continue
        seen.add(work)
        compacted.append({"work": work, "bib": bib,
                          "original_position": original_pos})
    # 紧排之后再截断
    rows = compacted[:limit]

    work_position = {row["work"]: pos for pos, row in enumerate(rows, start=1)}
    return PreparedRanking(
        name=name,
        rows=[dict(row, position=pos) for pos, row in enumerate(rows, 1)],
        dropped_repeats=dropped,
        cut=limit,
        weights=tuple(weights[:limit]),
        work_position=work_position,
    )


# ------------------------------------------------------------- 差值精确求解


@dataclass
class DiffWitness:
    assignment: dict[str, int]   # 全部规范作品的完整相关度赋值
    diff: int                    # score_A - score_B
    score_a: int
    score_b: int


@dataclass
class DiffResult:
    revision: str
    works: list[str]
    feasible_count: int          # 合法完整赋值数
    coefficients: dict[str, int] # 每作品在 diff = Σ c_w * v_w 中的系数
    min_diff: int
    max_diff: int
    min_witness: DiffWitness
    max_witness: DiffWitness
    outcomes: dict[str, bool]    # 胜/平/负是否可发生
    tie_assignments: list[dict[str, int]]  # 能打平的完整赋值（可能为空）

    @property
    def crosses_zero(self) -> bool:
        """min..max 区间是否跨过 0；注意跨零不代表能打平。"""
        return self.min_diff <= 0 <= self.max_diff


def _score(prepared: PreparedRanking, assignment: dict[str, int]) -> int:
    total = 0
    for pos, row in enumerate(prepared.rows, start=1):
        total += prepared.weights[pos - 1] * assignment[row["work"]]
    return total


def analyze_diff(judgments: QueryJudgments,
                 catalog: PartitionCatalog,
                 hits_a: list[str],
                 hits_b: list[str],
                 weights: list[int],
                 cut: int | None = None,
                 cut_a: int | None = None,
                 cut_b: int | None = None,
                 revision: str | None = None) -> DiffResult:
    """求 score_A - score_B 在同一组合法人工判断下的精确最小/最大值。

    weights 为两版共用的逐位非负整数权重；cut 给定时两版同截断，
    需要分别截断时改用 cut_a/cut_b（此时不要再给 cut）。
    A、B 共用同一赋值枚举，绝不拆成两套独立标注。
    """
    if cut_a is None:
        cut_a = cut
    if cut_b is None:
        cut_b = cut
    if len(weights) > MAX_WORKS:
        # 权重长度只需覆盖紧排后的位次，而位次不会超过作品数
        raise RelevancyError(f"权重位次超过 {MAX_WORKS}")
    cs = build_constraints(judgments, catalog, revision)
    prep_a = prepare_ranking("A", hits_a, catalog, weights, cut_a, revision)
    prep_b = prepare_ranking("B", hits_b, catalog, weights, cut_b, revision)

    # diff = Σ_w (wA_w - wB_w) * v_w；未上榜作品系数为 0
    coef: dict[str, int] = {w: 0 for w in cs.works}
    for row in prep_a.rows:
        coef[row["work"]] += prep_a.weights[row["position"] - 1]
    for row in prep_b.rows:
        coef[row["work"]] -= prep_b.weights[row["position"] - 1]

    min_w = max_w = None
    tie_assignments: list[dict[str, int]] = []
    feasible = 0
    for assignment in cs.feasible_assignments():
        feasible += 1
        sa, sb = _score(prep_a, assignment), _score(prep_b, assignment)
        diff = sa - sb
        witness = DiffWitness(dict(assignment), diff, sa, sb)
        if min_w is None or diff < min_w.diff:
            min_w = witness
        if max_w is None or diff > max_w.diff:
            max_w = witness
        if diff == 0:
            tie_assignments.append(dict(assignment))

    outcomes = {
        "A_win": max_w.diff > 0,    # 存在赋值使 diff > 0
        "tie": bool(tie_assignments),  # 必须真实出现 diff == 0
        "B_win": min_w.diff < 0,
    }
    return DiffResult(
        revision=catalog.revision if revision is None else revision,
        works=list(cs.works),
        feasible_count=feasible,
        coefficients=coef,
        min_diff=min_w.diff,
        max_diff=max_w.diff,
        min_witness=min_w,
        max_witness=max_w,
        outcomes=outcomes,
        tie_assignments=tie_assignments,
    )
