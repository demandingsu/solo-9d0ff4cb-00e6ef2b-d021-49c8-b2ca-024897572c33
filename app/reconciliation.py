"""月度镜像对账：整表指纹 + 固定区间分段指纹。

两端只交换「指纹 + 行号区间」即可核对；对不上的段按对账号逐级查下一级，
直到单行（叶子）即可区分“缺行”与“内容不同”。

确定性分段与编码规则（两端必须照抄，详见 README 第五节）
=========================================================
1. 行号宇宙固定为 [1, 2^25)（即 1..33,554,431），与具体数据无关，因此
   两端即使行号稀疏、缺行，切出来的区间也完全一致。
2. 分段树为固定 32 叉树，共 6 层（level 0..5）：
     level L 段宽 WIDTH[L] = 2^(25 - 5L)
       L=0: 33,554,432（整表只有一个段，即“整表指纹”）
       L=1:  1,048,576（32 段）
       L=2:     32,768
       L=3:      1,024
       L=4:         32
       L=5:          1（叶子=单行）
   段按行号整除对齐：行号 r 所属 level L 段为
       lo = floor((r-1) / WIDTH[L]) * WIDTH[L] + 1,  hi = lo + WIDTH[L] - 1
3. 单行编码（UTF-8，JSON 紧凑数组，键序固定，ensure_ascii=False）：
     work_hour   [row_id, work_order, minutes]
     dispatch    [row_id, work_order, project]
     attribution [row_id, project, budget_subject]
   行指纹 = sha256(行编码).hexdigest()（小写十六进制）。
4. 段指纹：
     空段：sha256(b"recon-empty-v1").hexdigest()（固定哨兵，两端一致）
     叶子段（level 5，宽度 1）：直接取该行的行指纹——两端拿到叶子即可直接
       比对单行内容，区分“缺行”（叶子为空哨兵）与“内容不同”（叶子非空但指纹变了）。
     其余非空段（含整表段 level 0）：
       sha256(b"recon-seg-v1|" + 把段内各行行指纹按其十六进制字符串排序后
       用 b"|" 连接).hexdigest()
   行按 row_id 升序收集、哈希前再排序，录入顺序不影响结果；行号唯一，排序无歧义。
5. 空段不落库（“空段标空，无须为每个空号建项”）：只有非空段写入
   reconciliation_segment；查询子段时按固定网格补出空子段的占位返回。
6. 指纹协议版本固定 "v1"，规则任何一项变化都应升版本。

快照语义
========
生成时录入照常提交：先在一个只读事务里对三张表做一致性快照读，
提交释放读锁后再用 BEGIN IMMEDIATE 一次性写入全部段指纹并登记对账号。
三表指纹必然来自同一个已提交快照；任一步失败则不登记对账号、整轮不可查。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from typing import Any

FINGERPRINT_VERSION = "v1"

# --- 固定分段参数（两端照抄，不得按数据自适应） ---
ROW_ID_BITS = 25
ROW_ID_MAX = (1 << ROW_ID_BITS) - 1  # 行号宇宙上界（含），33,554,431
FANOUT = 32
TREE_DEPTH = ROW_ID_BITS // 5  # 25/5 = 5 次下钻，共 6 层，level 0..5
WIDTHS = [1 << (ROW_ID_BITS - 5 * level) for level in range(TREE_DEPTH + 1)]
# [33554432, 1048576, 32768, 1024, 32, 1]

EMPTY_FINGERPRINT = hashlib.sha256(b"recon-empty-v1").hexdigest()
_SEG_PREFIX = b"recon-seg-v1|"

TABLES = ("work_hour", "dispatch", "attribution")

# 每张表参与行编码的列（顺序即 JSON 数组顺序；row_id 恒为第一个元素）
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "work_hour": ("row_id", "work_order", "minutes"),
    "dispatch": ("row_id", "work_order", "project"),
    "attribution": ("row_id", "project", "budget_subject"),
}


class ReconError(Exception):
    """对账业务错误，携带 HTTP 状态码与中文原因。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# 纯函数：编码与指纹（不依赖数据库，两端可直接照抄）
