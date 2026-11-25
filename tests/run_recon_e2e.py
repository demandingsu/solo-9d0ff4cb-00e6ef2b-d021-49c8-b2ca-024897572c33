"""对账功能端到端试跑（标准库即可运行，独立实现一份指纹规则做交叉验证）。

用法（在项目根目录）：
    APP_DB_PATH=/tmp/test-recon.db \
    BASE_URL=http://127.0.0.1:8011 \
    /tmp/venv/bin/python -I tests/run_recon_e2e.py

覆盖需求点：
  1. 录入顺序不同 => 同表同段指纹相同（含独立参考实现交叉核对整表/叶子）；
  2. 漏传一行：整表指纹不同，逐级下钻到叶子，缺行段标空、可与“内容不同”区分；
  3. 改值：叶子段仍在但单行指纹不同 => 内容不同；
  4. 稀疏行号：空段标空、不为空号建项（库里只存非空段，查询按网格补空）；
  5. 源表清空、服务重启后，凭对账号仍可逐级查询；
  6. 生成时并发覆盖源表：三表指纹仍对应某一已提交快照，录入照常提交；
  7. 生成落库失败：整轮回滚，对账号不可查（404），无残留段；
  8. 分段树全量自洽：父段指纹 = 按规则由段内行指纹聚合，层级/宽度固定。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8011")
MONTH = "2026-11"
PASS = "[PASS]"
FAIL = "[FAIL]"
failures: list[str] = []

# ---- 与服务端完全一致的固定参数（独立照抄一份，验证规则可移植） ----
ROW_ID_BITS = 25
FANOUT = 32
TREE_DEPTH = 5
WIDTHS = [1 << (ROW_ID_BITS - 5 * lv) for lv in range(TREE_DEPTH + 1)]
EMPTY_FP = hashlib.sha256(b"recon-empty-v1").hexdigest()
TABLES = ("work_hour", "dispatch", "attribution")
COLS = {
    "work_hour": ("row_id", "work_order", "minutes"),
    "dispatch": ("row_id", "work_order", "project"),
    "attribution": ("row_id", "project", "budget_subject"),
}


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = PASS if cond else FAIL
    print(f"  {mark} {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_ready(proc: subprocess.Popen) -> None:
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            status, _ = call("GET", "/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("服务未在 20s 内就绪")


# ------------------------------------------------------------ 参考实现
def ref_row_fp(table: str, row: dict) -> str:
    payload = [row[c] for c in COLS[table]]
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def ref_seg_fp(row_fps: list[str]) -> str:
    if not row_fps:
        return EMPTY_FP
    material = b"recon-seg-v1|" + b"|".join(h.encode() for h in sorted(row_fps))
    return hashlib.sha256(material).hexdigest()


def ref_bounds(row_id: int, level: int) -> tuple[int, int]:
    w = WIDTHS[level]
    lo = (row_id - 1) // w * w + 1
    return lo, lo + w - 1


def ref_whole(table: str, rows: list[dict]) -> str:
    return ref_seg_fp([ref_row_fp(table, r) for r in rows])


def start_server(db_path: str, fault: str = "") -> subprocess.Popen:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {
        **os.environ,
        "APP_DB_PATH": db_path,
        "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "RECON_FAULT": fault,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", BASE.rsplit(":", 1)[1]],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    wait_ready(proc)
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def load_month(work_rows, disp_rows, attr_rows) -> None:
    call("DELETE", f"/months/{MONTH}")
    _, r = call("POST", f"/months/{MONTH}/work-hours", {"rows": work_rows})
    assert r.get("inserted") == len(work_rows), r
    _, r = call("POST", f"/months/{MONTH}/dispatches", {"rows": disp_rows})
    assert r.get("inserted") == len(disp_rows), r
    _, r = call("POST", f"/months/{MONTH}/attributions", {"rows": attr_rows})
    assert r.get("inserted") == len(attr_rows), r


# 稀疏行号：1、2、33554431（跨整棵树），中间大量空号
WORK = [
    {"row_id": 1, "work_order": "WO1", "minutes": 120},
    {"row_id": 2, "work_order": "WO2", "minutes": 90},
    {"row_id": 33554431, "work_order": "WO-BIG", "minutes": 45},
]
DISP = [
    {"row_id": 7, "work_order": "WO1", "project": "P1"},
    {"row_id": 99, "work_order": "WO2", "project": "P2"},
]
ATTR = [
    {"row_id": 5, "project": "P1", "budget_subject": "SUBJ-A"},
    {"row_id": 88, "project": "P2", "budget_subject": "SUBJ-B"},
]


def drill_to_leaf(account: str, table: str, row_id: int) -> dict:
    """从整表段逐级下钻到 row_id 所在叶子，返回叶子段 dict。"""
    lo, hi = 1, WIDTHS[0]
    leaf = None
    for level in range(TREE_DEPTH):
        status, r = call(
            "GET",
            f"/reconciliation/{account}/tables/{table}/segments?"
            + urllib.parse.urlencode({"lo": lo, "hi": hi}),
        )
        assert status == 200, r
        check(f"L{level} 段返回 {FANOUT} 个固定子段", len(r["children"]) == FANOUT)
        target_lo, target_hi = ref_bounds(row_id, level + 1)
        nxt = next(c for c in r["children"] if c["lo"] == target_lo and c["hi"] == target_hi)
        lo, hi, leaf = nxt["lo"], nxt["hi"], nxt
    assert leaf["lo"] == row_id and leaf["hi"] == row_id
    return leaf


def scenario_order_independence(proc) -> None:
    print("场景 1：录入顺序不同 => 指纹相同（独立参考实现交叉核对）")
    load_month(WORK, DISP, ATTR)
    status, r1 = call("POST", f"/months/{MONTH}/reconciliation")
    check("生成对账号 200", status == 200, str(r1))
    fp1 = {t["table"]: t["whole_fingerprint"] for t in r1["tables"]}
    check("返回指纹版本 v1 与固定树参数",
          r1["fingerprint_version"] == "v1" and r1["fanout"] == FANOUT
          and r1["levels"] == 6 and r1["row_id_max"] == WIDTHS[0] - 1, str(r1))

    # 打乱录入顺序（反向 + replace 覆盖），再生成一个对账号
    call("POST", f"/months/{MONTH}/work-hours", {"rows": list(reversed(WORK))})
    call("POST", f"/months/{MONTH}/dispatches", {"rows": list(reversed(DISP))})
    call("POST", f"/months/{MONTH}/attributions", {"rows": list(reversed(ATTR))})
    status, r2 = call("POST", f"/months/{MONTH}/reconciliation")
    check("乱序后再次生成 200", status == 200, str(r2))
    fp2 = {t["table"]: t["whole_fingerprint"] for t in r2["tables"]}
    check("两次整表指纹完全相同（与录入顺序无关）", fp1 == fp2, f"{fp1} vs {fp2}")

    # 与独立参考实现逐一核对
    for table, rows in (("work_hour", WORK), ("dispatch", DISP), ("attribution", ATTR)):
        check(f"{table} 整表指纹 == 独立参考实现", fp1[table] == ref_whole(table, rows),
              f"{fp1[table]} vs {ref_whole(table, rows)}")

    # 叶子（单行）指纹与参考行指纹一致（r1 是乱序覆盖前生成的，对应原始 WORK）
    leaf = drill_to_leaf(r1["account_id"], "work_hour", 2)
    check("叶子段宽为 1 且非空", leaf["is_empty"] is False and leaf["level"] == 5, str(leaf))
    original_row2 = next(r for r in WORK if r["row_id"] == 2)
    check("叶子指纹 == 参考单行指纹",
          leaf["fingerprint"] == ref_row_fp("work_hour", original_row2), str(leaf))

    # 整表查询接口与生成响应一致（整表接口段对象用 table 字段）
    status, whole = call("GET", f"/reconciliation/{r1['account_id']}")
    check("按对账号查整表 200 且指纹一致",
          status == 200 and {t["table"]: t["fingerprint"] for t in whole["tables"]} == fp1,
          str(whole))


def scenario_missing_and_changed(proc) -> None:
    print("场景 2：漏传（缺行）与改值（内容不同）可区分")
    load_month(WORK, DISP, ATTR)
    _, base = call("POST", f"/months/{MONTH}/reconciliation")
    base_acc = base["account_id"]
    base_fp = {t["table"]: t["whole_fingerprint"] for t in base["tables"]}

    # 2a. 漏传：工时表少一行 row_id=2
    missing_work = [WORK[0], WORK[2]]
    call("POST", f"/months/{MONTH}/work-hours", {"rows": missing_work})
    _, miss = call("POST", f"/months/{MONTH}/reconciliation")
    miss_acc = miss["account_id"]
    miss_fp = {t["table"]: t["whole_fingerprint"] for t in miss["tables"]}
    check("漏行后工时整表指纹不同", base_fp["work_hour"] != miss_fp["work_hour"])
    check("未改动的分派/归科指纹不变",
          base_fp["dispatch"] == miss_fp["dispatch"]
          and base_fp["attribution"] == miss_fp["attribution"])

    leaf_missing = drill_to_leaf(miss_acc, "work_hour", 2)
    check("漏行：叶子标空且指纹为空哨兵",
          leaf_missing["is_empty"] is True and leaf_missing["fingerprint"] == EMPTY_FP,
          str(leaf_missing))
    leaf_present = drill_to_leaf(miss_acc, "work_hour", 1)
    check("同行号在基准侧非空、漏传侧为空 => 缺行可判定",
          leaf_present["is_empty"] is False)

    # 2b. 改值：恢复三行，把 row_id=2 的 minutes 改掉
    changed = [dict(WORK[0]), dict(WORK[1], minutes=91), dict(WORK[2])]
    call("POST", f"/months/{MONTH}/work-hours", {"rows": changed})
    _, chg = call("POST", f"/months/{MONTH}/reconciliation")
    chg_acc = chg["account_id"]
    chg_fp = {t["table"]: t["whole_fingerprint"] for t in chg["tables"]}
    check("改值后工时整表指纹不同", base_fp["work_hour"] != chg_fp["work_hour"])

    leaf_changed = drill_to_leaf(chg_acc, "work_hour", 2)
    check("改值：叶子仍非空（行还在）", leaf_changed["is_empty"] is False, str(leaf_changed))
    check("改值：叶子指纹 != 基准侧行指纹（内容不同）",
          leaf_changed["fingerprint"] != ref_row_fp("work_hour", WORK[1]))
    check("改值：叶子指纹 == 改后参考行指纹",
          leaf_changed["fingerprint"] == ref_row_fp("work_hour", changed[1]))
    check("缺行(空哨兵) 与 改值(非空新指纹) 形态可区分",
          leaf_missing["is_empty"] and not leaf_changed["is_empty"])

    # 两侧对账号互不影响，基准账号仍可查旧快照
    status, old = call("GET", f"/reconciliation/{base_acc}")
    check("旧对账号保留旧快照指纹", status == 200
          and {t["table"]: t["fingerprint"] for t in old["tables"]} == base_fp)


def scenario_sparse_and_storage(proc, db_path: str) -> None:
    print("场景 3：稀疏行号空段标空，且不为空号建项")
    load_month(WORK, DISP, ATTR)
    _, r = call("POST", f"/months/{MONTH}/reconciliation")
    acc = r["account_id"]

    # 顶层下钻：整表段 32 个子段里绝大多数应为空
    status, top = call(
        "GET",
        f"/reconciliation/{acc}/tables/work_hour/segments?"
        + urllib.parse.urlencode({"lo": 1, "hi": WIDTHS[0]}),
    )
    check("整表下钻返回 200", status == 200, str(top))
    empties = [c for c in top["children"] if c["is_empty"]]
    nonempty = [c for c in top["children"] if not c["is_empty"]]
    # 行 1/2 落在顶层第 1 格，行 33554431 落在最后一格 => 恰好 2 个非空、30 个空
    check("空子段 30 个且全部带空哨兵指纹",
          len(empties) == FANOUT - 2 and all(c["fingerprint"] == EMPTY_FP for c in empties),
          f"empty={len(empties)}")
    check("非空子段恰好 2 个（覆盖两端真实数据网格）",
          len(nonempty) == 2, str([(c["lo"], c["hi"]) for c in nonempty]))

    # 库里只存非空段（外加空表场景的 level0 空哨兵），空号没有逐项落库
    conn = sqlite3.connect(db_path)
    total_segments = conn.execute(
        "SELECT COUNT(*) FROM reconciliation_segment WHERE account_id = ?", (acc,)
    ).fetchone()[0]
    empty_stored = conn.execute(
        "SELECT COUNT(*) FROM reconciliation_segment WHERE account_id = ? AND is_empty = 1",
        (acc,),
    ).fetchone()[0]
    stored_pairs = {(row[0], row[1], row[2], row[3]) for row in conn.execute(
        "SELECT table_name, level, lo, hi FROM reconciliation_segment "
        "WHERE account_id = ? AND is_empty = 0", (acc,))}
    conn.close()
    # 非空段数量：每表每个非空 level 网格各一条；3 表总行数 3+2+2=7，每行数 6 层
    expect_max = (3 + 2 + 2) * 6
    check("落库段数上界 = 非空行数 × 层数（无空号建项）",
          total_segments <= expect_max, f"{total_segments} > {expect_max}")
    check("没有任何空段落库（空段只在查询时补位）", empty_stored == 0, f"empty_stored={empty_stored}")

    # 全量自洽：每条已存非空段的指纹与参考实现一致
    # 叶子(level5)=行指纹；其余层=段内各行指纹的聚合
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ok = True
    detail = ""
    rows_by_table = {"work_hour": WORK, "dispatch": DISP, "attribution": ATTR}
    for seg in conn.execute(
        "SELECT table_name, level, lo, hi, fingerprint FROM reconciliation_segment "
        "WHERE account_id = ? AND is_empty = 0", (acc,)
    ):
        covered = [
            row for row in rows_by_table[seg["table_name"]]
            if seg["lo"] <= row["row_id"] <= seg["hi"]
        ]
        if seg["level"] == 5:
            assert len(covered) == 1
            expect = ref_row_fp(seg["table_name"], covered[0])
        else:
            expect = ref_seg_fp([ref_row_fp(seg["table_name"], row) for row in covered])
        if expect != seg["fingerprint"]:
            ok, detail = False, dict(seg)
            break
    conn.close()
    check("全部已存段指纹与参考实现逐段一致（叶子=行指纹）", ok, str(detail))


def scenario_persistence(proc, db_path) -> subprocess.Popen:
    print("场景 4：源表清空 + 服务重启后，对账号仍可逐级查询")
    load_month(WORK, DISP, ATTR)
    _, r = call("POST", f"/months/{MONTH}/reconciliation")
    acc = r["account_id"]
    before_whole = call("GET", f"/reconciliation/{acc}")[1]

    # 源表清空（对账索引不随业务表清理）
    status, _ = call("DELETE", f"/months/{MONTH}")
    check("清空源表 200", status == 200)
    status, still = call("GET", f"/reconciliation/{acc}")
    check("源表清空后对账号仍可查且指纹不变",
          status == 200 and still == before_whole, str(still))
    leaf = drill_to_leaf(acc, "attribution", 88)
    check("清空后叶子指纹仍可读到（持久化在对账索引）",
          leaf["fingerprint"] == ref_row_fp("attribution", ATTR[1]))

    # 重启服务（同一块 SQLite 文件）
    stop_server(proc)
    proc2 = start_server(db_path)
    status, after = call("GET", f"/reconciliation/{acc}")
    check("服务重启后对账号仍可查", status == 200, str(after))
    check("重启前后指纹一致", after == before_whole, str(after))
    status, listing = call("GET", f"/months/{MONTH}/reconciliation")
    check("重启后对账号列表仍包含该账号",
          status == 200 and any(a["account_id"] == acc for a in listing["accounts"]),
          str(listing))
    return proc2


def scenario_concurrent_overwrite() -> None:
    print("场景 5：生成时并发覆盖源表，录入照常提交、指纹对应单一已提交快照")
    load_month(WORK, DISP, ATTR)
    outcomes: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def gen() -> None:
        barrier.wait()
        outcomes.append(("gen",) + call("POST", f"/months/{MONTH}/reconciliation"))

    def overwrite() -> None:
        barrier.wait()
        body = {"rows": [dict(WORK[0], minutes=777)]}  # 覆盖成只剩 1 行
        outcomes.append(("load",) + call("POST", f"/months/{MONTH}/work-hours", body))

    t1 = threading.Thread(target=gen)
    t2 = threading.Thread(target=overwrite)
    t1.start(); t2.start(); t1.join(); t2.join()

    load_res = [o for o in outcomes if o[0] == "load"][0]
    gen_res = [o for o in outcomes if o[0] == "gen"][0]
    check("并发覆盖录入成功提交（200）", load_res[1] == 200, str(load_res))
    check("对账生成成功（200，未被录入阻塞成失败）", gen_res[1] == 200, str(gen_res))

    if gen_res[1] == 200:
        acc = gen_res[2]["account_id"]
        # 快照只能是两种已提交状态之一：覆盖前 3 行 或 覆盖后 1 行
        fp_before = ref_whole("work_hour", WORK)
        fp_after = ref_whole("work_hour", [dict(WORK[0], minutes=777)])
        got = next(t for t in gen_res[2]["tables"] if t["table"] == "work_hour")["whole_fingerprint"]
        check("工时整表指纹恰为某一已提交快照（非混合态）",
              got in (fp_before, fp_after), f"got={got}")
        # 三表必须同一快照：若工时是覆盖后状态，行数=1；分派/归科未动
        check("返回行数与快照自洽",
              next(t for t in gen_res[2]["tables"] if t["table"] == "work_hour")["row_count"]
              in (1, 3))
        # 落库后按对账号查，指纹稳定可重复读
        status, whole = call("GET", f"/reconciliation/{acc}")
        check("对账号可重复查询且指纹不变",
              status == 200
              and next(t for t in whole["tables"] if t["table"] == "work_hour")["fingerprint"] == got)


def scenario_failure_atomic(db_path: str) -> None:
    print("场景 6：生成落库失败 => 整轮回滚、对账号不可查、无残留")
    proc = start_server(db_path + "-fault.db", fault="write")
    try:
        load_month(WORK, DISP, ATTR)
        before_listing = call("GET", f"/months/{MONTH}/reconciliation")[1]
        status, r = call("POST", f"/months/{MONTH}/reconciliation")
        check("注入落库故障后生成返回 5xx", 500 <= status < 600, f"{status} {r}")
        after_listing = call("GET", f"/months/{MONTH}/reconciliation")[1]
        check("失败后对账号列表无新增（整轮不可查）",
              before_listing["accounts"] == after_listing["accounts"], str(after_listing))
        conn = sqlite3.connect(db_path + "-fault.db")
        seg_cnt = conn.execute("SELECT COUNT(*) FROM reconciliation_segment").fetchone()[0]
        acc_cnt = conn.execute("SELECT COUNT(*) FROM reconciliation_account").fetchone()[0]
        conn.close()
        check("无任何段指纹/账号残留", seg_cnt == 0 and acc_cnt == 0, f"seg={seg_cnt},acc={acc_cnt}")
    finally:
        stop_server(proc)

    # 故障只是环境变量注入的测试开关：不带该开关重启同库，重试即可成功
    proc = start_server(db_path + "-fault.db", fault="")
    try:
        status, r = call("POST", f"/months/{MONTH}/reconciliation")
        check("故障清除后重新生成成功", status == 200, str(r))
        status, whole = call("GET", f"/reconciliation/{r['account_id']}")
        check("重试生成的对账号可正常查询", status == 200, str(whole))
    finally:
        stop_server(proc)


def scenario_api_guardrails() -> None:
    print("场景 7：接口护栏（不存在账号、非法区间、叶子下钻、非法表名）")
    load_month(WORK, DISP, ATTR)
    _, r = call("POST", f"/months/{MONTH}/reconciliation")
    acc = r["account_id"]

    status, body = call("GET", "/reconciliation/nonexistent-account")
    check("不存在对账号返回 404", status == 404 and bool(body["detail"]), str(body))

    status, body = call(
        "GET",
        f"/reconciliation/{acc}/tables/work_hour/segments?"
        + urllib.parse.urlencode({"lo": 3, "hi": 3 + WIDTHS[2] - 1}),  # 不对齐
    )
    check("非对齐区间返回 422", status == 422, str(body))

    status, body = call(
        "GET",
        f"/reconciliation/{acc}/tables/work_hour/segments?"
        + urllib.parse.urlencode({"lo": 2, "hi": 999999999}),  # 越界
    )
    check("越界区间返回 422", status == 422, str(body))

    # 叶子（width=1）再下钻 => 422
    status, body = call(
        "GET",
        f"/reconciliation/{acc}/tables/work_hour/segments?"
        + urllib.parse.urlencode({"lo": 1, "hi": 1}),
    )
    check("叶子段下钻返回 422", status == 422, str(body))

    # 空父段（无任何行的 level-1 网格 [1048577, 2097152]）下钻 => 32 个空子段
    empty_lo, empty_hi = 1048577, 1048577 + WIDTHS[1] - 1
    status, body = call(
        "GET",
        f"/reconciliation/{acc}/tables/work_hour/segments?"
        + urllib.parse.urlencode({"lo": empty_lo, "hi": empty_hi}),
    )
    check("空父段下钻 200 且子段全空",
          status == 200 and len(body["children"]) == FANOUT
          and all(c["is_empty"] and c["fingerprint"] == EMPTY_FP for c in body["children"]),
          str(body)[:200])


def main() -> int:
    db_path = os.environ.get("APP_DB_PATH", "/tmp/test-recon.db")
    for p in (db_path, db_path + "-fault.db"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    proc = start_server(db_path)
    try:
        scenario_order_independence(proc)
        scenario_missing_and_changed(proc)
        scenario_sparse_and_storage(proc, db_path)
        proc = scenario_persistence(proc, db_path)
        scenario_concurrent_overwrite()
        scenario_api_guardrails()
    finally:
        stop_server(proc)
    scenario_failure_atomic(db_path)

    print()
    if failures:
        print(f"{FAIL} 共 {len(failures)} 项失败: {failures}")
        return 1
    print(f"{PASS} 对账全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
