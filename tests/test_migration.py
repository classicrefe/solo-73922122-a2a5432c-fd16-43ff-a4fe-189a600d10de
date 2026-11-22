"""旧版 SQLite 库自动升级：保留底图/版本/暂存，失败回滚且可重试。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import SchemaUpgradeError, init_db
from app.main import create_app
from app.solver import TILE
from tests.conftest import full_grid, stage_tile

# 旧版（重复保存共边）的表结构
LEGACY_SCHEMA = """
CREATE TABLE basemap (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    rows       INTEGER NOT NULL,
    cols       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tiles (
    row     INTEGER NOT NULL,
    col     INTEGER NOT NULL,
    version INTEGER NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (row, col)
);
CREATE TABLE staged (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    row        INTEGER NOT NULL,
    col        INTEGER NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _legacy_tile_grid(rows, cols, value=100):
    """按旧版语义切块：每块 17×17，共边在两块中各存一份。"""
    grid = full_grid(rows, cols, value)
    return {(r, c): [row[16 * c:16 * c + TILE]
                     for row in grid[16 * r:16 * r + TILE]]
            for r in range(rows) for c in range(cols)}


def make_legacy_db(path, rows=2, cols=2, value=100, versions=None,
                   staged=(), override=None):
    """构造一个旧版数据库；override 可替换某块数据以模拟损坏库。"""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO basemap (id, rows, cols, created_at) VALUES (1,?,?,?)",
            (rows, cols, "2026-01-01T00:00:00+00:00"))
        tiles = _legacy_tile_grid(rows, cols, value)
        if override:
            override(tiles)
        versions = versions or {}
        conn.executemany(
            "INSERT INTO tiles (row, col, version, data) VALUES (?,?,?,?)",
            [(r, c, versions.get((r, c), 1), json.dumps(tiles[(r, c)]))
             for (r, c) in sorted(tiles)])
        conn.executemany(
            "INSERT INTO staged (row, col, data, created_at) VALUES (?,?,?,?)",
            [(r, c, json.dumps(data), "2026-01-02T00:00:00+00:00")
             for (r, c, data) in staged])
    finally:
        conn.close()
    return path


def _inspect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
        return tables, conn.execute("PRAGMA user_version").fetchone()[0], {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables}
    finally:
        conn.close()


def test_upgrade_preserves_basemap_versions_and_staged(tmp_path):
    t = [[250] * TILE for _ in range(TILE)]
    t[5][5] = 42
    path = make_legacy_db(tmp_path / "old.db", rows=2, cols=3, value=250,
                          versions={(0, 0): 4, (1, 2): 7},
                          staged=[(0, 0, t)])
    app = create_app(str(path))
    with TestClient(app) as client:
        info = client.get("/basemap").json()
        assert info["rows"] == 2 and info["cols"] == 3
        assert info["versions"] == [[4, 1, 1], [1, 1, 7]]
        assert info["staged_counts"][0][0] == 1
        assert client.get("/tiles/0/0/staged").json()["count"] == 1

        tile = client.get("/tiles/1/2").json()
        assert tile["version"] == 7
        assert all(v == 250 for row in tile["data"] for v in row)

        grid = client.get("/basemap/grid").json()
        assert grid["grid_rows"] == 33 and grid["grid_cols"] == 49
        assert all(v == 250 for row in grid["grid"] for v in row)

        # 升级后照常发版：保留的暂存仍可用
        assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 200
        assert client.get("/tiles/0/0").json()["version"] == 5

    tables, version, counts = _inspect(path)
    assert "tiles" not in tables
    assert {"basemap", "elevation", "tile_versions", "staged"} <= tables
    assert version == 1
    assert counts["elevation"] == 33 * 49
    assert counts["tile_versions"] == 6


def test_upgrade_merges_duplicate_edge_points(tmp_path):
    path = make_legacy_db(tmp_path / "old.db", rows=1, cols=2, value=100)
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        # 1×2 共 33×17 个全图坐标，而不是两块各 289 个
        assert conn.execute("SELECT COUNT(*) FROM elevation").fetchone()[0] == 33 * 17
        assert conn.execute(
            "SELECT COUNT(*) FROM elevation WHERE row = 0 AND col = 16"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_upgrade_is_idempotent(tmp_path):
    path = make_legacy_db(tmp_path / "old.db", rows=1, cols=1)
    init_db(str(path))
    init_db(str(path))
    tables, version, counts = _inspect(path)
    assert version == 1
    assert counts["elevation"] == 17 * 17


@pytest.mark.parametrize("corruption", ["seam", "shape", "missing_tile", "bad_json"])
def test_broken_legacy_db_rolls_back_and_stays_retryable(tmp_path, corruption):
    def corrupt(tiles):
        if corruption == "seam":
            tiles[(0, 1)][3][0] = 999          # 与左块重复共边矛盾
        elif corruption == "shape":
            tiles[(0, 1)] = tiles[(0, 1)][:16]
        elif corruption == "missing_tile":
            del tiles[(1, 1)]
        else:
            tiles[(0, 0)] = None

    path = make_legacy_db(tmp_path / "old.db", rows=2, cols=2, override=corrupt)
    if corruption == "bad_json":
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("UPDATE tiles SET data = '{not json' WHERE row=0 AND col=0")
        conn.close()

    before = _inspect(path)
    with pytest.raises(SchemaUpgradeError):
        init_db(str(path))
    after = _inspect(path)

    # 失败后旧库原样保留：旧表、旧数据、版本号都没变
    assert after == before
    assert "tiles" in after[0] and "elevation" not in after[0]
    assert after[1] == 0

    # 修复后重新启动即可升级成功
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        if corruption == "seam":
            conn.execute("UPDATE tiles SET data = ? WHERE row=0 AND col=1",
                         (json.dumps(_legacy_tile_grid(2, 2)[(0, 1)]),))
        elif corruption == "shape":
            conn.execute("UPDATE tiles SET data = ? WHERE row=0 AND col=1",
                         (json.dumps(_legacy_tile_grid(2, 2)[(0, 1)]),))
        elif corruption == "missing_tile":
            conn.execute("INSERT INTO tiles (row, col, version, data) "
                         "VALUES (1, 1, 1, ?)",
                         (json.dumps(_legacy_tile_grid(2, 2)[(1, 1)]),))
        else:
            conn.execute("UPDATE tiles SET data = ? WHERE row=0 AND col=0",
                         (json.dumps(_legacy_tile_grid(2, 2)[(0, 0)]),))
    finally:
        conn.close()

    init_db(str(path))
    tables, version, counts = _inspect(path)
    assert "tiles" not in tables and version == 1
    assert counts["elevation"] == 33 * 33


def test_refuses_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    init_db(str(path))
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaUpgradeError):
        init_db(str(path))


def test_empty_legacy_db_upgrades_to_empty_new_schema(tmp_path):
    path = make_legacy_db(tmp_path / "empty.db")  # 先造旧结构
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("DELETE FROM tiles")
    conn.execute("DELETE FROM basemap")
    conn.close()

    with TestClient(create_app(str(path))) as client:
        r = client.get("/basemap")
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "no_basemap"
        assert client.post("/basemap", json={
            "grid": full_grid(1, 1)}).status_code == 201
    tables, version, _ = _inspect(path)
    assert "tiles" not in tables and version == 1