# ---------------------------------------------------------------------------
def encode_row(table: str, row: dict[str, Any]) -> bytes:
    """单行规范编码：紧凑 JSON 数组、固定列序、UTF-8、不转义非 ASCII。"""
    cols = TABLE_COLUMNS[table]
    payload = [row[c] for c in cols]
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")


def row_fingerprint(table: str, row: dict[str, Any]) -> str:
    return hashlib.sha256(encode_row(table, row)).hexdigest()


def segment_fingerprint(row_hashes: list[str]) -> str:
    """段指纹：空段返回固定哨兵；非空按行指纹字典序聚合。"""
    if not row_hashes:
        return EMPTY_FINGERPRINT
    material = _SEG_PREFIX + b"|".join(h.encode("ascii") for h in sorted(row_hashes))
    return hashlib.sha256(material).hexdigest()


def segment_bounds(row_id: int, level: int) -> tuple[int, int]:
    """行号 row_id 在 level 层所属段的 [lo, hi]（闭区间，两端对齐一致）。"""
    width = WIDTHS[level]
    lo = (row_id - 1) // width * width + 1
    return lo, lo + width - 1


def child_ranges(lo: int, hi: int, level: int) -> list[tuple[int, int]]:
    """父段 [lo,hi]（level 层）在 level+1 层的 32 个固定子区间（闭区间）。"""
    if level >= TREE_DEPTH:
        raise ReconError(422, "叶子段（单行）没有下一级，可直接按行指纹区分缺行/改值")
    child_width = WIDTHS[level + 1]
    return [(start, start + child_width - 1) for start in range(lo, hi + 1, child_width)]


# ---------------------------------------------------------------------------
# 生成：一致性快照 -> 分段指纹 -> 原子落库
# ---------------------------------------------------------------------------
def _snapshot_rows(conn: sqlite3.Connection, month: str) -> dict[str, list[dict[str, Any]]]:
    """单个只读事务内读三张表，保证来自同一已提交快照。"""
    try:
        conn.execute("BEGIN")
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for table in TABLES:
            cols = TABLE_COLUMNS[table]
            sql = (
                f"SELECT {', '.join(cols)} FROM {table} "
                f"WHERE month = ? ORDER BY row_id"
            )
            snapshot[table] = [dict(r) for r in conn.execute(sql, (month,)).fetchall()]
    finally:
        # 只读事务，提交即释放快照读锁；录入事务之后可以照常提交
        conn.execute("COMMIT")
    return snapshot


def _build_segments(
    table: str, rows: list[dict[str, Any]]
) -> tuple[list[tuple[Any, ...]], str, int]:
    """为一张表构建全部非空段记录，返回 (落库行, 整表指纹, 行数)。

    落库行字段：(table_name, level, lo, hi, is_empty, fingerprint)
    只存非空段；空段由查询端按固定网格补位。
    """
    for r in rows:
        if not (1 <= r["row_id"] <= ROW_ID_MAX):
            raise ReconError(
                422,
                f"{table} 行号 {r['row_id']} 超出对账行号宇宙 [1, {ROW_ID_MAX}]，"
                "无法按固定区间分段，请先调整行号",
            )

    # 每行 -> 行指纹，按行号升序收集（rows 已 ORDER BY row_id，这里再兜底排序）
    ordered = sorted(rows, key=lambda r: r["row_id"])
    per_row = [(r["row_id"], row_fingerprint(table, r)) for r in ordered]

    # level -> (lo, hi) -> 该段行指纹列表
    buckets: dict[int, dict[tuple[int, int], list[str]]] = {
        level: {} for level in range(TREE_DEPTH + 1)
    }
    for row_id, fp in per_row:
        for level in range(TREE_DEPTH + 1):
            lo, hi = segment_bounds(row_id, level)
            buckets[level].setdefault((lo, hi), []).append(fp)

    records: list[tuple[Any, ...]] = []
    for level in range(TREE_DEPTH + 1):
        for (lo, hi), fps in sorted(buckets[level].items()):
            if level == TREE_DEPTH:
                # 叶子段宽度为 1、至多一行：叶子指纹直接取行指纹，
                # 两端拿到叶子即可直接比对单行内容，区分缺行/改值。
                assert len(fps) == 1 and lo == hi
                records.append((table, level, lo, hi, 0, fps[0]))
            else:
                records.append((table, level, lo, hi, 0, segment_fingerprint(fps)))

    # 整表指纹：level 0 唯一整表段；空表则为固定空哨兵
    if per_row:
        whole_fp = segment_fingerprint([fp for _, fp in per_row])
    else:
        whole_fp = EMPTY_FINGERPRINT
        # 空表没有非空段，但整表段需可查：登记一条空的整表段
        records.append((table, 0, 1, WIDTHS[0], 1, EMPTY_FINGERPRINT))
    return records, whole_fp, len(per_row)


