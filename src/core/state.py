"""SQLite 状态库：断点续传 + 内容指纹去重 + 单日熔断计数。

设计要点：
- WAL 模式 + 线程锁（GUI QThread 与主线程并发安全）
- (账号, 内容类型, 内容指纹) 唯一 → 跨场次永不重发
- published 状态永不回退（安全红线的一部分）
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Optional

from ..constants import STATE_DB_PATH

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS publish_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT    NOT NULL,
    content_type TEXT    NOT NULL,
    title        TEXT    NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    detail       TEXT    NOT NULL DEFAULT '',
    evidence     TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    published_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup
    ON publish_records(account, content_type, content_hash);
CREATE INDEX IF NOT EXISTS idx_account_date
    ON publish_records(account, created_at);
CREATE TABLE IF NOT EXISTS profile_registry (
    profile      TEXT PRIMARY KEY,
    nickname     TEXT NOT NULL DEFAULT '',
    last_login   TEXT NOT NULL DEFAULT '',
    login_count  INTEGER NOT NULL DEFAULT 0
);
"""


def content_hash(*parts: str) -> str:
    """内容指纹：任意字符串拼接 → SHA256。"""
    joined = "|".join(p.strip() for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class StateDB:
    """线程安全的状态库。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else STATE_DB_PATH
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            logger.error("状态库初始化失败: %s", exc)
            raise

    def is_published(self, account: str, ctype: str, chash: str) -> bool:
        """该内容是否已成功发布过（跨场次去重）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM publish_records "
                "WHERE account=? AND content_type=? AND content_hash=? AND status='published'",
                (account, ctype, chash),
            ).fetchone()
        return row is not None

    def today_published_count(self, account: str, today: Optional[date] = None) -> int:
        """该账号今日已发布数（熔断计数依据）。"""
        d = (today or date.today()).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM publish_records "
                "WHERE account=? AND status='published' AND date(created_at)=?",
                (account, d),
            ).fetchone()
        return int(row[0]) if row else 0

    def upsert(
        self,
        *,
        account: str,
        ctype: str,
        chash: str,
        title: str,
        status: str,
        detail: str = "",
        evidence: str = "",
    ) -> None:
        """写入/更新一条记录。published 状态永不回退（红线）。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM publish_records "
                "WHERE account=? AND content_type=? AND content_hash=?",
                (account, ctype, chash),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO publish_records "
                    "(account, content_type, content_hash, title, status, detail, evidence) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (account, ctype, chash, title, status, detail, evidence),
                )
            elif row["status"] != "published":
                conn.execute(
                    "UPDATE publish_records SET title=?, status=?, detail=?, evidence=? "
                    "WHERE account=? AND content_type=? AND content_hash=?",
                    (title, status, detail, evidence, account, ctype, chash),
                )
            conn.commit()

    def mark_published(self, *, account: str, ctype: str, chash: str,
                       title: str, evidence: str = "") -> None:
        conn_published_at = _now_local()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE publish_records SET status='published', published_at=?, evidence=?, title=? "
                "WHERE account=? AND content_type=? AND content_hash=?",
                (conn_published_at, evidence, title, account, ctype, chash),
            )
            conn.commit()

    def today_records(self, today: Optional[date] = None) -> list[sqlite3.Row]:
        """今日全部记录（日报用）。"""
        d = (today or date.today()).isoformat()
        with self._lock, self._connect() as conn:
            return list(conn.execute(
                "SELECT account, content_type, title, status, detail, published_at "
                "FROM publish_records WHERE date(created_at)=? ORDER BY id",
                (d,),
            ))

    # ---------- profile 注册表（多账号 cookie 持久化档案）----------

    def register_profile(self, profile: str, nickname: str) -> None:
        """登记/更新账号 profile（次日按名单免扫码复用 cookie）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO profile_registry (profile, nickname, last_login, login_count) "
                "VALUES (?,?,?,1) "
                "ON CONFLICT(profile) DO UPDATE SET "
                "nickname=excluded.nickname, last_login=excluded.last_login, "
                "login_count=login_count+1",
                (profile, nickname, _now_local()),
            )
            conn.commit()

    def list_profiles(self) -> list[tuple[str, str]]:
        """已登记的全部 (profile, nickname)。最近登录的优先（cookie 更可能有效）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT profile, nickname FROM profile_registry "
                "ORDER BY last_login DESC"
            ).fetchall()
        return [(r["profile"], r["nickname"]) for r in rows]


def _now_local() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
