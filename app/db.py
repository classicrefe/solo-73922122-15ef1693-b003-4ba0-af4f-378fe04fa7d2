"""SQLite 连接与表结构。

表设计：
- meta            : 全局参数，当前仅有 pending_month（待结月）。
- batches         : 导入批次；period_month 记录导入时的待结月，决定该批计入哪一期封账。
- claims          : 赔款当前状态（同号重报整笔覆盖后的最新值）。ever_closed 标记该赔款
                    是否已出现在某次成功封账中，用于区分「新赔款」与「封过但本期未变」。
- claim_archived  : 待结期内发生变化赔款的「上次封账值」基线，每笔至多一行；封账成功后
                    整批释放。未变化的已结赔款不再占用副本（当前值即基线）。
- closes          : 已封账月份。
- archive         : 每期归档汇总，只保存净变动非零的 (事故月, 支付月) 组合；回查只读此表。

旧库升级：旧版 claim_archived 为每笔已结赔款保留快照。启动时在一个事务中为 claims
补 ever_closed 列、标记已结赔款、删除与当前值完全一致的快照（这些快照不再需要），
保留与当前值不同的快照（待结更正）。任何一步失败则整体回滚，继续使用旧版逐笔快照
逻辑封账，旧库不会停留在半升级状态。
"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  period_month  TEXT NOT NULL,
  row_count     INTEGER NOT NULL,
  total_cents   INTEGER NOT NULL,
  overwritten   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS claims (
  claim_no       TEXT PRIMARY KEY,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  amount_cents   INTEGER NOT NULL CHECK (amount_cents >= 0),
  batch_id       INTEGER NOT NULL REFERENCES batches(id),
  ever_closed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS claim_archived (
  claim_no       TEXT PRIMARY KEY,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  amount_cents   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS closes (
  close_month TEXT PRIMARY KEY,
  batch_count INTEGER NOT NULL,
  combo_count INTEGER NOT NULL,
  closed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS archive (
  close_month    TEXT NOT NULL,
  accident_month TEXT NOT NULL,
  payment_month  TEXT NOT NULL,
  net_cents      INTEGER NOT NULL,
  PRIMARY KEY (close_month, accident_month, payment_month)
);

CREATE INDEX IF NOT EXISTS idx_claims_batch   ON claims(batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_period ON batches(period_month);
"""


def claims_have_ever_closed(conn):
    """当前库是否已升级到「按变化留存副本」结构（claims.ever_closed 列存在）。"""
    return any(row[1] == "ever_closed" for row in conn.execute("PRAGMA table_info(claims)"))


def migrate_db(conn):
    """把旧库（每笔已结赔款都留快照）就地升级为变化留存结构。

    整个过程在单个事务内完成：失败则整体回滚，库保持旧结构，调用方继续走旧版
    逐笔快照逻辑封账。成功时：
    - 给 claims 增加 ever_closed 并标记已出现在归档快照中的赔款；
    - 删除「快照 == 当前值」的副本（已结未变动，当前值即基线，无需副本）；
    - 保留「快照 != 当前值」的副本（这些是尚未封账的待结更正）。
    不触碰 archive，历期三角回查口径不变。可重复执行。
    返回 "current"（已是新结构）、"migrated" 或抛异常。
    """
    if claims_have_ever_closed(conn):
        return "current"
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 事务内复核：并发启动时另一进程可能已完成升级。
        if not claims_have_ever_closed(conn):
            conn.execute("ALTER TABLE claims ADD COLUMN ever_closed INTEGER NOT NULL DEFAULT 0")
        # 旧版只给封过账的赔款留快照，因此有快照即视为已结。
        conn.execute(
            "UPDATE claims SET ever_closed = 1"
            " WHERE ever_closed = 0"
            "   AND claim_no IN (SELECT claim_no FROM claim_archived)"
        )
        conn.execute(
            "DELETE FROM claim_archived"
            " WHERE EXISTS (SELECT 1 FROM claims c"
            "               WHERE c.claim_no = claim_archived.claim_no"
            "                 AND c.accident_month = claim_archived.accident_month"
            "                 AND c.payment_month  = claim_archived.payment_month"
            "                 AND c.amount_cents   = claim_archived.amount_cents)"
        )
        # 无对应赔款的孤立副本不可能是待结更正，一并清掉。
        conn.execute(
            "DELETE FROM claim_archived"
            " WHERE claim_no NOT IN (SELECT claim_no FROM claims)"
        )
        conn.commit()
        return "migrated"
    except Exception:
        conn.rollback()
        raise


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DB_PATH"], isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app):
    conn = sqlite3.connect(app.config["DB_PATH"], isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(SCHEMA)
        try:
            migrate_db(conn)
        except Exception as exc:  # noqa: BLE001
            # 升级失败不阻断启动：库仍是旧结构，逻辑层会自动退回逐笔快照模式。
            app.logger.exception("旧库升级失败，继续使用旧版快照逻辑：%s", exc)
        # 校验最终结构，供逻辑层选择归档策略。
        app.config["LEGACY_SNAPSHOTS"] = not claims_have_ever_closed(conn)
    finally:
        conn.close()
