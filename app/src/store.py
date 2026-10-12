"""SQLite time-series store: raw samples for 48 h, 5-minute averages for 30 days."""
import os
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "/data/metrics.db")
RAW_HOURS, ROLLUP_DAYS = 48, 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS s  (key TEXT NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL, PRIMARY KEY (key, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS s5 (key TEXT NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL, PRIMARY KEY (key, ts)) WITHOUT ROWID;
"""


def connect():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init():
    c = connect()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def insert(c, ts, metrics):
    rows = [(k, int(ts), float(v)) for k, v in metrics.items() if v is not None]
    c.executemany("INSERT OR REPLACE INTO s VALUES (?,?,?)", rows)
    c.commit()


def rollup(c):
    now = int(time.time())
    cutoff = (now - RAW_HOURS * 3600) // 300 * 300  # aligned, so every 5-minute bucket is complete
    c.execute("INSERT OR REPLACE INTO s5 SELECT key, ts/300*300, AVG(v) FROM s WHERE ts < ? GROUP BY key, ts/300*300", (cutoff,))
    c.execute("DELETE FROM s WHERE ts < ?", (cutoff,))
    c.execute("DELETE FROM s5 WHERE ts < ?", (now - ROLLUP_DAYS * 86400,))
    c.commit()


def series(keys, hours):
    hours = max(1.0, min(float(hours), ROLLUP_DAYS * 24.0))
    since = int(time.time() - hours * 3600)
    long_range = hours > RAW_HOURS
    bucket = max(15, int(hours * 3600 / 300))
    if long_range:
        bucket = max(bucket, 300)
    c = connect()
    try:
        out = {}
        for k in keys:
            if long_range:
                sql = (f"SELECT ts/{bucket}*{bucket}, AVG(v) FROM ("
                       "SELECT ts, v FROM s WHERE key=? AND ts>=? UNION ALL "
                       "SELECT ts, v FROM s5 WHERE key=? AND ts>=?) GROUP BY 1")
                args = (k, since, k, since)
            else:
                sql = f"SELECT ts/{bucket}*{bucket}, AVG(v) FROM s WHERE key=? AND ts>=? GROUP BY 1"
                args = (k, since)
            out[k] = dict(c.execute(sql, args).fetchall())
    finally:
        c.close()
    ts = sorted({t for d in out.values() for t in d})
    return {"t": ts, "series": {k: [out[k].get(t) for t in ts] for k in keys}, "bucket": bucket}
