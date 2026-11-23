#!/usr/bin/env python3
"""关键操作自测（不依赖 pytest）：设置待结月、CSV 校验、封账推进、同号覆盖、
补报净变动归档、两期回查一致性、零额显示、失败原子性、导入/封账并发。

运行：python3 tests/selftest.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_CSV = os.path.join(REPO, "demo", "claims_2026-01_base.csv")
SUPP_CSV = os.path.join(REPO, "demo", "claims_2026-02_supplement.csv")

_passed, _failed = [], []


def check(name, cond, detail=""):
    (_passed if cond else _failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"  -> {detail}"))


def make_client():
    tmp = tempfile.mkdtemp(prefix="lt-test-")
    app = create_app({"DB_PATH": os.path.join(tmp, "t.db"), "TESTING": True})
    return app, app.test_client(), app.config["DB_PATH"]


def q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def setup_pending(client, month="2026-01"):
    return client.post("/setup", data={"pending_month": month}, follow_redirects=True)


def import_text(client, text):
    return client.post("/import", data={"csv_text": text})


def close_month(client, month):
    return client.post("/close", data={"close_month": month}, follow_redirects=True)


HEADER = "claim_no,accident_month,payment_month,amount_cents\n"


# ---------------------------------------------------------------- 1. 全流程 + 两期回查
def test_full_flow_two_periods():
    print("\n[1] 完整流程：基础批 -> 封账 -> 补报/更正 -> 封账 -> 两期回查")
    app, client, db = make_client()
    r = setup_pending(client)
    check("设置首个待结月 2026-01", "首个待结月已设为 2026-01" in r.get_data(as_text=True))

    r = import_text(client, open(BASE_CSV, encoding="utf-8").read())
    check("基础批导入成功", r.status_code == 200 and "批次 #1" in r.get_data(as_text=True),
          f"status={r.status_code}")
    check("基础批计入 2026-01 期", "计入 2026-01 期" in r.get_data(as_text=True))

    r = close_month(client, "2026-02")
    check("封非待结月被拒绝", "只能封当前待结月 2026-01" in r.get_data(as_text=True))
    check("拒绝后待结月仍为 2026-01",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-01")

    r = close_month(client, "2026-01")
    check("封 2026-01 成功并推进", "已封账 2026-01" in r.get_data(as_text=True)
          and "2026-02" in r.get_data(as_text=True))
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-01'")
    check("2026-01 期归档 7 个组合", len(rows) == 7, f"got {len(rows)}")

    r = import_text(client, open(SUPP_CSV, encoding="utf-8").read())
    body = r.get_data(as_text=True)
    check("补报批计入 2026-02 期", "计入 2026-02 期" in body)
    check("补报批覆盖重报 3 笔", "覆盖重报 3 笔" in body)

    r = close_month(client, "2026-02")
    check("封 2026-02 成功", "已封账 2026-02" in r.get_data(as_text=True))
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    expected = {
        ("2025-10", "2025-12"): 15000,    # C-0002 更正金额 80000 -> 95000
        ("2025-11", "2026-01"): -60000,   # C-0004 支付月 2026-01 -> 2026-02
        ("2025-11", "2026-02"): 105000,   # C-0004 移入 60000 + C-0008 补报 45000
        ("2025-12", "2026-02"): 70000,    # C-0009 补报
        ("2026-01", "2026-02"): 110000,   # C-0010 补报
        ("2026-02", "2026-02"): 180000,   # C-0011 新事故月
    }
    check("2026-02 期仅归档 6 个净变动组合（同数重报 C-0007 不归档）", nets == expected,
          f"got {nets}")

    t1 = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("一期三角含旧值 200,000 / 210,000 / 290,000",
          all(s in t1 for s in ("200,000", "210,000", "290,000")))
    check("一期三角不含补报后的 215,000", "215,000" not in t1)
    check("一期三角封账月之后留空（4 个空白格）",
          t1.count('<td class="blank"></td>') == 4, f"got {t1.count('blank')}")

    t2 = client.get("/triangle?start=2025-10&close=2026-02").get_data(as_text=True)
    check("二期三角含更正后 215,000 / 255,000 / 360,000 / 180,000",
          all(s in t2 for s in ("215,000", "255,000", "360,000", "180,000")))
    check("二期三角无空白格（已结到最新月）", '<td class="blank"></td>' not in t2)

    t1b = client.get("/triangle?start=2025-10&close=2026-01").get_data(as_text=True)
    check("两期回查：一期三角在二期封账后保持原样", t1b == t1)

    a2 = client.get("/archive/2026-02").get_data(as_text=True)
    check("归档明细页展示 2026-02 期净变动", "2025-11" in a2 and "-60,000" in a2)


# ---------------------------------------------------------------- 2. CSV 校验
def test_import_validation():
    print("\n[2] CSV 校验：非法行整批拒绝")
    app, client, db = make_client()
    setup_pending(client)
    cases = {
        "负数金额": "C1,2025-12,2026-01,-100",
        "小数金额": "C1,2025-12,2026-01,10.5",
        "非法月份": "C1,2025-13,2026-01,100",
        "支付月早于事故月": "C1,2026-01,2025-12,100",
        "支付月晚于待结月": "C1,2025-12,2026-02,100",
        "批内赔款号重复": "C1,2025-12,2026-01,100\nC1,2025-12,2026-01,200",
        "赔款号含逗号": '"C,1",2025-12,2026-01,100',
    }
    for name, line in cases.items():
        r = import_text(client, HEADER + line + "\n")
        check(f"拒绝：{name}", r.status_code == 400, f"status={r.status_code}")
    r = import_text(client, "claim_no,accident_month,payment_month,amount_cents,extra\nC1,2025-12,2026-01,100,x\n")
    check("拒绝：无法识别的表头列", r.status_code == 400)
    r = import_text(client, HEADER + "C1,2025-12,2026-01\n")
    check("拒绝：列数不一致", r.status_code == 400)
    r = import_text(client, HEADER)
    check("拒绝：无数据行", r.status_code == 400)
    check("全部拒绝后 claims/batches 仍为空（原子性）",
          q(db, "SELECT COUNT(*) FROM claims")[0][0] == 0
          and q(db, "SELECT COUNT(*) FROM batches")[0][0] == 0)


# ---------------------------------------------------------------- 3. 覆盖语义 + 零额显示
def test_overwrite_and_zero():
    print("\n[3] 同号覆盖（改事故月/支付月）与零额显示")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "A1,2025-12,2026-01,10000\nA2,2026-01,2026-01,5000\n")
    # 同期内重报 A1：改事故月 2025-12 -> 2026-01，金额 10000 -> 30000
    import_text(client, HEADER + "A1,2026-01,2026-01,30000\n")
    close_month(client, "2026-01")
    rows = q(db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-01'")
    check("封前同期覆盖：只按最新值归档 (2026-01,2026-01)=35000",
          rows == [("2026-01", "2026-01", 35000)], f"got {rows}")

    # 下期更正 A1 支付月 2026-01 -> 2026-02；A3 正常补报
    import_text(client, HEADER + "A1,2026-01,2026-02,30000\nA3,2026-01,2026-02,4000\n")
    close_month(client, "2026-02")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("跨期更正支付月：旧组合 -30000，新组合 +34000",
          nets == {("2026-01", "2026-01"): -30000, ("2026-01", "2026-02"): 34000},
          f"got {nets}")

    # A5 事故 2026-02、支付 2026-03：事故当月无赔付 -> 零额显示 0
    import_text(client, HEADER + "A5,2026-02,2026-03,9000\n")
    close_month(client, "2026-03")
    t = client.get("/triangle?start=2026-01&close=2026-03").get_data(as_text=True)
    check("已到月份零额显示 0", '<td class="num">0</td>' in t)
    check("累计值 5,000 / 39,000 / 9,000 正确",
          all(s in t for s in ("5,000", "39,000", "9,000")))


# ---------------------------------------------------------------- 4. 封账约束
def test_close_constraints():
    print("\n[4] 封账约束：只能封当前待结月，成功后进入次月")
    app, client, db = make_client()
    r = client.get("/close", follow_redirects=True)
    check("未设待结月时封账页引导设置", "请先设置首个待结月" in r.get_data(as_text=True))
    setup_pending(client)
    r = close_month(client, "2025-12")
    check("封过去月份被拒绝", "只能封当前待结月 2026-01" in r.get_data(as_text=True))
    r = close_month(client, "2026-01")
    check("空期间也可封账（0 组合）", "已封账 2026-01" in r.get_data(as_text=True))
    r = close_month(client, "2026-01")
    check("重复封同一月被拒绝", "只能封当前待结月 2026-02" in r.get_data(as_text=True))
    check("closes 表仅一条记录", len(q(db, "SELECT * FROM closes")) == 1)
    r = client.get("/triangle")
    check("空归档期三角页可访问", r.status_code == 200)


# ---------------------------------------------------------------- 5. 失败原子性
def test_batch_atomicity():
    print("\n[5] 整批原子性：部分行非法则整批不留痕")
    app, client, db = make_client()
    setup_pending(client)
    csv_text = HEADER + "G1,2025-12,2026-01,100\nG2,2025-12,2026-01,200\nG3,2026-02,2026-01,300\n"
    r = import_text(client, csv_text)
    check("含非法行的批次被拒绝", r.status_code == 400)
    check("前两行有效也未写入",
          q(db, "SELECT COUNT(*) FROM claims")[0][0] == 0
          and q(db, "SELECT COUNT(*) FROM batches")[0][0] == 0)


# ---------------------------------------------------------------- 6. 待结月锁定
def test_setup_locked_after_data():
    print("\n[6] 有数据后禁止改待结月")
    app, client, db = make_client()
    setup_pending(client, "2026-01")
    import_text(client, HEADER + "L1,2025-12,2026-01,100\n")
    r = setup_pending(client, "2026-03")
    check("修改被拒绝", "不能再修改待结月" in r.get_data(as_text=True))
    check("待结月保持 2026-01",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-01")


# ---------------------------------------------------------------- 7. 导入/封账并发
def test_concurrent_import_and_close():
    print("\n[7] 并发：导入与封账重叠时整批计入本期或下期")
    app, client, db = make_client()
    setup_pending(client)
    barrier = threading.Barrier(4)
    results, errors = [], []

    def worker(i):
        c = app.test_client()
        csv_text = HEADER + f"T{i},2025-12,2026-01,{1000 * (i + 1)}\n"
        try:
            barrier.wait(timeout=10)
            time.sleep(0.01 * i)
            r = c.post("/import", data={"csv_text": csv_text})
            results.append((i, r.status_code))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"worker{i}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    barrier.wait(timeout=10)
    time.sleep(0.02)  # 让部分导入先提交，制造重叠
    r1 = close_month(client, "2026-01")
    for t in threads:
        t.join()
    r2 = close_month(client, "2026-02")

    check("并发期间无异常", not errors, "; ".join(errors))
    check("3 个批次全部成功", sorted(s for _, s in results) == [200, 200, 200],
          f"got {results}")
    check("两次封账均成功", "已封账 2026-01" in r1.get_data(as_text=True)
          and "已封账 2026-02" in r2.get_data(as_text=True))
    periods = [row[0] for row in q(db, "SELECT period_month FROM batches")]
    check("每批计入 2026-01 或 2026-02", set(periods) <= {"2026-01", "2026-02"}
          and len(periods) == 3, f"got {periods}")
    total_claims = q(db, "SELECT SUM(amount_cents) FROM claims")[0][0]
    total_archived = q(db, "SELECT SUM(net_cents) FROM archive")[0][0]
    check("两期归档净额合计 == 赔款合计（无丢失无重复）",
          total_archived == total_claims == 6000,
          f"archived={total_archived} claims={total_claims}")


def main():
    for fn in (test_full_flow_two_periods, test_import_validation,
               test_overwrite_and_zero, test_close_constraints,
               test_batch_atomicity, test_setup_locked_after_data,
               test_concurrent_import_and_close):
        fn()
    print(f"\n==== 结果：{len(_passed)} 通过，{len(_failed)} 失败 ====")
    if _failed:
        print("失败项：")
        for name in _failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
