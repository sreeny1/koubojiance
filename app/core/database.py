"""SQLite 数据层。

设计要点：
- 使用标准库 sqlite3（零额外依赖），WAL 模式提升并发读写稳定性；
- 所有连接 check_same_thread=False + 全局写锁，允许多线程 API 与工作线程共用；
- schema_version 表支持后续平滑升级；
- 首次启动自动建库并写入种子违禁词库（可在界面随意增删改）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,          -- 磁盘绝对路径
    filename TEXT NOT NULL,
    duration_ms INTEGER,                -- 转写后回填
    created_at TEXT DEFAULT (datetime('now','localtime')),
    transcribed_at TEXT                 -- 最近一次转写完成时间
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/error/canceled
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,               -- 段序号
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(video_id);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    color TEXT DEFAULT '#e5484d'
);
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    word TEXT NOT NULL,
    note TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(category_id, word)
);
CREATE TABLE IF NOT EXISTS hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    word_id INTEGER NOT NULL,
    word_text TEXT NOT NULL,            -- 词库中的标准词
    matched_text TEXT NOT NULL,         -- 文本中实际命中的原文（可能是谐音变体）
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    sentence TEXT NOT NULL              -- 命中所在完整字幕句
);
CREATE INDEX IF NOT EXISTS idx_hits_video ON hits(video_id);
CREATE TABLE IF NOT EXISTS cut_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    hit_ids TEXT NOT NULL,              -- JSON 数组：选中的命中 id
    pad REAL DEFAULT 0.3,               -- 命中前后缓冲（秒）
    status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/error/canceled
    error TEXT,
    progress REAL DEFAULT 0,
    backup_path TEXT,                   -- 去词前原文件备份路径
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cut_jobs_video ON cut_jobs(video_id);
"""

# 种子违禁词库：覆盖常见的广告法极限词 / 医疗功效 / 金融风险 / 平台敏感
# 仅供起步，用户可在"词库管理"页自由增删。
_SEED_CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("广告法极限词", "#e5484d", [
        "最好", "最佳", "最优", "最强", "最先进", "最高级", "最低价", "最便宜",
        "第一", "唯一", "首个", "首选", "全网第一", "全国第一", "销量第一",
        "顶级", "极品", "绝无仅有", "史无前例", "万能", "完美", "永久",
        "绝对", "百分百", "100%有效", "国家级", "世界级", "宇宙级",
        "王牌", "尖端", "顶级享受", "极致", "空前绝后", "无敌",
        "领导品牌", "遥遥领先", "业界第一", "排名第一位",
    ]),
    ("医疗健康功效", "#f76b15", [
        "根治", "治愈", "疗效", "药到病除", "立竿见影", "包治百病",
        "无效退款", "延年益寿", "抗癌", "消炎杀菌", "活血化瘀",
        "降三高", "彻底清除", "清除自由基", "医学认可", "医院推荐",
        "最高医术", "药王", "神药", "偏方治愈",
    ]),
    ("投资金融风险", "#8e4ec6", [
        "稳赚不赔", "保本保息", "零风险", "无风险", "绝对收益",
        "一夜暴富", "躺赚", "躺著赚钱", "日赚千元", "月入十万",
        "内部消息", "必涨", "翻倍", "财富密码", "免费送钱",
    ]),
    ("平台违禁内容", "#2f9e44", [
        "加微信", "加V", "私信我", "私聊", "点击链接", "扫码进群",
        "货到付款", "先验货后付款", "代购", "刷单", "兼职日结",
        "外挂", "破解版", "免费领取", "点赞关注走一波",
    ]),
]


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        # WAL 模式：读写不互斥，浏览器轮询期间后台写库不阻塞
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.commit()
                self._seed_words()

    def _seed_words(self) -> None:
        """首次建库时写入种子违禁词（词库为空才写，避免覆盖用户数据）。"""
        cnt = self._conn.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]
        if cnt > 0:
            return
        for name, color, words in _SEED_CATEGORIES:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO categories(name,color) VALUES(?,?)",
                (name, color),
            )
            cat_id = self._conn.execute(
                "SELECT id FROM categories WHERE name=?", (name,)
            ).fetchone()["id"]
            self._conn.executemany(
                "INSERT OR IGNORE INTO words(category_id,word) VALUES(?,?)",
                [(cat_id, w) for w in words],
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文：自动 commit / 异常回滚。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        """执行只读查询，返回 dict 列表。"""
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """执行单条写语句，返回 lastrowid。"""
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        with self.tx() as conn:
            conn.executemany(sql, seq)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# 模块级单例（由 main.py 初始化后注入使用）
_db: Database | None = None


def init_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def get_db() -> Database:
    assert _db is not None, "数据库未初始化，请先调用 init_db()"
    return _db
