"""核心业务逻辑：导入批次、封账归档、三角回查。

并发与原子性约定：
- 所有写操作（设置待结月 / 导入批次 / 封账）先抢进程内 WRITE_LOCK，再在
  BEGIN IMMEDIATE 单事务内完成；SQLite 同时串行化写者。导入与封账重叠时，
  谁先提交谁先进库：批次按提交时的待结月计入本期或下期，失败整体回滚，
  不留部分结果。
"""
import csv
import io
import re
import threading

from .months import month_range, next_month, parse_month

WRITE_LOCK = threading.Lock()

MAX_AMOUNT_CENTS = 999_999_999_999_999  # 15 位以内，避免 SUM 溢出 int64
CLAIM_NO_RE = re.compile(r"^[^\s,]{1,64}$")
AMOUNT_RE = re.compile(r"^\d{1,15}$")

HEADER_ALIASES = {
    "claim_no": {"claim_no", "claimno", "claim", "claim_id", "赔款号", "赔案号"},
    "accident_month": {"accident_month", "accident", "accidentmonth", "事故月"},
    "payment_month": {"payment_month", "payment", "paymentmonth", "支付月"},
    "amount_cents": {"amount_cents", "amount", "amountcent", "金额", "金额分", "金额(分)", "金额（分）"},
}


class BatchError(Exception):
    """整批校验/写入失败，errors 为行级错误列表；不会产生部分结果。"""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors[:5]))


class CloseError(Exception):
    """封账请求非法（如只能封当前待结月）。"""


# ---------------------------------------------------------------- 待结月

def get_pending(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'pending_month'").fetchone()
    return row["value"] if row else None


def _set_pending(conn, month):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('pending_month', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (month,),
    )


def has_any_data(conn):
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM batches) + (SELECT COUNT(*) FROM closes) AS n"
    ).fetchone()
    return row["n"] > 0


