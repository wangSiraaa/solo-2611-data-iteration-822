"""端到端演示：严格重放 materials/765/input.json，再追加边界场景。

运行：python3.11 demo.py
所有断言通过且进程退出码为 0，即说明材料中的预期全部兑现。
"""

from __future__ import annotations

import json
from pathlib import Path

from library import IdempotencyConflict, LibrarySystem

MATERIAL = Path(__file__).parent / "materials" / "765" / "input.json"
DB_PATH = str(Path(__file__).parent / "run_library.db")


def load_initial(system: LibrarySystem, data: dict) -> None:
    """按合成材料装载初态：两个目录修订、三个副本、三张票据与既有保留 H3。"""
    copies = data["copies"]
    for cat in data["catalogs"]:
        system.catalog.submit_revision(
            revision=cat["revision"],
            bibs=list(data["books"]),
            aliases=cat["aliases"],
            copies=copies,
            op_id=f"seed-catalog-{cat['revision']}",
        )

    for t in data["tickets"]:
        system.ledger.register_ticket(
            ticket_id=t["id"],
            reader_id=t["reader"],
            original_bib=t["book"],
            arrival_seq=t["seq"],
            allowed_branches=t["branches"],
            allowed_formats=t["formats"],
            op_id=f"seed-ticket-{t['id']}",
        )

    system.catalog.activate_revision("v1")
    held = data["held"][0]
    system.ledger.seed_hold(
        hold_id=held["id"],
        revision=held["catalog"],
        ticket_id=held["ticket"],
        reader_id=held["reader"],
        copy_id=held["copy"],
        branch=held["branch"],
    )


