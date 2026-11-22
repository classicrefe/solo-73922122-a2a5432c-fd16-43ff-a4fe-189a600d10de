"""SQLite 连接、建表，以及旧版库的原地升级。

现行存储模型（每个全图坐标只存一份高程）：

  basemap        单幅底图元数据（行列数）
  elevation      全图高程点，主键 (row, col) —— 共边点、四块交会的角点都只一行
  tile_versions  每块瓦片的现行版本号
  staged         按块暂存的修测快照（17×17，暂存期间仍保留重复共边）

旧版把完整 17×17（含重复共边）JSON 存在 tiles 表。启动时在**一个
BEGIN IMMEDIATE 事务**内把 tiles 拆成全图高程点 + 每块版本号：校验瓦片集合、
17×17 形状与所有重复点一致后，才删旧表并置 user_version。任何一步失败都会
整体回滚，旧库原样保留，下次启动可重试。

并发约定：写事务用 BEGIN IMMEDIATE；WAL 模式让读事务看到一致性快照。
"""

from __future__ import annotations

import json
import os
import sqlite3

from .solver import TILE

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 30000

# 新库建表语句。逐条 execute（不用 executescript），确保 DDL 也在显式事务里，
# 升级失败时能连同新建的表一起回滚。
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS basemap (
        id         INTEGER PRIMARY KEY CHECK (id = 1),  -- 单幅底图，恒为 1
        rows       INTEGER NOT NULL,
        cols       INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS elevation (
        row   INTEGER NOT NULL,  -- 全图行坐标，0 .. 16*rows
        col   INTEGER NOT NULL,  -- 全图列坐标，0 .. 16*cols
        value INTEGER NOT NULL,
        PRIMARY KEY (row, col)   -- 一个全图坐标只保存一份现行高程
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tile_versions (
        row     INTEGER NOT NULL,
        col     INTEGER NOT NULL,
        version INTEGER NOT NULL,  -- 现行版本号，每次换版 +1
        PRIMARY KEY (row, col)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS staged (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 同时也是"最新"的判定依据
        row        INTEGER NOT NULL,
        col        INTEGER NOT NULL,
        data       TEXT NOT NULL,  -- 17×17 整数高程，JSON；暂存期允许重复共边
        created_at TEXT NOT NULL
    )
    """,
)


class SchemaUpgradeError(RuntimeError):
    """旧库无法安全升级。调用方已回滚，旧库仍可修复后重试。"""


def connect(db_path: str) -> sqlite3.Connection:
    """新建连接。isolation_level=None 进入自动提交模式，事务由 SQL 显式控制。"""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _table_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def _column_names(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _create_new_schema(conn) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _decode_legacy_tile(raw, tile_label):
    """解析并校验旧库里的 17×17 JSON。返回二维整数列表。"""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SchemaUpgradeError(
            f"旧库瓦片 {tile_label} 的 data 不是合法 JSON") from exc
    if (not isinstance(data, list) or len(data) != TILE
            or any(not isinstance(row, list) or len(row) != TILE for row in data)):
        raise SchemaUpgradeError(
            f"旧库瓦片 {tile_label} 的形状不是 {TILE}×{TILE}")
    if any(type(v) is not int for row in data for v in row):
        raise SchemaUpgradeError(f"旧库瓦片 {tile_label} 含非整数高程")
    return data


def _migrate_legacy(conn) -> None:
    """把旧 tiles(row,col,version,data) 拆成 elevation + tile_versions。

    调用前已开启写事务。先建新表；校验或写入失败时由调用方 ROLLBACK，
    新建的表和 user_version 都会一起撤销。
    """
    _create_new_schema(conn)

    bm = conn.execute("SELECT rows, cols FROM basemap WHERE id = 1").fetchone()
    points: dict[tuple[int, int], int] = {}
    versions: dict[tuple[int, int], int] = {}

    if bm is None:
        # 空库（旧版也允许只有空表）。旧表若有数据则视为损坏，拒绝升级。
        if conn.execute("SELECT COUNT(*) AS n FROM tiles").fetchone()["n"]:
            raise SchemaUpgradeError("旧库没有 basemap，但 tiles 表非空")
    else:
        rows, cols = bm["rows"], bm["cols"]
        if type(rows) is not int or type(cols) is not int or rows < 1 or cols < 1:
            raise SchemaUpgradeError("旧库 basemap 的行列数非法")
        expected_tiles = {(r, c) for r in range(rows) for c in range(cols)}
        seen_tiles: set[tuple[int, int]] = set()

        for tile in conn.execute(
                "SELECT row, col, version, data FROM tiles ORDER BY row, col"):
            t = (tile["row"], tile["col"])
            if t not in expected_tiles:
                raise SchemaUpgradeError(f"旧库瓦片坐标 {t} 超出底图范围")
            if t in seen_tiles:
                raise SchemaUpgradeError(f"旧库瓦片 {t} 重复")
            seen_tiles.add(t)
            version = tile["version"]
            if type(version) is not int:
                raise SchemaUpgradeError(f"旧库瓦片 {t} 的版本号非法")
            data = _decode_legacy_tile(tile["data"], t)
            versions[t] = version
            for i in range(TILE):
                for j in range(TILE):
                    point = (16 * t[0] + i, 16 * t[1] + j)
                    value = data[i][j]
                    previous = points.get(point)
                    if previous is not None:
                        # 相邻瓦片重复保存的共边/角点必须一致，否则无法无损
                        # 拆成一份全图坐标，拒绝升级并回滚。
                        if previous != value:
                            raise SchemaUpgradeError(
                                f"旧库在共边 {point} 处存在不同高程 "
                                f"{previous} / {value}，无法合并为单份全图高程")
                        continue
                    points[point] = value

        if seen_tiles != expected_tiles:
            missing = sorted(expected_tiles - seen_tiles)
            raise SchemaUpgradeError(f"旧库瓦片集合不完整，缺少 {missing[:8]}")

        grid_rows, grid_cols = 16 * rows + 1, 16 * cols + 1
        if len(points) != grid_rows * grid_cols:
            raise SchemaUpgradeError(
                f"旧库高程点数量不符：期望 {grid_rows * grid_cols}，"
                f"实际 {len(points)}")

        conn.executemany(
            "INSERT INTO elevation (row, col, value) VALUES (?, ?, ?)",
            [(r, c, points[(r, c)]) for (r, c) in sorted(points)])
        conn.executemany(
            "INSERT INTO tile_versions (row, col, version) VALUES (?, ?, ?)",
            [(r, c, versions[(r, c)]) for (r, c) in sorted(versions)])

    # 数据已完整搬入新表，删除旧表；随后设置 user_version，最后一起提交。
    conn.execute("DROP TABLE tiles")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _upgrade(conn) -> None:
    """检查库版本并在写事务内升级。失败回滚，旧库保持可重试状态。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        tables = _table_names(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if not tables:
            _create_new_schema(conn)
        else:
            if version > SCHEMA_VERSION:
                raise SchemaUpgradeError(
                    f"数据库 schema 版本 {version} 高于本程序支持的 "
                    f"{SCHEMA_VERSION}，请使用更新版本的服务")
            if "basemap" not in tables:
                raise SchemaUpgradeError("无法识别的数据库：缺少 basemap 表")

            legacy_tiles = ("tiles" in tables
                            and "data" in _column_names(conn, "tiles"))
            new_schema = ("elevation" in tables
                          and "tile_versions" in tables)
            if legacy_tiles and ("elevation" in tables
                                 or "tile_versions" in tables):
                raise SchemaUpgradeError(
                    "数据库同时存在旧 tiles 表和新表，无法判断升级状态")
            if legacy_tiles:
                if version >= SCHEMA_VERSION:
                    raise SchemaUpgradeError(
                        "数据库版本号已是新版，但仍存在旧 tiles 表")
                _migrate_legacy(conn)
            elif new_schema:
                # 已是新结构；补齐可能缺失的辅助表并校准版本号。
                for statement in SCHEMA_STATEMENTS:
                    conn.execute(statement)
                if version != SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                raise SchemaUpgradeError(
                    "无法识别的数据库：既没有旧版 tiles 表，也没有新版高程表")

        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def init_db(db_path: str) -> None:
    """建目录、开 WAL，并按需自动升级旧库。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        _upgrade(conn)
    finally:
        conn.close()