def set_first_pending(conn, month):
    """设置首个待结月；已存在任何批次或封账数据后禁止修改。调用方须持有 WRITE_LOCK。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if has_any_data(conn):
            raise CloseError("已存在批次或封账数据，不能再修改待结月")
        _set_pending(conn, month)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- CSV 解析校验

def parse_csv_text(text, pending):
    """解析并整批校验 CSV 文本，返回行列表；任何错误抛 BatchError，不产生部分结果。

    pending 为当前待结月，用于校验 支付月 ∈ [事故月, 待结月]。
    """
    errors, rows, seen = [], [], {}
    header = col_map = None
    reader = csv.reader(io.StringIO(text))
    for lineno, raw in enumerate(reader, start=1):
        if not raw or all(not c.strip() for c in raw):
            continue
        if header is None:
            header = [c.strip().lstrip("\ufeff").lower() for c in raw]
            col_map = {}
            for idx, name in enumerate(header):
                field = next((f for f, aliases in HEADER_ALIASES.items() if name in aliases), None)
                if field is None:
                    errors.append(f"表头第 {idx + 1} 列无法识别：{name!r}")
                elif field in col_map.values():
                    errors.append(f"表头列重复：{name!r}")
                else:
                    col_map[idx] = field
            missing = sorted(set(HEADER_ALIASES) - set(col_map.values()))
            if missing:
                errors.append("缺少必需列：" + ", ".join(missing))
            if errors:
                raise BatchError(errors)
            continue
        if len(raw) != len(header):
            errors.append(f"第 {lineno} 行：列数 {len(raw)} 与表头 {len(header)} 不一致")
            continue
        rec = {col_map[i]: raw[i].strip() for i in range(len(header))}
        claim_no = rec["claim_no"]
        no_ok = bool(CLAIM_NO_RE.match(claim_no))
        if not no_ok:
            errors.append(f"第 {lineno} 行：赔款号为空或含空格/逗号等非法字符：{claim_no!r}")
        elif claim_no in seen:
            errors.append(f"第 {lineno} 行：赔款号 {claim_no} 与本批第 {seen[claim_no]} 行重复（每批赔款号须唯一）")
        else:
            seen[claim_no] = lineno
        acc = parse_month(rec["accident_month"])
        if acc is None:
            errors.append(f"第 {lineno} 行：事故月格式应为 YYYY-MM：{rec['accident_month']!r}")
        pay = parse_month(rec["payment_month"])
        if pay is None:
            errors.append(f"第 {lineno} 行：支付月格式应为 YYYY-MM：{rec['payment_month']!r}")
        amt = None
        if not AMOUNT_RE.match(rec["amount_cents"]):
            errors.append(f"第 {lineno} 行：金额须为非负整数（分）：{rec['amount_cents']!r}")
        else:
            amt = int(rec["amount_cents"])
            if amt > MAX_AMOUNT_CENTS:
                errors.append(f"第 {lineno} 行：金额超出上限 {MAX_AMOUNT_CENTS} 分")
        if acc and pay:
            if pay < acc:
                errors.append(f"第 {lineno} 行：支付月 {pay} 早于事故月 {acc}")
            elif pending and pay > pending:
                errors.append(f"第 {lineno} 行：支付月 {pay} 晚于当前待结月 {pending}")
        if no_ok and acc and pay and amt is not None:
            rows.append({"claim_no": claim_no, "accident_month": acc,
                         "payment_month": pay, "amount_cents": amt})
        if len(errors) > 100:
            errors.append("错误过多，其余已截断")
            break
    if header is None:
        errors.append("CSV 为空或缺少表头")
    if errors:
        raise BatchError(errors)
    if not rows:
        raise BatchError(["没有任何数据行"])
    return rows


def apply_batch(conn, rows):
    """整批写入（同号整笔覆盖）。调用方须持有 WRITE_LOCK。失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        pend = get_pending(conn)
        if pend is None:
            raise BatchError(["尚未设置待结月"])
        # 事务内按最新待结月复核（导入与封账可能交错）
        for r in rows:
            if r["payment_month"] > pend:
                raise BatchError([
                    f"支付月 {r['payment_month']} 晚于当前待结月 {pend}（期间封账已推进，请重新提交）"
                ])
        total = sum(r["amount_cents"] for r in rows)
        cur = conn.execute(
            "INSERT INTO batches(period_month, row_count, total_cents, overwritten) VALUES(?,?,?,0)",
            (pend, len(rows), total),
        )
        batch_id = cur.lastrowid
        overwritten = 0
        for r in rows:
            if conn.execute("SELECT 1 FROM claims WHERE claim_no = ?", (r["claim_no"],)).fetchone():
                overwritten += 1
            conn.execute(
                "INSERT INTO claims(claim_no, accident_month, payment_month, amount_cents, batch_id)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(claim_no) DO UPDATE SET"
                "   accident_month = excluded.accident_month,"
                "   payment_month  = excluded.payment_month,"
                "   amount_cents   = excluded.amount_cents,"
                "   batch_id       = excluded.batch_id",
                (r["claim_no"], r["accident_month"], r["payment_month"], r["amount_cents"], batch_id),
            )
        conn.execute("UPDATE batches SET overwritten = ? WHERE id = ?", (overwritten, batch_id))
        conn.commit()
        return {"batch_id": batch_id, "period_month": pend, "row_count": len(rows),
                "total_cents": total, "overwritten": overwritten}
    except Exception:
        conn.rollback()
        raise


def rows_iter(rows):
    return iter(rows)


# ---------------------------------------------------------------- 封账

def _period_deltas(conn, pend):
    """本期（待结月 = pend）各 (事故月, 支付月) 组合的净变动。

    对本期批次触及的每笔赔案：减去其归档快照（如有），加上当前值。
    返回 (触及赔案行, {组合: 净变动})，净变动为 0 的组合已被剔除。
    """
    touched = conn.execute(
        "SELECT c.claim_no, c.accident_month, c.payment_month, c.amount_cents,"
        "       a.accident_month AS p_acc, a.payment_month AS p_pay, a.amount_cents AS p_amt"
        " FROM claims c"
        " JOIN batches b ON b.id = c.batch_id"
        " LEFT JOIN claim_archived a ON a.claim_no = c.claim_no"
        " WHERE b.period_month = ?",
        (pend,),
    ).fetchall()
    deltas = {}
    for t in touched:
        if t["p_amt"] is not None:
            key = (t["p_acc"], t["p_pay"])
            deltas[key] = deltas.get(key, 0) - t["p_amt"]
        key = (t["accident_month"], t["payment_month"])
        deltas[key] = deltas.get(key, 0) + t["amount_cents"]
    return touched, {k: v for k, v in deltas.items() if v != 0}