def run_material_operations(system: LibrarySystem, data: dict) -> dict:
    results: dict[str, dict] = {}
    for op in data["operations"]:
        action = op["action"]
        if action == "preview":
            r = system.ledger.prepare(op["id"], op["catalog"], op["copy"])
        elif action == "switch_catalog":
            r = system.catalog.activate_revision(op["catalog"])
        elif action == "confirm_preview":
            r = system.ledger.commit(op["id"], op["preview"])
        elif action == "reserve":
            r = system.ledger.reserve(op["id"], op["catalog"], op["copy"])
        elif action == "pickup":
            r = system.ledger.pickup(
                op["id"], op["hold"], op["reader"], op["copy"]
            )
        else:
            raise AssertionError(f"未知操作 {action}")
        results[op.get("id", action)] = r
        # J4 被拒后必须当场验证 H3 未被消耗（下一步 J5 才合法兑现它）
        if op.get("id") == "J4":
            assert r["status"] == "rejected"
            assert system.ledger.get_hold("H3")["status"] == "held"
    return results


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    # 干净起步，保证可重复运行
    for suffix in ("", "-wal", "-shm"):
        p = Path(DB_PATH + suffix)
        if p.exists():
            p.unlink()

    data = json.loads(MATERIAL.read_text(encoding="utf-8"))
    system = LibrarySystem(DB_PATH)

    # ---------------------------------------------------------------- 初态
    banner("初态：v1 目录、三张票据、H3 已留给 TC/RC 的 C3（尚未过期）")
    load_initial(system, data)
    holds = system.ledger.get_active_reservations()
    assert len(holds) == 1 and holds[0]["hold_id"] == "H3"
    assert holds[0]["ticket_id"] == "TC" and holds[0]["copy_id"] == "C3"
    print(json.dumps(holds, ensure_ascii=False, indent=2))

    banner("公开查询：v1 与 v2 规范目录（成员原 ID 与副本原始身份均保留）")
    cat_v1 = system.catalog.canonical_catalog("v1")
    cat_v2 = system.catalog.canonical_catalog("v2")
    print("v1:", json.dumps(cat_v1, ensure_ascii=False))
    print("v2:", json.dumps(cat_v2, ensure_ascii=False))
    # v1 两本独立；v2 合并为同一规范作品 WA，WB 仍是成员的原始身份
    assert {w["canonical_bib"] for w in cat_v1["works"]} == {"WA", "WB"}
    v2_work = next(w for w in cat_v2["works"] if w["canonical_bib"] == "WA")
    assert sorted(v2_work["members"]) == ["WA", "WB"]
    c3 = next(c for c in v2_work["copies"] if c["id"] == "C3")
    assert c3["original_bib"] == "WB"  # 合并不揉掉原始书目身份

    # ------------------------------------------------- 材料操作序列的严格重放
    banner("重放材料操作序列")
    r = run_material_operations(system, data)

    print("P1 v1 预览 C1 ->", r["P1"]["status"], "命中票据", r["P1"]["ticket"])
    assert r["P1"]["status"] == "ok" and r["P1"]["ticket"] == "TA"

    print("J1 切到 v2 后提交旧准备 P1 ->", r["J1"]["status"], r["J1"]["error"]["reason"])
    assert r["J1"]["status"] == "rejected"
    assert r["J1"]["error"]["reason"] == "stale_prep_catalog_changed"

    print("J2 v2 为 C1 保留 ->", r["J2"]["status"], r["J2"]["ticket"], r["J2"]["reader"])
    assert (r["J2"]["status"], r["J2"]["ticket"], r["J2"]["hold_id"]) == ("ok", "TB", "H4")

    print("J3 v2 为 C2 保留 ->", r["J3"]["status"], r["J3"]["ticket"], r["J3"]["reader"])
    assert (r["J3"]["status"], r["J3"]["ticket"], r["J3"]["hold_id"]) == ("ok", "TA", "H5")

    print("J4 错误读者 RA 取 C3 ->", r["J4"]["status"], r["J4"]["error"]["reason"])
    assert r["J4"]["status"] == "rejected"
    assert r["J4"]["error"]["reason"] == "reader_mismatch"
    # “不消耗 H3”已在 run_material_operations 内 J4 当场断言；
    # 走到这里时 J5 已由真实读者 RC 合法兑现，H3 应为 fulfilled
    print("J5 真实读者 RC 取 C3 ->", r["J5"]["status"])
    assert r["J5"]["status"] == "ok"
    assert system.ledger.get_hold("H3")["status"] == "fulfilled"

    banner("当前排队与保留（成功/拒绝回执全部公开）")
    print("排队:", json.dumps(system.ledger.get_queue(), ensure_ascii=False))
    print("保留:", json.dumps(system.ledger.get_active_reservations(), ensure_ascii=False))
    receipts = system.ledger.get_receipts()
    for rc in receipts:
        print(f"  #{rc['receipt_id']} {rc['op_id']:<14} {rc['kind']:<16} {rc['status']}")
    assert any(x["op_id"] == "J1" and x["status"] == "rejected" for x in receipts)
    assert any(x["op_id"] == "J4" and x["status"] == "rejected" for x in receipts)

    # ------------------------------------------------- 幂等：同文重放 / 异文拒绝
    banner("幂等：同操作 ID 同内容重放原样返回，异文拒绝且不写库")
    replay = system.ledger.pickup("J5", "H3", "RC", "C3")
    assert replay == r["J5"]
    print("J5 同文重放 -> 同一回执", replay["hold_id"], replay["status"])
    before = len(system.ledger.get_receipts())
    try:
        # 同 op_id J2，但改要 C2：指纹不同，必须抛冲突，且无新增回执/保留
        system.ledger.reserve("J2", "v2", "C2")
        raise AssertionError("异文重放未被拒绝")
    except IdempotencyConflict as exc:
        print("J2 异文重放 ->", type(exc).__name__, exc.op_id)
    assert len(system.ledger.get_receipts()) == before

    # --------------------------------------------- 拆别名 + 等待状态变化失效
    banner("后续修订 v3 拆除 WB->WA 错误别名，并加入新副本 C4")
    # 先在 v2 下准备一张针对 C4 的预览是不可能的（C4 尚不存在）；
    # 改为登记新票据 TD，再演示目录切换使旧准备失效。
    system.ledger.register_ticket(
        "TD", "RD", "WB", 4, ["north"], ["hardcover"], op_id="J6"
    )
    prep_v2 = system.ledger.prepare("P2", "v2", "C1")
    assert prep_v2["status"] == "rejected"  # C1 已被 TB 保留，占用中
    print("v2 下对已占用 C1 的准备 ->", prep_v2["error"]["reason"])

    v3_copies = [
        {"id": "C1", "book": "WA", "branch": "north", "format": "hardcover"},
        {"id": "C2", "book": "WA", "branch": "north", "format": "paperback"},
        {"id": "C3", "book": "WB", "branch": "south", "format": "paperback"},
        {"id": "C4", "book": "WA", "branch": "north", "format": "hardcover"},
    ]
    system.catalog.submit_revision(
        "v3", ["WA", "WB"], [], v3_copies, op_id="J7-cat"
    )
    system.catalog.activate_revision("v3")
    assert system.catalog.canonical_map("v3") == {"WA": "WA", "WB": "WB"}
    print("v3 规范映射（别名已拆除）:", system.catalog.canonical_map("v3"))

    # 旧目录仍可查
    old = system.catalog.get_revision("v2")
    assert old["aliases"] == [{"from": "WB", "to": "WA"}]
    print("旧修订 v2 仍可查，别名边:", old["aliases"])

    # v3 下 TD（原书目 WB）与 C4（作品 WA）已不是同一作品 -> 无合格票据
    no_match = system.ledger.reserve("J8", "v3", "C4")
    assert no_match["status"] == "rejected"
    assert no_match["error"]["reason"] == "no_eligible_ticket"
    print("v3 下 TD 不再匹配 C4 ->", no_match["error"]["reason"])

    # 登记 WA 票据 TE 后，C4 给 TE；再演示 prepare/commit 的等待版本失效
    system.ledger.register_ticket(
        "TE", "RE", "WA", 5, ["north"], ["hardcover"], op_id="J9"
    )
    p5 = system.ledger.prepare("P5", "v3", "C4")
    assert p5["status"] == "ok" and p5["ticket"] == "TE"
    # 准备之后等待状态改变：新票据 TF 到达
    system.ledger.register_ticket(
        "TF", "RF", "WA", 6, ["north"], ["hardcover"], op_id="J10"
    )
    stale = system.ledger.commit("J11", "P5")
    assert stale["status"] == "rejected"
    assert stale["error"]["reason"] == "stale_prep_wait_changed"
    print("准备后等待状态变化 -> 提交被拒:", stale["error"]["reason"])
    # 重新准备并提交：TF 虽新，但 TE 更早，仍只能选 TE
    system.ledger.prepare("P6", "v3", "C4")
    commit = system.ledger.commit("J12", "P6")
    assert commit["status"] == "ok" and commit["ticket"] == "TE"
    print("重新准备后提交 -> C4 命中更早的 TE，hold:", commit["hold_id"])

    # --------------------------------------------- 过期释放：解除后按当前目录重选
    banner("过期解除 H6 后，TE 回队并再次被同一规则选中；旧回执保留")
    receipts_before = len(system.ledger.get_receipts())
    release = system.ledger.release("J13", commit["hold_id"], "expired")
    assert release["status"] == "ok"
    assert system.ledger.get_ticket("TE")["status"] == "waiting"
    again = system.ledger.reserve("J14", "v3", "C4")
    assert again["status"] == "ok" and again["ticket"] == "TE"
    print("释放:", release, "\n重选:", again)
    assert len(system.ledger.get_receipts()) == receipts_before + 2  # 旧回执未删

    # ------------------------------------------------------- 循环别名整批拒绝
    banner("含环修订整批拒绝（自环/多跳环同样处理）")
    cyc = system.catalog.submit_revision(
        "vbad",
        ["WA", "WB"],
        [{"from": "WA", "to": "WB"}, {"from": "WB", "to": "WA"}],
        v3_copies,
        op_id="J15-bad",
    )
    assert cyc["status"] == "rejected" and cyc["error"]["reason"] == "alias_cycle"
    assert "vbad" not in system.catalog.list_revisions()
    print("循环修订拒绝:", cyc["error"])
    # 失败重放同文：返回同一条拒绝回执，不产生第二条
    cyc_replay = system.catalog.submit_revision(
        "vbad",
        ["WA", "WB"],
        [{"from": "WA", "to": "WB"}, {"from": "WB", "to": "WA"}],
        v3_copies,
        op_id="J15-bad",
    )
    assert cyc_replay == cyc
    print("失败请求同文重放 -> 同一拒绝回执")

    # 边终点不存在也整批拒绝
    dangling = system.catalog.submit_revision(
        "vbad2",
        ["WA"],
        [{"from": "WA", "to": "GHOST"}],
        v3_copies,
        op_id="J16-bad",
    )
    assert dangling["status"] == "rejected"
    assert dangling["error"]["reason"] == "edge_endpoint_missing"
    print("悬空边修订拒绝:", dangling["error"]["reason"])

    # ------------------------------------------------------------- 重开持久化
    banner("关闭后重开：占用、原凭据与全部回执原样保留")
    system.close()
    again_system = LibrarySystem.reopen(DB_PATH)
    holds2 = again_system.ledger.get_active_reservations()
    by_copy = {h["copy_id"]: h for h in holds2}
    assert by_copy["C1"]["ticket_id"] == "TB" and by_copy["C1"]["status"] == "held"
    assert by_copy["C2"]["ticket_id"] == "TA" and by_copy["C2"]["status"] == "held"
    assert by_copy["C3"]["ticket_id"] == "TC" and by_copy["C3"]["status"] == "fulfilled"
    assert by_copy["C4"]["ticket_id"] == "TE"
    print(json.dumps(holds2, ensure_ascii=False, indent=2))
    print("回执条数:", len(again_system.ledger.get_receipts()))
    print("当前头修订:", again_system.catalog.head_revision)
    print("副本占用视图:")
    print(json.dumps(again_system.ledger.get_copy_statuses(), ensure_ascii=False, indent=2))
    again_system.close()

    banner("全部断言通过")


if __name__ == "__main__":
    main()
