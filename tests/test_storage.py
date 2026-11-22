"""存储模型测试：全图坐标唯一、四块角点共享、局部发版只写受影响记录。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.solver import TILE
from tests.conftest import full_grid, init_map, stage_tile


@pytest.fixture()
def db_app(tmp_path):
    db_path = tmp_path / "storage.db"
    app = create_app(str(db_path))
    with TestClient(app) as client:
        yield client, db_path


def _connect(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _audit_triggers(conn):
    """统计发版事务里 elevation / tile_versions 的 UPDATE 次数。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit (kind TEXT NOT NULL);
        DELETE FROM audit;
        CREATE TRIGGER IF NOT EXISTS audit_elev AFTER UPDATE ON elevation
        BEGIN INSERT INTO audit (kind) VALUES ('elevation'); END;
        CREATE TRIGGER IF NOT EXISTS audit_ver AFTER UPDATE ON tile_versions
        BEGIN INSERT INTO audit (kind) VALUES ('version'); END;
    """)


def _audit_counts(conn):
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM audit GROUP BY kind").fetchall()
    return {r["kind"]: r["n"] for r in rows}


def test_grid_creates_one_row_per_global_coordinate(db_app):
    client, db_path = db_app
    grid = full_grid(2, 3, 100)
    # 让每个全图坐标有可区分的值，验证去重后取到的就是该坐标的值
    for r in range(len(grid)):
        for c in range(len(grid[0])):
            grid[r][c] = 1000 + 10 * r + c
    assert client.post("/basemap", json={"grid": grid}).status_code == 201

    conn = _connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM elevation").fetchone()[0] == 33 * 49
        assert conn.execute(
            "SELECT COUNT(*) FROM (SELECT row, col FROM elevation "
            "GROUP BY row, col HAVING COUNT(*) > 1)").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tile_versions").fetchone()[0] == 6
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        value = conn.execute(
            "SELECT value FROM elevation WHERE row = 16 AND col = 16"
        ).fetchone()[0]
        assert value == grid[16][16]
    finally:
        conn.close()

    # 四块围绕 (16,16) 的瓦片各自返回同一物理点的同一个值
    for row, col, i, j in ((0, 0, 16, 16), (0, 1, 16, 0),
                           (1, 0, 0, 16), (1, 1, 0, 0)):
        tile = client.get(f"/tiles/{row}/{col}").json()
        assert tile["data"][i][j] == grid[16][16]


def test_single_tile_fetch_returns_full_tile(db_app):
    client, _ = db_app
    init_map(client, 3, 3, 100)
    for row in (0, 1, 2):
        for col in (0, 1, 2):
            body = client.get(f"/tiles/{row}/{col}").json()
            assert body["version"] == 1
            assert len(body["data"]) == TILE
            assert all(len(r) == TILE for r in body["data"])
            assert all(v == 100 for r in body["data"] for v in r)

    # 单块查询只落在该块的坐标范围上（使用主键索引，不整表扫描）
    conn = _connect(_)
    try:
        plan = " ".join(
            r["detail"] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT row, col, value FROM elevation "
                "WHERE row BETWEEN ? AND ? AND col BETWEEN ? AND ?",
                (16, 32, 16, 32)))
    finally:
        conn.close()
    assert "SEARCH ELEVATION USING" in plan.upper()
    assert "SCAN ELEVATION" not in plan.upper()


def test_publish_updates_only_affected_elevation_and_versions(db_app):
    client, db_path = db_app
    init_map(client, 2, 2, 100)
    conn = _connect(db_path)
    try:
        _audit_triggers(conn)
        t = [[100] * TILE for _ in range(TILE)]
        t[8][8] = 333  # 只改内部一点
        stage_tile(client, 0, 0, t)
        body = client.post("/publish", json={"row": 0, "col": 0}).json()
        assert body["updated_elevation_points"] == 1
        assert body["changed_tiles"] == [{"row": 0, "col": 0, "version": 2}]
        assert _audit_counts(conn) == {"elevation": 1, "version": 1}

        # 共边修改：只写真正变化的全图点，共边点只写一份
        conn.execute("DELETE FROM audit")
        t0 = [[100] * TILE for _ in range(TILE)]
        t1 = [[100] * TILE for _ in range(TILE)]
        t0[3][16] = 400
        t1[3][0] = 400
        stage_tile(client, 0, 0, t0)
        stage_tile(client, 0, 1, t1)
        body = client.post("/publish", json={"row": 0, "col": 0}).json()
        assert body["updated_elevation_points"] == 2
        assert _audit_counts(conn) == {"elevation": 2, "version": 2}

        # 整条共边 17 点一起改：17 条高程记录各写一次（两块各覆盖一遍）、
        # 两块版本各升一次
        client.delete("/basemap")
        init_map(client, 1, 2, 100)
        _audit_triggers(conn)
        t0 = [[100] * TILE for _ in range(TILE)]
        t1 = [[100] * TILE for _ in range(TILE)]
        for i in range(TILE):
            t0[i][16] = 400
            t1[i][0] = 400
        stage_tile(client, 0, 0, t0)
        stage_tile(client, 0, 1, t1)
        body = client.post("/publish", json={"row": 0, "col": 0}).json()
        assert body["updated_elevation_points"] == TILE
        assert _audit_counts(conn) == {"elevation": TILE, "version": 2}
    finally:
        conn.close()


def test_four_tile_corner_is_single_record(db_app):
    client, db_path = db_app
    init_map(client, 2, 2, 100)

    # 四块修测都把同一物理角点 (16,16) 改成 777，共边因此仍逐点吻合
    corners = {(0, 0): (16, 16), (0, 1): (16, 0),
               (1, 0): (0, 16), (1, 1): (0, 0)}
    for (r, c), (i, j) in corners.items():
        t = [[100] * TILE for _ in range(TILE)]
        t[i][j] = 777
        stage_tile(client, r, c, t)

    body = client.post("/publish", json={"row": 0, "col": 0}).json()
    assert {(t["row"], t["col"]) for t in body["changed_tiles"]} == {
        (0, 0), (0, 1), (1, 0), (1, 1)}
    assert body["updated_elevation_points"] == 1  # 角点只有一份记录

    conn = _connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM elevation WHERE row = 16 AND col = 16"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT value FROM elevation WHERE row = 16 AND col = 16"
        ).fetchone()[0] == 777
    finally:
        conn.close()

    for (r, c), (i, j) in corners.items():
        assert client.get(f"/tiles/{r}/{c}").json()["data"][i][j] == 777
    assert client.get("/basemap/window", params={
        "row0": 16, "col0": 16, "row1": 16, "col1": 16}).json()["grid"] == [[777]]


def test_conflict_keeps_elevation_and_versions_untouched(db_app):
    client, db_path = db_app
    init_map(client, 1, 2, 100)
    sid = stage_tile(client, 0, 0, [[100] * 16 + [200] for _ in range(TILE)])
    assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 409

    conn = _connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM elevation WHERE value != 100").fetchone()[0] == 0
        assert conn.execute(
            "SELECT DISTINCT version FROM tile_versions").fetchall()[0][0] == 1
    finally:
        conn.close()
    assert [s["staged_id"] for s in
            client.get("/tiles/0/0/staged").json()["items"]] == [sid]