def close_preview(conn, pend):
    touched, deltas = _period_deltas(conn, pend)
    batch_count = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE period_month = ?", (pend,)
    ).fetchone()["c"]
    items = [(acc, pay, f"{net:+,}") for (acc, pay), net in sorted(deltas.items())]
    return {"batch_count": batch_count, "claim_count": len(touched), "combos": items}


def close_pending(conn, month):
    """封当前待结月并推进到次月。调用方须持有 WRITE_LOCK。失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        pend = get_pending(conn)
        if pend is None:
            raise CloseError("尚未设置待结月")
        if month != pend:
            raise CloseError(f"只能封当前待结月 {pend}，不能封 {month}")
        touched, deltas = _period_deltas(conn, pend)
        for (acc, pay), net in sorted(deltas.items()):
            conn.execute(
                "INSERT INTO archive(close_month, accident_month, payment_month, net_cents)"
                " VALUES(?,?,?,?)",
                (pend, acc, pay, net),
            )
        for t in touched:
            conn.execute(
                "INSERT INTO claim_archived(claim_no, accident_month, payment_month, amount_cents)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(claim_no) DO UPDATE SET"
                "   accident_month = excluded.accident_month,"
                "   payment_month  = excluded.payment_month,"
                "   amount_cents   = excluded.amount_cents",
                (t["claim_no"], t["accident_month"], t["payment_month"], t["amount_cents"]),
            )
        batch_count = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE period_month = ?", (pend,)
        ).fetchone()["c"]
        conn.execute(
            "INSERT INTO closes(close_month, batch_count, combo_count) VALUES(?,?,?)",
            (pend, batch_count, len(deltas)),
        )
        _set_pending(conn, next_month(pend))
        conn.commit()
        return {"closed": pend, "next": next_month(pend), "batch_count": batch_count,
                "combo_count": len(deltas), "claim_count": len(touched)}
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 回查（只读归档汇总）

def closed_months(conn):
    return [r["close_month"] for r in
            conn.execute("SELECT close_month FROM closes ORDER BY close_month")]


def archive_detail(conn, close_month):
    return conn.execute(
        "SELECT accident_month, payment_month, net_cents FROM archive"
        " WHERE close_month = ? ORDER BY accident_month, payment_month",
        (close_month,),
    ).fetchall()


def build_triangle(conn, start, close_month):
    """按 close_month 封账时的口径重建累计赔付三角（只读 archive 汇总表）。

    行：起始事故月 start 至 close_month 的每个事故月；
    列：start 至最新已结月。每行从事故当月起逐月累计；
    事故月之前为 None（不适用），封账月之后留空。
    """
    rows = conn.execute(
        "SELECT accident_month, payment_month, SUM(net_cents) AS s"
        " FROM archive"
        " WHERE close_month <= ? AND accident_month >= ? AND accident_month <= ?"
        " GROUP BY accident_month, payment_month",
        (close_month, start, close_month),
    ).fetchall()
    cells = {(r["accident_month"], r["payment_month"]): r["s"] for r in rows}
    latest = conn.execute("SELECT MAX(close_month) AS m FROM closes").fetchone()["m"]
    columns = month_range(start, latest)
    table = []
    for acc in month_range(start, close_month):
        cum = 0
        out = []
        for col in columns:
            if col < acc:
                out.append({"kind": "na"})
            elif col <= close_month:
                cum += cells.get((acc, col), 0)
                out.append({"kind": "val", "text": f"{cum:,}"})
            else:
                out.append({"kind": "blank"})
        table.append({"accident": acc, "cells": out})
    return {"columns": columns, "rows": table, "start": start, "close": close_month}
