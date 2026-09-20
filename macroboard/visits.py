"""轻量访问统计：页面访问人次、匿名独立访客与访客 IP（本地 SQLite，不联网）。

口径（页面展示与 `docs/DATA_SOURCES.md` 必须一致）：

- **一次页面会话计 1 人次**：打开或刷新页面会建立一次 Streamlit 会话；会话内的交互
  （切换时间范围、缩放图表等）会重跑脚本，但调用方只在会话里记录一次。
- **独立访客按指纹去重**：`SHA-256(随机盐 + 客户端 IP + User-Agent)` 的前 16 位十六进制。
  指纹相同只代表「同一 IP + 同一浏览器」，不是身份识别，不用于登录或风控。
  盐随机生成并保存在统计库内。
- **每条记录同时保存客户端 IP**（`visits.ip`，2026-09-20 起）：供后续在后台按 IP 核对，
  页面暂不显示。存的是 Streamlit 会话上下文给出的连接来源 IP，本机访问为 `None`（记 NULL，
  因为 Streamlit 对 `127.0.0.1` / `::1` 返回 None）；User-Agent 仍然只以指纹形式参与，
  不保存原文。补列之前写入的历史行 `ip` 为空，不回溯推断。
- **自然日按北京时间（Asia/Shanghai）划分**，时间戳按 UTC 保存。
- 统计写入与行情库分开的文件（默认 `<行情库目录>/visits.sqlite3`），
  因此页面对行情库保持只读，不修改任何行情观测值；统计库在本机、权限 600、已被
  `.gitignore` 忽略，但 IP 会一直留存，删除该文件即清除全部访问记录。
- 已知限制：WebSocket 断开或服务重启后 Streamlit 会新建会话，同一个人可能被计成多次；
  换浏览器或换设备会被算成不同访客；没有 cookie，无法跨设备去重；
  经反向代理部署时记录的是 Streamlit 看到的来源 IP（不保证是终端访客真实 IP）。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo('Asia/Shanghai')

DEFAULT_VISIT_DB_NAME = 'visits.sqlite3'
SALT_KEY = 'visitor_hash_salt'
FINGERPRINT_LENGTH = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    visit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    day      TEXT NOT NULL,
    visitor  TEXT NOT NULL,
    ip       TEXT
);

CREATE INDEX IF NOT EXISTS idx_visits_day ON visits(day);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 只读统计查询：SQL 全部写成常量，不做字符串拼接（避免 SQL 注入类告警）。
COUNT_BY_DAY = (
    'SELECT COUNT(*) AS visits, COUNT(DISTINCT visitor) AS visitors FROM visits WHERE day = ?'
)
COUNT_SINCE_DAY = (
    'SELECT COUNT(*) AS visits, COUNT(DISTINCT visitor) AS visitors FROM visits WHERE day >= ?'
)
COUNT_TOTALS = (
    'SELECT COUNT(*) AS visits, COUNT(DISTINCT visitor) AS visitors, '
    'MIN(at) AS first_at, MAX(at) AS last_at FROM visits'
)
COUNT_HISTORY = (
    'SELECT day, COUNT(*) AS visits, COUNT(DISTINCT visitor) AS visitors '
    'FROM visits WHERE day >= ? GROUP BY day ORDER BY day'
)


@dataclass(frozen=True)
class VisitDay:
    """一天的人次与独立访客数。"""

    day: date
    visits: int
    visitors: int


@dataclass(frozen=True)
class VisitStats:
    """页面展示用的访问统计汇总（全部为真实记录，缺失即 0）。"""

    window_days: int
    history_days: int
    today_visits: int
    today_visitors: int
    window_visits: int
    window_visitors: int
    total_visits: int
    total_visitors: int
    first_at: str | None
    last_at: str | None
    daily: tuple[VisitDay, ...]


def logging_enabled() -> bool:
    """是否记录访问：默认开启，`MACROBOARD_VISIT_LOG=0/false/no/off` 关闭。"""
    value = os.environ.get('MACROBOARD_VISIT_LOG', '').strip().lower()
    return value not in {'0', 'false', 'no', 'off'}


def resolve_visit_db_path(market_db_path: Path | str) -> Path:
    """统计库路径：`MACROBOARD_VISIT_DB` 优先，否则与行情库同目录。"""
    override = os.environ.get('MACROBOARD_VISIT_DB', '').strip()
    if override:
        return Path(override).expanduser()
    return Path(market_db_path).expanduser().parent / DEFAULT_VISIT_DB_NAME


def beijing_now() -> datetime:
    return datetime.now(UTC).astimezone(BEIJING)


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    _restrict_permissions(path)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """旧版统计库没有 `ip` 列：补列，历史行保持为空（不推断、不补数据）。"""
    columns = {str(row['name']) for row in conn.execute('PRAGMA table_info(visits)')}
    if 'ip' not in columns:
        conn.execute('ALTER TABLE visits ADD COLUMN ip TEXT')


def visitor_fingerprint(ip: str | None, user_agent: str | None, salt: str) -> str:
    """匿名访客指纹：不可逆，且加盐后无法用彩虹表反查 IP / User-Agent。"""
    payload = f'{salt}|{ip or ""}|{user_agent or ""}'.encode()
    return hashlib.sha256(payload).hexdigest()[:FINGERPRINT_LENGTH]


def normalize_ip(ip: str | None) -> str | None:
    """落库前清洗：只接受字符串，去空白、限长 64；本机（None）与空串都记 NULL。

    非字符串（测试替身、未来接口变更返回的对象）一律忽略，避免把不可序列化的值写进库。
    """
    if not isinstance(ip, str):
        return None
    text = ip.strip()
    return text[:64] or None


def record_visit(
    db_path: Path | str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> str:
    """记录一次页面访问（含客户端 IP），返回匿名指纹便于调用方自检。"""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local_day = moment.astimezone(BEIJING).date().isoformat()
    stamp = moment.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
    conn = connect(db_path)
    try:
        init_db(conn)
        fingerprint = visitor_fingerprint(ip, user_agent, _salt(conn))
        conn.execute(
            'INSERT INTO visits (at, day, visitor, ip) VALUES (?, ?, ?, ?)',
            (stamp, local_day, fingerprint, normalize_ip(ip)),
        )
        conn.commit()
    finally:
        conn.close()
    return fingerprint


def load_stats(
    db_path: Path | str,
    *,
    window_days: int = 7,
    history_days: int = 14,
    today: date | None = None,
) -> VisitStats | None:
    """读取统计；文件缺失返回全 0，文件损坏或无权限返回 None（页面标注「未接通」）。"""
    path = Path(db_path)
    reference = today or beijing_now().date()
    window = max(int(window_days), 1)
    history = max(int(history_days), 1)
    if not path.exists():
        return _empty_stats(reference, window, history)

    window_start = reference - timedelta(days=window - 1)
    history_start = reference - timedelta(days=history - 1)
    try:
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            totals = _one(conn.execute(COUNT_TOTALS))
            today_row = _one(conn.execute(COUNT_BY_DAY, (reference.isoformat(),)))
            window_row = _one(conn.execute(COUNT_SINCE_DAY, (window_start.isoformat(),)))
            daily = tuple(
                VisitDay(
                    day=date.fromisoformat(str(row['day'])),
                    visits=int(row['visits']),
                    visitors=int(row['visitors']),
                )
                for row in conn.execute(COUNT_HISTORY, (history_start.isoformat(),))
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    return VisitStats(
        window_days=window,
        history_days=history,
        today_visits=int(today_row['visits']),
        today_visitors=int(today_row['visitors']),
        window_visits=int(window_row['visits']),
        window_visitors=int(window_row['visitors']),
        total_visits=int(totals['visits']),
        total_visitors=int(totals['visitors']),
        first_at=totals['first_at'],
        last_at=totals['last_at'],
        daily=daily,
    )


def to_beijing_text(stamp: str | None) -> str:
    """把库里的 UTC 时间戳转成北京时间文本；无法解析时返回「—」。"""
    if not stamp:
        return '—'
    try:
        moment = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return '—'
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(BEIJING).strftime('%Y-%m-%d %H:%M')


def _salt(conn: sqlite3.Connection) -> str:
    row = conn.execute('SELECT value FROM meta WHERE key = ?', (SALT_KEY,)).fetchone()
    if row is not None:
        return str(row['value'])
    salt = secrets.token_hex(16)
    conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (SALT_KEY, salt))
    conn.commit()
    return salt


def _one(cursor: sqlite3.Cursor) -> sqlite3.Row:
    row = cursor.fetchone()
    assert row is not None  # COUNT 查询必然返回一行
    return row


def _empty_stats(reference: date, window: int, history: int) -> VisitStats:
    return VisitStats(
        window_days=window,
        history_days=history,
        today_visits=0,
        today_visitors=0,
        window_visits=0,
        window_visitors=0,
        total_visits=0,
        total_visitors=0,
        first_at=None,
        last_at=None,
        daily=(),
    )


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:  # 平台不支持 chmod 时不影响统计
        pass
