"""SQLite 持久化：观测值幂等写入 + 每个指标的最新状态与基础错误日志。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    metric     TEXT NOT NULL,
    obs_date   TEXT NOT NULL,
    value      REAL NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (metric, obs_date)
);

CREATE TABLE IF NOT EXISTS metric_status (
    metric     TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    obs_date   TEXT,
    value      REAL,
    source     TEXT,
    note       TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok_count   INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS errors (
    error_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    metric    TEXT NOT NULL,
    message   TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_observations(
    conn: sqlite3.Connection,
    metric: str,
    rows: Iterable[Sequence[object]],
    source: str,
    fetched_at: str,
) -> int:
    """写入观测值。主键 (metric, obs_date) 保证重复采集不产生重复记录。"""
    payload = [
        (metric, _as_date_text(row[0]), float(row[1]), source, fetched_at)
        for row in rows
        if row[1] is not None
    ]
    if not payload:
        return 0
    before = conn.execute('SELECT COUNT(*) FROM observations WHERE metric = ?', (metric,)).fetchone()[0]
    conn.executemany(
        'INSERT INTO observations (metric, obs_date, value, source, fetched_at) '
        'VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT(metric, obs_date) DO UPDATE SET '
        'value = excluded.value, source = excluded.source, fetched_at = excluded.fetched_at',
        payload,
    )
    conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM observations WHERE metric = ?', (metric,)).fetchone()[0]
    return after - before


def latest_observation(conn: sqlite3.Connection, metric: str) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT obs_date, value, source, fetched_at FROM observations '
        'WHERE metric = ? ORDER BY obs_date DESC LIMIT 1',
        (metric,),
    ).fetchone()


def observation_before(conn: sqlite3.Connection, metric: str, obs_date: str) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT obs_date, value, source, fetched_at FROM observations '
        'WHERE metric = ? AND obs_date < ? ORDER BY obs_date DESC LIMIT 1',
        (metric, obs_date),
    ).fetchone()


def observations_since(conn: sqlite3.Connection, metric: str, start: str) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT obs_date, value FROM observations WHERE metric = ? AND obs_date >= ? ORDER BY obs_date',
        (metric, start),
    ).fetchall()


def observations_all(conn: sqlite3.Connection, metric: str) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT obs_date, value FROM observations WHERE metric = ? ORDER BY obs_date',
        (metric,),
    ).fetchall()


def count_observations(conn: sqlite3.Connection, metric: str) -> int:
    return conn.execute('SELECT COUNT(*) FROM observations WHERE metric = ?', (metric,)).fetchone()[0]


def set_status(
    conn: sqlite3.Connection,
    metric: str,
    status: str,
    *,
    obs_date: str | None = None,
    value: float | None = None,
    source: str | None = None,
    note: str | None = None,
    checked_at: str | None = None,
) -> None:
    conn.execute(
        'INSERT INTO metric_status (metric, status, obs_date, value, source, note, checked_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(metric) DO UPDATE SET '
        'status = excluded.status, obs_date = excluded.obs_date, value = excluded.value, '
        'source = excluded.source, note = excluded.note, checked_at = excluded.checked_at',
        (metric, status, obs_date, value, source, note, checked_at or utcnow()),
    )
    conn.commit()


def get_statuses(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row['metric']: row for row in conn.execute('SELECT * FROM metric_status')}


def log_error(conn: sqlite3.Connection, metric: str, message: str, at: str | None = None) -> None:
    conn.execute(
        'INSERT INTO errors (at, metric, message) VALUES (?, ?, ?)',
        (at or utcnow(), metric, message[:500]),
    )
    conn.commit()


def recent_errors(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT at, metric, message FROM errors ORDER BY error_id DESC LIMIT ?', (limit,)
    ).fetchall()


def start_run(conn: sqlite3.Connection, started_at: str | None = None) -> int:
    cursor = conn.execute('INSERT INTO runs (started_at) VALUES (?)', (started_at or utcnow(),))
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, ok_count: int, fail_count: int) -> None:
    conn.execute(
        'UPDATE runs SET finished_at = ?, ok_count = ?, fail_count = ? WHERE run_id = ?',
        (utcnow(), ok_count, fail_count, run_id),
    )
    conn.commit()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute('SELECT * FROM runs ORDER BY run_id DESC LIMIT 1').fetchone()


def _as_date_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10]
