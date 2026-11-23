"""页面路由：首页 / 设置待结月 / 导入赔款 / 封账 / 三角回查 / 归档明细。"""
from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, url_for)

from . import logic
from .db import get_db
from .months import month_range, parse_month

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    conn = get_db()
    pending = logic.get_pending(conn)
    closes = conn.execute(
        "SELECT * FROM closes ORDER BY close_month DESC").fetchall()
    batches = conn.execute(
        "SELECT * FROM batches ORDER BY id DESC LIMIT 10").fetchall()
    stat = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS s FROM claims"
    ).fetchone()
    return render_template("index.html", pending=pending, closes=closes,
                           batches=batches, stat=stat)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    conn = get_db()
    locked = logic.has_any_data(conn)
    if request.method == "POST":
        if locked:
            flash("已存在批次或封账数据，不能再修改待结月", "error")
            return redirect(url_for("main.index"))
        month = parse_month(request.form.get("pending_month", ""))
        if month is None:
            flash("月份格式应为 YYYY-MM（如 2026-01）", "error")
            return redirect(url_for("main.setup"))
        with logic.WRITE_LOCK:
            logic.set_first_pending(conn, month)
        flash(f"首个待结月已设为 {month}", "ok")
        return redirect(url_for("main.index"))
    return render_template("setup.html", pending=logic.get_pending(conn), locked=locked)


@bp.route("/import", methods=["GET", "POST"])
def import_csv():
    conn = get_db()
    pending = logic.get_pending(conn)
    if pending is None:
        flash("请先设置首个待结月", "error")
        return redirect(url_for("main.setup"))
    if request.method == "POST":
        upload = request.files.get("file")
        if upload is not None and upload.filename:
            text = upload.read().decode("utf-8-sig", errors="replace")
        else:
            text = request.form.get("csv_text", "")
        if not text.strip():
            flash("请上传 CSV 文件或粘贴 CSV 内容", "error")
            return redirect(url_for("main.import_csv"))
        try:
            rows = logic.parse_csv_text(text, pending)
        except logic.BatchError as exc:
            return render_template("import.html", pending=pending,
                                   errors=exc.errors, result=None), 400
        try:
            with logic.WRITE_LOCK:
                result = logic.apply_batch(conn, rows)
        except logic.BatchError as exc:
            return render_template("import.html", pending=logic.get_pending(conn),
                                   errors=exc.errors, result=None), 409
        flash(f"批次 #{result['batch_id']} 导入成功：{result['row_count']} 笔，"
              f"计入 {result['period_month']} 期（其中覆盖重报 {result['overwritten']} 笔）", "ok")
        return render_template("import.html", pending=logic.get_pending(conn),
                               errors=None, result=result)
    return render_template("import.html", pending=pending, errors=None, result=None)


@bp.route("/close", methods=["GET", "POST"])
def close():
    conn = get_db()
    pending = logic.get_pending(conn)
    if pending is None:
        flash("请先设置首个待结月", "error")
        return redirect(url_for("main.setup"))
    if request.method == "POST":
        month = parse_month(request.form.get("close_month", ""))
        if month is None:
            flash("月份格式应为 YYYY-MM", "error")
            return redirect(url_for("main.close"))
        try:
            with logic.WRITE_LOCK:
                result = logic.close_pending(conn, month)
        except logic.CloseError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.close"))
        flash(f"已封账 {result['closed']}：本期 {result['batch_count']} 批、"
              f"{result['claim_count']} 笔变动，归档 {result['combo_count']} 个组合；"
              f"待结月推进至 {result['next']}", "ok")
        return redirect(url_for("main.index"))
    preview = logic.close_preview(conn, pending)
    return render_template("close.html", pending=pending, preview=preview)


@bp.route("/triangle")
def triangle():
    conn = get_db()
    closes = logic.closed_months(conn)
    if not closes:
        flash("尚无已结月份，请先导入赔款并封账", "error")
        return redirect(url_for("main.index"))
    close_m = parse_month(request.args.get("close", ""))
    if close_m not in closes:
        close_m = closes[-1]
    start = parse_month(request.args.get("start", ""))
    if start is None or start > close_m:
        start = closes[0]
    first_acc = conn.execute("SELECT MIN(accident_month) AS m FROM archive").fetchone()["m"]
    start_options = month_range(first_acc or closes[0], closes[-1])
    data = logic.build_triangle(conn, start, close_m)
    return render_template("triangle.html", closes=closes, start_options=start_options,
                           start=start, close_m=close_m, data=data)


@bp.route("/archive/<close_month>")
def archive(close_month):
    conn = get_db()
    if close_month not in logic.closed_months(conn):
        abort(404)
    rows = logic.archive_detail(conn, close_month)
    total = sum(r["net_cents"] for r in rows)
    return render_template("archive.html", close_month=close_month,
                           rows=rows, total=total)
