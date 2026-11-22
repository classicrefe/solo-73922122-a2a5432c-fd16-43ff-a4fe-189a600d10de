"""API 行为测试：录入、暂存、发版、取数全流程。"""

from __future__ import annotations

from tests.conftest import (full_grid, init_map, set_left_edge,
                            set_right_edge, stage_tile, staged_ids, tile_grid)


# ---- 底图录入 ------------------------------------------------------------

def test_init_from_grid_and_fetch(client):
    init_map(client, 2, 3, 100)
    info = client.get("/basemap").json()
    assert info["rows"] == 2 and info["cols"] == 3
    assert info["versions"] == [[1, 1, 1], [1, 1, 1]]

    grid = client.get("/basemap/grid").json()
    assert grid["grid_rows"] == 33 and grid["grid_cols"] == 49
    assert all(v == 100 for row in grid["grid"] for v in row)

    tile = client.get("/tiles/1/2").json()
    assert tile["version"] == 1 and len(tile["data"]) == 17


def test_init_from_tiles_ok(client):
    tiles = [{"row": r, "col": c, "data": tile_grid(50 + 10 * r + c)}
             for r in range(2) for c in range(2)]
    # 把共边对齐：右边块的左列 = 左边块的右列，下边块的首行 = 上边块的末行
    by = {(t["row"], t["col"]): t["data"] for t in tiles}
    for r in range(2):
        for i in range(17):
            by[(r, 1)][i][0] = by[(r, 0)][i][16]
    for c in range(2):
        for j in range(17):
            by[(1, c)][0][j] = by[(0, c)][16][j]
    r = client.post("/basemap",
                    json={"rows": 2, "cols": 2, "tiles": tiles})
    assert r.status_code == 201, r.text


def test_init_seam_mismatch_rejected(client):
    tiles = [{"row": 0, "col": 0, "data": tile_grid(100)},
             {"row": 0, "col": 1, "data": tile_grid(100)}]
    tiles[1]["data"][3][0] = 999  # 破坏共边
    r = client.post("/basemap",
                    json={"rows": 1, "cols": 2, "tiles": tiles})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "seam_mismatch"
    assert detail["mismatches"][0]["tile_a"] == {"row": 0, "col": 0}
    assert detail["mismatches"][0]["tile_b"] == {"row": 0, "col": 1}
    assert 3 in detail["mismatches"][0]["mismatched_points"]


def test_reinit_conflict_and_reset(client):
    init_map(client, 1, 1)
    r = client.post("/basemap", json={"grid": full_grid(1, 1)})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "basemap_exists"
    assert client.delete("/basemap").status_code == 200
    assert client.post("/basemap", json={"grid": full_grid(1, 1)}).status_code == 201


def test_grid_size_validation(client):
    r = client.post("/basemap", json={"grid": [[1] * 33 for _ in range(32)]})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_shape"


def test_ops_without_basemap(client):
    assert client.get("/basemap").status_code == 409
    assert client.get("/basemap/grid").status_code == 409
    assert client.post("/tiles/0/0/staged",
                       json={"data": tile_grid()}).status_code == 409
    assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 409


# ---- 暂存 ----------------------------------------------------------------

def test_stage_and_list(client):
    init_map(client, 1, 2)
    sid1 = stage_tile(client, 0, 0, tile_grid(100))
    sid2 = stage_tile(client, 0, 0, tile_grid(100))
    assert sid2 > sid1
    assert staged_ids(client, 0, 0) == [sid1, sid2]
    assert client.get("/staged").json()["count"] == 2


def test_stage_bad_shape(client):
    init_map(client, 1, 1)
    r = client.post("/tiles/0/0/staged", json={"data": [[1] * 17] * 16})
    assert r.status_code == 422


def test_stage_out_of_range(client):
    init_map(client, 1, 1)
    r = client.post("/tiles/0/1/staged", json={"data": tile_grid()})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "tile_out_of_range"


# ---- 发版 ----------------------------------------------------------------

def test_publish_no_staged(client):
    init_map(client, 1, 1)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_staged_version"


def test_publish_interior_change_single_tile(client):
    init_map(client, 2, 2)
    t = tile_grid(100)
    t[3][3] = 5
    sid = stage_tile(client, 0, 0, t)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed_tiles"] == [{"row": 0, "col": 0, "version": 2}]
    assert body["cleared_staged_ids"] == [sid]
    assert staged_ids(client, 0, 0) == []
    assert client.get("/tiles/0/0").json()["data"][3][3] == 5
    assert client.get("/tiles/0/0").json()["version"] == 2


