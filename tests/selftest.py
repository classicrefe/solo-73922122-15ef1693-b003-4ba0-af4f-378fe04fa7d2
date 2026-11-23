#!/usr/bin/env python3
"""关键操作自测（不依赖 pytest）：设置待结月、CSV 校验、封账推进、同号覆盖、
补报净变动归档、两期回查一致性、零额显示、失败原子性、导入/封账并发；
以及逐笔副本优化：只留变化项、改回释放、新赔款全额、零抵消、失败保留原状、
旧库升级与升级失败回退。

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


def exec_db(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql)
        conn.commit()
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


# ---------------------------------------------------------------- 8. 逐笔副本只留变化项
def test_snapshots_only_changed():
    print("\n[8] 逐笔副本：仅留存与上次封账不同的赔款，每笔至多一份")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, open(BASE_CSV, encoding="utf-8").read())
    check("导入后尚无副本（未封账，无基线）",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 0)
    close_month(client, "2026-01")
    check("封账后释放本期副本（7 笔均不占副本）",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 0)

    # 补报批：C-0002 改金额、C-0004 改支付月、C-0007 同数重报，其余 4 笔为本期待结新值
    import_text(client, open(SUPP_CSV, encoding="utf-8").read())
    snaps = {r[0]: r[1:] for r in q(
        db, "SELECT claim_no, accident_month, payment_month, amount_cents FROM claim_archived")}
    check("导入后只留 2 份变化副本", set(snaps) == {"C-0002", "C-0004"}, f"got {snaps}")
    check("C-0002 副本保留上次封账值 2025-10/2025-12/80000",
          snaps.get("C-0002") == ("2025-10", "2025-12", 80000), f"got {snaps.get('C-0002')}")
    check("C-0004 副本保留上次封账值 2025-11/2026-01/60000",
          snaps.get("C-0004") == ("2025-11", "2026-01", 60000), f"got {snaps.get('C-0004')}")
    check("同数重报 C-0007 不占副本", "C-0007" not in snaps)
    check("未封过的新赔款（C-0008..）不占副本",
          not ({"C-0008", "C-0009", "C-0010", "C-0011"} & set(snaps)))

    page = client.get("/close")
    text = page.get_data(as_text=True)
    check("封账页显示额外留存的逐笔记录数 2", "逐笔副本：<strong>2</strong>" in text, text[-500:])

    close_month(client, "2026-02")
    check("成功封账后本期副本全部释放",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 0)

    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("新赔款按全额计入本期（C-0008 45000 / C-0009 70000 / C-0010 110000 / C-0011 180000）",
          nets.get(("2025-11", "2026-02")) == 105000
          and nets.get(("2025-12", "2026-02")) == 70000
          and nets.get(("2026-01", "2026-02")) == 110000
          and nets.get(("2026-02", "2026-02")) == 180000, f"got {nets}")


# ---------------------------------------------------------------- 9. 反复重报与改回
def test_repeat_and_revert():
    print("\n[9] 同号反复重报按最终值留存；改回上次封账值即释放副本")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "R1,2025-12,2026-01,10000\nR2,2025-12,2026-01,20000\n")
    close_month(client, "2026-01")

    import_text(client, HEADER + "R1,2025-12,2026-01,15000\n")
    check("第一次改金额后留 1 份副本",
          q(db, "SELECT COUNT(*) FROM claim_archived WHERE claim_no='R1'")[0][0] == 1)
    import_text(client, HEADER + "R1,2025-12,2026-01,18000\n")
    rows = q(db, "SELECT accident_month, payment_month, amount_cents FROM claim_archived WHERE claim_no='R1'")
    check("再次改金额仍只有一份，且保留最初封账值 10000",
          rows == [("2025-12", "2026-01", 10000)], f"got {rows}")
    import_text(client, HEADER + "R1,2025-12,2026-01,10000\n")
    check("最终值改回上次封账值 -> 副本释放",
          q(db, "SELECT COUNT(*) FROM claim_archived WHERE claim_no='R1'")[0][0] == 0)

    close_month(client, "2026-02")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("改回后本期无净变动（R1 不进归档，R2 未触及）", nets == {}, f"got {nets}")

    # 改月份后又改回：副本同样释放
    import_text(client, HEADER + "R2,2025-12,2026-02,20000\n")
    check("R2 改支付月后留 1 份副本",
          q(db, "SELECT COUNT(*) FROM claim_archived WHERE claim_no='R2'")[0][0] == 1)
    import_text(client, HEADER + "R2,2025-12,2026-01,20000\n")
    check("R2 支付月改回 -> 副本释放",
          q(db, "SELECT COUNT(*) FROM claim_archived WHERE claim_no='R2'")[0][0] == 0)


# ---------------------------------------------------------------- 10. 零抵消与失败保留
def test_zero_offset_and_failure_keeps_snapshots():
    print("\n[10] 零抵消更正下期仍按逐笔扣减；导入/封账失败保留副本原状")
    app, client, db = make_client()
    setup_pending(client)
    import_text(client, HEADER + "Z1,2025-12,2026-01,10000\nZ2,2025-12,2026-01,20000\n")
    close_month(client, "2026-01")

    # Z1 金额 +5000，Z2 金额 -5000 -> 同组合净变动为 0，但两笔都算本期变动
    import_text(client, HEADER + "Z1,2025-12,2026-01,15000\nZ2,2025-12,2026-01,15000\n")
    check("两笔更正各留一份副本",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 2)
    preview = client.get("/close").get_data(as_text=True)
    check("封账预览零抵消组合不显示",
          "本期无净变动" in preview, preview)
    close_month(client, "2026-02")
    check("零抵消期不产生归档行",
          q(db, "SELECT COUNT(*) FROM archive WHERE close_month='2026-02'")[0][0] == 0)
    check("零抵消封账后副本释放",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 0)

    # 下期只改 Z1，应仅按 Z1 的新旧差扣减（15000 -> 12000，-3000），不受上期零抵消影响
    import_text(client, HEADER + "Z1,2025-12,2026-01,12000\n")
    before = q(db, "SELECT claim_no, amount_cents FROM claim_archived ORDER BY claim_no")
    check("失败前留有 1 份 Z1 副本", before == [("Z1", 15000)], f"got {before}")
    bad = import_text(client, HEADER + "Z2,2025-12,2026-13,20000\n")
    check("含非法行的整批被拒绝", bad.status_code == 400)
    after = q(db, "SELECT claim_no, amount_cents FROM claim_archived ORDER BY claim_no")
    check("导入失败后副本原状保留", after == before, f"got {after}")
    r = close_month(client, "2026-04")  # 当前待结月为 2026-03，应报错
    check("封错月份失败", "只能封当前待结月 2026-03" in r.get_data(as_text=True))
    after2 = q(db, "SELECT claim_no, amount_cents FROM claim_archived ORDER BY claim_no")
    check("封账失败后副本原状保留", after2 == before, f"got {after2}")
    close_month(client, "2026-03")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-03'"))
    check("下期按本次封账逐笔金额扣减（Z1 -3000）",
          nets == {("2025-12", "2026-01"): -3000}, f"got {nets}")


# ---------------------------------------------------------------- 11. 旧库升级
def _make_legacy_db(path):
    """构造旧版库：每笔已结赔款都有快照，其中一笔待结更正。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT, period_month TEXT NOT NULL,
          row_count INTEGER NOT NULL, total_cents INTEGER NOT NULL,
          overwritten INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')));
        CREATE TABLE claims (claim_no TEXT PRIMARY KEY, accident_month TEXT NOT NULL,
          payment_month TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
          batch_id INTEGER NOT NULL REFERENCES batches(id));
        CREATE TABLE claim_archived (claim_no TEXT PRIMARY KEY, accident_month TEXT NOT NULL,
          payment_month TEXT NOT NULL, amount_cents INTEGER NOT NULL);
        CREATE TABLE closes (close_month TEXT PRIMARY KEY, batch_count INTEGER NOT NULL,
          combo_count INTEGER NOT NULL,
          closed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')));
        CREATE TABLE archive (close_month TEXT NOT NULL, accident_month TEXT NOT NULL,
          payment_month TEXT NOT NULL, net_cents INTEGER NOT NULL,
          PRIMARY KEY (close_month, accident_month, payment_month));
        """)
        conn.execute("INSERT INTO meta VALUES('pending_month','2026-02')")
        conn.execute("INSERT INTO batches(period_month,row_count,total_cents,overwritten)"
                     " VALUES('2026-01',2,30000,0)")
        # 第 2 批（2026-02 期）重报 L2：已结值 20000 -> 待结更正 25000
        conn.execute("INSERT INTO batches(period_month,row_count,total_cents,overwritten)"
                     " VALUES('2026-02',1,25000,1)")
        conn.execute("INSERT INTO claims VALUES('L1','2025-12','2026-01',10000,1)")
        conn.execute("INSERT INTO claims VALUES('L2','2025-12','2026-01',25000,2)")
        conn.execute("INSERT INTO claim_archived VALUES('L1','2025-12','2026-01',10000)")
        conn.execute("INSERT INTO claim_archived VALUES('L2','2025-12','2026-01',20000)")
        conn.execute("INSERT INTO closes VALUES('2026-01',1,1,'2026-01-31 00:00:00')")
        conn.execute("INSERT INTO archive VALUES('2026-01','2025-12','2026-01',30000)")
        conn.commit()
    finally:
        conn.close()


def test_legacy_upgrade():
    print("\n[11] 旧库升级：删掉未变化副本，保住待结更正，历期三角不变")
    tmp = tempfile.mkdtemp(prefix="lt-legacy-")
    db = os.path.join(tmp, "old.db")
    _make_legacy_db(db)
    app = create_app({"DB_PATH": db, "TESTING": True})
    client = app.test_client()
    check("升级后 claims 有 ever_closed 列",
          q(db, "SELECT COUNT(*) FROM pragma_table_info('claims') WHERE name='ever_closed'")[0][0] == 1)
    rows = q(db, "SELECT claim_no, amount_cents FROM claim_archived ORDER BY claim_no")
    check("只保留待结更正 L2 的旧值 20000", rows == [("L2", 20000)], f"got {rows}")
    flags = dict(q(db, "SELECT claim_no, ever_closed FROM claims"))
    check("已结赔款标记 ever_closed=1", flags == {"L1": 1, "L2": 1}, f"got {flags}")
    t = client.get("/triangle?start=2025-12&close=2026-01").get_data(as_text=True)
    check("升级不改历期三角汇总（30,000）", "30,000" in t, t[-800:])
    close_month(client, "2026-02")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("升级后封账只产生 L2 更正 +5000",
          nets == {("2025-12", "2026-01"): 5000}, f"got {nets}")
    check("升级后封账释放全部副本",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 0)

    # 重复启动（幂等）：不再改动副本
    exec_db(db, "INSERT INTO claim_archived VALUES('L1','2025-12','2026-01',9000)")
    create_app({"DB_PATH": db, "TESTING": True})
    check("重复启动不重复升级、不动已有副本",
          q(db, "SELECT claim_no FROM claim_archived ORDER BY claim_no") == [("L1",)])


# ---------------------------------------------------------------- 12. 升级失败回退旧逻辑
def test_upgrade_failure_falls_back():
    print("\n[12] 升级失败：库保持旧结构，仍能用旧版快照逻辑封账")
    tmp = tempfile.mkdtemp(prefix="lt-legacy-fail-")
    db = os.path.join(tmp, "old.db")
    _make_legacy_db(db)
    # 用真实数据库错误制造升级中途失败（ALTER 之后、清理副本时被阻止）。
    exec_db(db, "CREATE TRIGGER block_migrate BEFORE DELETE ON claim_archived"
                " BEGIN SELECT RAISE(ABORT, 'upgrade blocked'); END")
    app = create_app({"DB_PATH": db, "TESTING": True})
    client = app.test_client()
    check("升级失败后仍无 ever_closed 列（ALTER 已回滚）",
          q(db, "SELECT COUNT(*) FROM pragma_table_info('claims') WHERE name='ever_closed'")[0][0] == 0)
    check("升级失败后旧副本原样保留（L1、L2 各一份）",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 2)
    check("应用按旧结构启动（LEGACY_SNAPSHOTS=True）", app.config.get("LEGACY_SNAPSHOTS") is True)
    r = client.get("/close")
    check("旧结构封账页可打开", r.status_code == 200)
    check("旧结构封账页提示仍处于旧结构", "旧版结构" in r.get_data(as_text=True))
    close_month(client, "2026-02")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("旧结构封账净变动仍正确（L2 +5000）",
          nets == {("2025-12", "2026-01"): 5000}, f"got {nets}")
    check("旧结构封账继续按旧版为触及赔案留快照",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 2)

    # 障碍排除后重启即可完成升级，已有快照按新规则收缩为待结更正。
    exec_db(db, "DROP TRIGGER block_migrate")
    app2 = create_app({"DB_PATH": db, "TESTING": True})
    check("重启后完成升级", app2.config.get("LEGACY_SNAPSHOTS") is False)


# ---------------------------------------------------------------- 13. 网页演示批次
def test_demo_revise_revert_flow():
    print("\n[13] 网页演示批次：重报 -> 改回 -> 封账 -> 零抵消下期逐笔扣减")
    app, client, db = make_client()
    setup_pending(client)
    base = "claims_2026-01_base.csv"
    r = client.post("/import", data={
        "file": (open(os.path.join(REPO, "demo", base), "rb"), base),
    }, content_type="multipart/form-data")
    check(f"演示批次 {base} 导入成功", r.status_code == 200)
    close_month(client, "2026-01")
    supp = "claims_2026-02_supplement.csv"
    r = client.post("/import", data={
        "file": (open(os.path.join(REPO, "demo", supp), "rb"), supp),
    }, content_type="multipart/form-data")
    check(f"演示批次 {supp} 导入成功", r.status_code == 200)
    close_month(client, "2026-02")

    r = client.post("/import", data={
        "file": (open(os.path.join(REPO, "demo", "claims_2026-03_revise.csv"), "rb"),
                 "claims_2026-03_revise.csv"),
    }, content_type="multipart/form-data")
    check("重报批导入成功", r.status_code == 200)
    check("重报后 5 份副本",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 5)
    check("封账页显示 5 份副本",
          "逐笔副本：<strong>5</strong>" in client.get("/close").get_data(as_text=True))

    r = client.post("/import", data={
        "file": (open(os.path.join(REPO, "demo", "claims_2026-03_revert.csv"), "rb"),
                 "claims_2026-03_revert.csv"),
    }, content_type="multipart/form-data")
    check("改回批导入成功", r.status_code == 200)
    snaps = {r[0] for r in q(db, "SELECT claim_no FROM claim_archived")}
    check("改回 3 笔后仅剩 2 份（C-0004 / C-0008 为零抵消更正）",
          snaps == {"C-0004", "C-0008"}, f"got {snaps}")
    check("改回后封账预览无净变动",
          "本期无净变动" in client.get("/close").get_data(as_text=True))
    r = close_month(client, "2026-03")
    check("零抵消期封账成功且 0 归档组合", "归档 0 个组合" in r.get_data(as_text=True))
    check("零抵消期封账后副本释放",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 0)

    client.post("/import", data={
        "file": (open(os.path.join(REPO, "demo", "claims_2026-04_after_zero.csv"), "rb"),
                 "claims_2026-04_after_zero.csv"),
    }, content_type="multipart/form-data")
    check("下期再改 C-0004 时仅 1 份副本",
          q(db, "SELECT COUNT(*) FROM claim_archived")[0][0] == 1)
    close_month(client, "2026-04")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-04'"))
    check("下期按本次封账逐笔金额扣减（C-0004 55000 -> 52000，-3000）",
          nets == {("2025-11", "2026-02"): -3000}, f"got {nets}")
    t = client.get("/triangle?start=2025-10&close=2026-04").get_data(as_text=True)
    check("四期三角沿用原归档可回查", t.count("<tr>") >= 7 and "200,000" in t)


# ---------------------------------------------------------------- 14. 事务中途失败保持原状
def test_midtransaction_failure_keeps_state():
    print("\n[14] 事务中途失败（数据库报错）：导入/封账均整体回滚，副本与待结月不变")
    app, client, db = make_client()
    app.config["PROPAGATE_EXCEPTIONS"] = False  # 模拟生产：数据库异常返回 500
    setup_pending(client)
    import_text(client, HEADER + "M1,2025-12,2026-01,10000\n")
    close_month(client, "2026-01")
    import_text(client, HEADER + "M1,2025-12,2026-01,15000\nM2,2025-12,2026-01,7000\n")

    snaps_before = q(db, "SELECT claim_no, amount_cents FROM claim_archived ORDER BY claim_no")
    claims_before = q(db, "SELECT claim_no, amount_cents, batch_id FROM claims ORDER BY claim_no")
    batches_before = q(db, "SELECT COUNT(*) FROM batches")

    # 导入：第二条 claims 写入时触发器报错 -> 整批（含第一条的副本变化）回滚
    exec_db(db, "CREATE TRIGGER boom_imp BEFORE INSERT ON claims"
                " WHEN NEW.claim_no = 'M3' BEGIN SELECT RAISE(ABORT, 'boom'); END")
    r = client.post("/import", data={"csv_text": HEADER + "M3,2025-12,2026-01,100\n"})
    check("数据库报错导致导入 500（非静默成功）", r.status_code == 500, f"status={r.status_code}")
    check("导入失败后副本原状", q(db, "SELECT claim_no, amount_cents FROM claim_archived"
                                     " ORDER BY claim_no") == snaps_before)
    check("导入失败后 claims/batches 原状",
          q(db, "SELECT claim_no, amount_cents, batch_id FROM claims ORDER BY claim_no") == claims_before
          and q(db, "SELECT COUNT(*) FROM batches") == batches_before)

    # 封账：写 archive 时触发器报错 -> 归档、副本、待结月全部回滚
    exec_db(db, "DROP TRIGGER boom_imp")
    exec_db(db, "CREATE TRIGGER boom_close BEFORE INSERT ON archive"
                " BEGIN SELECT RAISE(ABORT, 'boom'); END")
    r = client.post("/close", data={"close_month": "2026-02"}, follow_redirects=True)
    check("封账数据库报错时页面未显示成功", "已封账 2026-02" not in r.get_data(as_text=True))
    check("封账失败后副本原状", q(db, "SELECT claim_no, amount_cents FROM claim_archived"
                                     " ORDER BY claim_no") == snaps_before)
    check("封账失败后未写 archive 行",
          q(db, "SELECT COUNT(*) FROM archive WHERE close_month='2026-02'")[0][0] == 0)
    check("封账失败后待结月仍为 2026-02",
          q(db, "SELECT value FROM meta WHERE key='pending_month'")[0][0] == "2026-02")
    check("封账失败后未写 closes 行",
          q(db, "SELECT COUNT(*) FROM closes WHERE close_month='2026-02'")[0][0] == 0)

    exec_db(db, "DROP TRIGGER boom_close")
    close_month(client, "2026-02")
    nets = dict(((a, p), n) for a, p, n in q(
        db, "SELECT accident_month, payment_month, net_cents FROM archive WHERE close_month='2026-02'"))
    check("恢复后重封成功，净变动 +5000 / +7000 正确",
          nets == {("2025-12", "2026-01"): 12000}, f"got {nets}")


# ---------------------------------------------------------------- 15. 参考模型差分随机测试
def test_reference_model_fuzz():
    """随机导入/封账序列，与「逐笔上次封账值」参考模型逐步比对。

    不变量：每笔至多一份副本；副本存在 <=> 已结且当前值与上次封账值不同，
    且副本内容等于上次封账值；archive 与参考净变动一致。
    """
    import random
    print("\n[15] 参考模型差分随机测试（20 个种子 × 最多 40 步）")
    header = HEADER
    months = ["2026-01", "2026-02", "2026-03", "2026-04"]
    accs = ["2025-11", "2025-12", "2026-01", "2026-02"]
    ok = True
    for seed in range(20):
        rng = random.Random(seed)
        app, client, db = make_client()
        setup_pending(client)
        pending, staged = months[0], []
        last_closed, ever, expected = {}, set(), {}
        claimants = [f"K{i}" for i in range(1, 7)]

        def invariants(step):
            conn = sqlite3.connect(db)
            try:
                claims = {r[0]: tuple(r[1:]) for r in conn.execute(
                    "SELECT claim_no, accident_month, payment_month, amount_cents FROM claims")}
                snaps = {r[0]: tuple(r[1:]) for r in conn.execute(
                    "SELECT claim_no, accident_month, payment_month, amount_cents FROM claim_archived")}
                arch = {(r[0], r[1], r[2]): r[3] for r in conn.execute(
                    "SELECT close_month, accident_month, payment_month, net_cents FROM archive")}
            finally:
                conn.close()
            if arch != expected:
                return f"seed{seed} step{step}: archive {arch} != {expected}"
            if not set(snaps) <= ever:
                return f"seed{seed} step{step}: snapshot for never-closed {set(snaps) - ever}"
            for no, snap in snaps.items():
                if claims.get(no) == last_closed.get(no) or snap != last_closed.get(no):
                    return f"seed{seed} step{step}: bad snapshot {no}={snap} last={last_closed.get(no)} cur={claims.get(no)}"
            for no, cur in claims.items():
                if no in ever and cur != last_closed[no] and snaps.get(no) != last_closed[no]:
                    return f"seed{seed} step{step}: missing snapshot {no}"
            return None

        for step in range(40):
            if rng.random() < 0.65:
                chosen = rng.sample(claimants, rng.randint(1, 3))
                parsed = {}
                lines = []
                for no in chosen:
                    acc = rng.choice([a for a in accs if a <= pending])
                    pay = rng.choice([m for m in months if acc <= m <= pending])
                    amt = rng.choice([0, 1000, 5000, 10000, 25000])
                    parsed[no] = (acc, pay, amt)
                    lines.append(f"{no},{acc},{pay},{amt}")
                r = client.post("/import", data={"csv_text": header + "\n".join(lines) + "\n"})
                if r.status_code != 200:
                    return f"seed{seed} step{step}: import {r.status_code}"
                staged.append((pending, parsed))
            else:
                touched = {}
                for period, parsed in staged:
                    if period == pending:
                        touched.update(parsed)
                deltas = {}
                for no, cur in touched.items():
                    base = last_closed.get(no)
                    if base:
                        deltas[(base[0], base[1])] = deltas.get((base[0], base[1]), 0) - base[2]
                    deltas[(cur[0], cur[1])] = deltas.get((cur[0], cur[1]), 0) + cur[2]
                r = client.post("/close", data={"close_month": pending}, follow_redirects=True)
                if "已封账" not in r.get_data(as_text=True):
                    return f"seed{seed} step{step}: close failed"
                for key, net in deltas.items():
                    if net != 0:
                        expected[(pending, key[0], key[1])] = net
                for no, cur in touched.items():
                    last_closed[no] = cur
                    ever.add(no)
                staged = [(p, b) for p, b in staged if p != pending]
                if pending == months[-1]:
                    break
                pending = months[months.index(pending) + 1]
            err = invariants(step)
            if err:
                ok = False
                print(f"  FAIL  {err}")
                break
        if not ok:
            break
    check("随机序列与参考模型一致", ok)


def main():
    for fn in (test_full_flow_two_periods, test_import_validation,
               test_overwrite_and_zero, test_close_constraints,
               test_batch_atomicity, test_setup_locked_after_data,
               test_concurrent_import_and_close, test_snapshots_only_changed,
               test_repeat_and_revert, test_zero_offset_and_failure_keeps_snapshots,
               test_legacy_upgrade, test_upgrade_failure_falls_back,
               test_demo_revise_revert_flow, test_midtransaction_failure_keeps_state,
               test_reference_model_fuzz):
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
