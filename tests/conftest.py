"""测试共享工具与 fixtures。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.solver import TILE


def tile_grid(value=100):
    """生成一块 17×17 全等值的瓦片。"""
    return [[value] * TILE for _ in range(TILE)]


def full_grid(rows, cols, value=100):
    """生成 (16·rows+1)×(16·cols+1) 的整图。"""
    return [[value] * (16 * cols + 1) for _ in range(16 * rows + 1)]


def set_right_edge(t, v):
    for row in t:
        row[TILE - 1] = v


def set_left_edge(t, v):
    for row in t:
        row[0] = v


def set_bottom_edge(t, v):
    t[TILE - 1] = [v] * TILE


def set_top_edge(t, v):
    t[0] = [v] * TILE


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def init_map(client, rows, cols, value=100):
    """用整图方式录入一幅全等值底图。"""
    r = client.post("/basemap", json={"grid": full_grid(rows, cols, value)})
    assert r.status_code == 201, r.text
    return r


def stage_tile(client, row, col, data):
    r = client.post(f"/tiles/{row}/{col}/staged", json={"data": data})
    assert r.status_code == 201, r.text
    return r.json()["staged_id"]


def staged_ids(client, row, col):
    r = client.get(f"/tiles/{row}/{col}/staged")
    assert r.status_code == 200, r.text
    return [it["staged_id"] for it in r.json()["items"]]