def test_publish_propagates_and_grid_consistent(client):
    init_map(client, 1, 2)
    t0, t1 = tile_grid(100), tile_grid(100)
    set_right_edge(t0, 200)
    set_left_edge(t1, 200)
    stage_tile(client, 0, 0, t0)
    stage_tile(client, 0, 1, t1)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 200, r.text
    changed = {(t["row"], t["col"]) for t in r.json()["changed_tiles"]}
    assert changed == {(0, 0), (0, 1)}
    grid = client.get("/basemap/grid").json()["grid"]
    assert all(row[16] == 200 for row in grid)  # 共边整列更新


def test_publish_minimal_does_not_touch_uninvolved(client):
    init_map(client, 2, 2)
    t00, t11 = tile_grid(100), tile_grid(100)
    t00[1][1] = 5
    t11[2][2] = 9
    stage_tile(client, 0, 0, t00)
    sid11 = stage_tile(client, 1, 1, t11)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert {(t["row"], t["col"]) for t in r.json()["changed_tiles"]} == {(0, 0)}
    assert client.get("/tiles/1/1").json()["version"] == 1
    assert staged_ids(client, 1, 1) == [sid11]  # 未用到的暂存保留


def test_publish_clears_only_used_staged(client):
    init_map(client, 1, 1)
    t1, t2 = tile_grid(100), tile_grid(100)
    t1[0][0] = 1
    t2[0][0] = 2
    sid1 = stage_tile(client, 0, 0, t1)
    sid2 = stage_tile(client, 0, 0, t2)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.json()["cleared_staged_ids"] == [sid2]  # 只用最新版
    assert staged_ids(client, 0, 0) == [sid1]        # 旧版保留
    assert client.get("/tiles/0/0").json()["data"][0][0] == 2


def test_publish_conflict_reports_edges_and_preserves_state(client):
    init_map(client, 1, 2)
    t0 = tile_grid(100)
    set_right_edge(t0, 200)  # 邻块没有能接上的暂存版
    sid = stage_tile(client, 0, 0, t0)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "publish_conflict"
    assert detail["conflicting_edges"] == [
        {"tile_a": {"row": 0, "col": 0}, "tile_b": {"row": 0, "col": 1}}]
    # 现行底图与暂存原样保留
    assert client.get("/tiles/0/0").json()["version"] == 1
    assert staged_ids(client, 0, 0) == [sid]
    grid = client.get("/basemap/grid").json()["grid"]
    assert all(v == 100 for row in grid for v in row)


def test_publish_chain(client):
    init_map(client, 1, 3)
    t0, t1, t2 = tile_grid(100), tile_grid(100), tile_grid(100)
    set_right_edge(t0, 200)
    set_left_edge(t1, 200)
    set_right_edge(t1, 300)
    set_left_edge(t2, 300)
    stage_tile(client, 0, 0, t0)
    stage_tile(client, 0, 1, t1)
    stage_tile(client, 0, 2, t2)
    r = client.post("/publish", json={"row": 0, "col": 0})
    changed = {(t["row"], t["col"]) for t in r.json()["changed_tiles"]}
    assert changed == {(0, 0), (0, 1), (0, 2)}
    info = client.get("/basemap").json()
    assert info["versions"] == [[2, 2, 2]]


# ---- 取数 ----------------------------------------------------------------

def test_window_query(client):
    init_map(client, 2, 2)
    r = client.get("/basemap/window",
                   params={"row0": 15, "col0": 15, "row1": 17, "col1": 17})
    assert r.status_code == 200
    body = r.json()
    assert len(body["grid"]) == 3 and len(body["grid"][0]) == 3

    t = tile_grid(100)
    t[8][8] = 777  # 内部点 -> 整图坐标 (8,8)，不触碰共边
    stage_tile(client, 0, 0, t)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 200, r.text
    r = client.get("/basemap/window",
                   params={"row0": 8, "col0": 8, "row1": 8, "col1": 8})
    assert r.json()["grid"] == [[777]]


def test_window_out_of_range(client):
    init_map(client, 1, 1)
    r = client.get("/basemap/window",
                   params={"row0": 0, "col0": 0, "row1": 17, "col1": 0})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "window_out_of_range"
