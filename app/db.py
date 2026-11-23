"""SQLite 连接与表结构。

表设计：
- meta            : 全局参数，当前仅有 pending_month（待结月）。
- batches         : 导入批次；period_month 记录导入时的待结月，决定该批计入哪一期封账。
- claims          : 赔款当前状态（同号重报整笔覆盖后的最新值）。
- claim_archived  : 每笔赔案在最近一次封账时的归档快照，用于计算下期净变动。
- closes          : 已封账月份。
- archive         : 每期归档汇总，只保存净变动非零的 (事故月, 支付月) 组合；回查只读此表。
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
  batch_id       INTEGER NOT NULL REFERENCES batches(id)
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
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