def generate_reconciliation(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    """生成月度对账号：三表整表指纹 + 全量非空子段，原子可查。"""
    account_id = f"{month}-{secrets.token_hex(8)}"

    # 阶段 1：一致性快照读（短只读事务，不阻塞录入提交）
    try:
        snapshot = _snapshot_rows(conn, month)
    except sqlite3.OperationalError as exc:
        raise ReconError(500, f"读取已提交快照失败，本轮未生成任何对账数据: {exc}")

    # 阶段 2：在 Python 侧构建全部段（纯计算，不持锁）
    all_records: list[tuple[Any, ...]] = []
    table_summary: list[dict[str, Any]] = []
    for table in TABLES:
        records, whole_fp, row_count = _build_segments(table, snapshot[table])
        all_records.extend(records)
        table_summary.append(
            {
                "table": table,
                "whole_fingerprint": whole_fp,
                "row_count": row_count,
                "range_lo": 1,
                "range_hi": WIDTHS[0],
            }
        )

    # 阶段 3：原子写入。BEGIN IMMEDIATE 与录入写事务互斥，
    # 但快照早已取得，锁等待不影响“三表同一快照”；失败则整轮回滚、对账号不可查。
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 测试故障注入：RECON_FAULT=write 时在写中途抛错，验证整轮回滚语义。
        # 默认不设置该环境变量，生产路径不受影响。
        if os.getenv("RECON_FAULT") == "write":
            raise RuntimeError("injected reconciliation write failure")
        conn.execute(
            "INSERT INTO reconciliation_account (account_id, month) VALUES (?, ?)",
            (account_id, month),
        )
        conn.executemany(
            "INSERT INTO reconciliation_segment "
            "(account_id, table_name, level, lo, hi, is_empty, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(account_id, *rec) for rec in all_records],
        )
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        _rollback_if_open(conn)
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            raise ReconError(503, "数据库正被录入/纠错事务占用，请重试；本轮未生成任何对账数据")
        raise ReconError(500, f"对账数据落库失败，已回滚，整轮不可查: {exc}")
    except Exception as exc:
        _rollback_if_open(conn)
        raise ReconError(500, f"对账生成失败，已回滚，整轮不可查: {exc}")

    return {
        "account_id": account_id,
        "month": month,
        "fingerprint_version": FINGERPRINT_VERSION,
        "fanout": FANOUT,
        "levels": TREE_DEPTH + 1,
        "row_id_min": 1,
        "row_id_max": ROW_ID_MAX,
        "tables": table_summary,
    }


# ---------------------------------------------------------------------------
# 查询：逐级读已存子段，空段按固定网格补位
# ---------------------------------------------------------------------------
def _require_account(conn: sqlite3.Connection, account_id: str) -> str:
    row = conn.execute(
        "SELECT month FROM reconciliation_account WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise ReconError(404, f"对账号 {account_id} 不存在或生成失败（整轮不可查）")
    return row["month"]


def _require_table(table: str) -> None:
    if table not in TABLE_COLUMNS:
        raise ReconError(
            422, f"表名必须是 {TABLES} 之一，收到 {table!r}"
        )


def get_whole(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """对账号总览：三表整表指纹（level 0 段）。"""
    month = _require_account(conn, account_id)
    tables_out: list[dict[str, Any]] = []
    for table in TABLES:
        rec = conn.execute(
            "SELECT lo, hi, is_empty, fingerprint FROM reconciliation_segment "
            "WHERE account_id = ? AND table_name = ? AND level = 0",
            (account_id, table),
        ).fetchone()
        # 生成时每表必写 level 0（空表写空哨兵），查不到只可能是数据被外部破坏
        if rec is None:  # pragma: no cover - 防御
            raise ReconError(500, f"对账号 {account_id} 的 {table} 整表段缺失，索引不完整")
        tables_out.append(
            {
                "table": table,
                "level": 0,
                "lo": rec["lo"],
                "hi": rec["hi"],
                "is_empty": bool(rec["is_empty"]),
                "fingerprint": rec["fingerprint"],
            }
        )
    return {
        "account_id": account_id,
        "month": month,
        "fingerprint_version": FINGERPRINT_VERSION,
        "tables": tables_out,
    }


def get_children(
    conn: sqlite3.Connection,
    account_id: str,
    table: str,
    lo: int,
    hi: int,
) -> dict[str, Any]:
    """查某段的下一级：直接读已存子段指纹，空段按固定网格补空哨兵。"""
    month = _require_account(conn, account_id)
    _require_table(table)

    # 校验区间：必须是某层合法的对齐段
    parent_level = _locate_level(lo, hi)
    if parent_level is None:
        raise ReconError(
            422,
            f"区间 [{lo}, {hi}] 不是合法对齐段；合法段宽为 {WIDTHS}，"
            "且 lo 必须满足 (lo-1) 能被段宽整除",
        )
    if parent_level == TREE_DEPTH:
        raise ReconError(422, "叶子段宽度为 1（单行），没有下一级；单行指纹即最终区分依据")

    # 父段已落库 => 查它的已存子段；父段未落库 => 它是空段，子段必然全空。
    parent_exists = conn.execute(
        "SELECT 1 FROM reconciliation_segment "
        "WHERE account_id = ? AND table_name = ? AND level = ? AND lo = ? AND hi = ?",
        (account_id, table, parent_level, lo, hi),
    ).fetchone()
    if parent_exists is None:
        # 父段是空段（未落库）。空段的子段必然全空，直接按网格补 32 个空子段。
        stored: dict[tuple[int, int], sqlite3.Row] = {}
    else:
        child_level = parent_level + 1
        rows = conn.execute(
            "SELECT lo, hi, is_empty, fingerprint FROM reconciliation_segment "
            "WHERE account_id = ? AND table_name = ? AND level = ? AND lo >= ? AND hi <= ? "
            "ORDER BY lo",
            (account_id, table, child_level, lo, hi),
        ).fetchall()
        stored = {(r["lo"], r["hi"]): r for r in rows}

    children: list[dict[str, Any]] = []
    for child_lo, child_hi in child_ranges(lo, hi, parent_level):
        rec = stored.get((child_lo, child_hi))
        if rec is None:
            children.append(
                {
                    "level": parent_level + 1,
                    "lo": child_lo,
                    "hi": child_hi,
                    "is_empty": True,
                    "fingerprint": EMPTY_FINGERPRINT,
                }
            )
        else:
            children.append(
                {
                    "level": parent_level + 1,
                    "lo": rec["lo"],
                    "hi": rec["hi"],
                    "is_empty": bool(rec["is_empty"]),
                    "fingerprint": rec["fingerprint"],
                }
            )

    return {
        "account_id": account_id,
        "month": month,
        "table": table,
        "parent": {"level": parent_level, "lo": lo, "hi": hi},
        "fingerprint_version": FINGERPRINT_VERSION,
        "children": children,
    }


def _locate_level(lo: int, hi: int) -> int | None:
    """根据 [lo,hi] 反推它属于哪一层；不合法返回 None。"""
    if not (1 <= lo <= hi <= WIDTHS[0]):
        return None
    width = hi - lo + 1
    for level, w in enumerate(WIDTHS):
        if w == width:
            if (lo - 1) % width != 0:
                return None
            return level
    return None


def list_accounts(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    """列出某月已成功生成、仍可查的对账号。"""
    rows = conn.execute(
        "SELECT account_id, month, created_at FROM reconciliation_account "
        "WHERE month = ? ORDER BY created_at DESC, account_id DESC",
        (month,),
    ).fetchall()
    return {"month": month, "accounts": [dict(r) for r in rows]}


def _rollback_if_open(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass
