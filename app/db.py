"""SQLite 连接与建表。每次操作新开连接，配合 WAL + busy_timeout 支撑并发。"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS basemap (
    id         INTEGER PRIMARY KEY CHECK (id = 1),  -- 单幅底图，恒为 1
    rows       INTEGER NOT NULL,
    cols       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tiles (
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    version INTEGER NOT NULL,   -- 现行版本号，每次换版 +1
    data    TEXT NOT NULL,      -- 17×17 整数高程，JSON
    PRIMARY KEY (row, col)
);
CREATE TABLE IF NOT EXISTS staged (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 同时也是"最新"的判定依据
    row        INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

BUSY_TIMEOUT_MS = 30000


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。isolation_level=None 进入自动提交模式，事务由 SQL 显式控制。"""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def init_db(db_path: str) -> None:
    """建目录、开 WAL、建表。WAL 让读写不互斥，写事务之间仍严格串行。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
