"""发版规划器（纯函数）单元测试。"""

from __future__ import annotations

import pytest

from app.solver import NoStagedVersion, plan_publish
from tests.conftest import (set_bottom_edge, set_left_edge, set_right_edge,
                            set_top_edge, tile_grid)


def current_map(rows, cols, value=100):
    return {(r, c): tile_grid(value) for r in range(rows) for c in range(cols)}


def test_named_tile_without_staged_raises():
    with pytest.raises(NoStagedVersion):
        plan_publish(1, 1, current_map(1, 1), {}, (0, 0))


def test_interior_change_only_needs_named_tile():
    cur = current_map(2, 2)
    staged = {(0, 0): tile_grid(100)}
    staged[(0, 0)][3][3] = 5  # 只改内部点，不动边
    chosen, conflicts = plan_publish(2, 2, cur, staged, (0, 0))
    assert conflicts is None
    assert chosen == {(0, 0)}


def test_edge_change_forces_matching_neighbor():
    cur = current_map(1, 2)
    t0, t1 = tile_grid(100), tile_grid(100)
    set_right_edge(t0, 200)
    set_left_edge(t1, 200)
    chosen, conflicts = plan_publish(1, 2, cur, {(0, 0): t0, (0, 1): t1}, (0, 0))
    assert conflicts is None
    assert chosen == {(0, 0), (0, 1)}


def test_chain_propagation():
    cur = current_map(1, 3)
    t0, t1, t2 = tile_grid(100), tile_grid(100), tile_grid(100)
    set_right_edge(t0, 200)
    set_left_edge(t1, 200)
    set_right_edge(t1, 300)
    set_left_edge(t2, 300)
    staged = {(0, 0): t0, (0, 1): t1, (0, 2): t2}
    chosen, conflicts = plan_publish(1, 3, cur, staged, (0, 0))
    assert conflicts is None
    assert chosen == {(0, 0), (0, 1), (0, 2)}


def test_chain_stops_where_edge_matches_current():
    cur = current_map(1, 3)
    t0, t1, t2 = tile_grid(100), tile_grid(100), tile_grid(100)
    set_right_edge(t0, 200)
    set_left_edge(t1, 200)   # t1 右边不动，仍是现行值
    t2[5][5] = 9             # 无关的内部修测，不应被带上
    staged = {(0, 0): t0, (0, 1): t1, (0, 2): t2}
    chosen, conflicts = plan_publish(1, 3, cur, staged, (0, 0))
    assert conflicts is None
    assert chosen == {(0, 0), (0, 1)}


def test_neighbor_without_staged_blocks():
    cur = current_map(1, 2)
    t0 = tile_grid(100)
    set_right_edge(t0, 200)
    chosen, conflicts = plan_publish(1, 2, cur, {(0, 0): t0}, (0, 0))
    assert chosen is None
    assert conflicts == [((0, 0), (0, 1))]


def test_neighbor_staged_equal_current_blocks():
    # 邻块有暂存但共边仍是现行值，接不上 => 冲突
    cur = current_map(1, 2)
    t0 = tile_grid(100)
    set_right_edge(t0, 200)
    t1 = tile_grid(100)
    t1[4][4] = 7
    chosen, conflicts = plan_publish(1, 2, cur, {(0, 0): t0, (0, 1): t1}, (0, 0))
    assert chosen is None
    assert conflicts == [((0, 0), (0, 1))]


def test_both_changed_but_unequal_blocks():
    cur = current_map(1, 2)
    t0, t1 = tile_grid(100), tile_grid(100)
    set_right_edge(t0, 200)
    set_left_edge(t1, 300)
    chosen, conflicts = plan_publish(1, 2, cur, {(0, 0): t0, (0, 1): t1}, (0, 0))
    assert chosen is None
    assert conflicts == [((0, 0), (0, 1))]


def test_internal_conflict_inside_forced_component():
    # 四块被等值边连成一体，但 (1,0)-(1,1) 的暂存共边互不相等 => 整体无解
    cur = current_map(2, 2)
    t00, t01, t10, t11 = (tile_grid(100) for _ in range(4))
    set_right_edge(t00, 200)
    set_left_edge(t01, 200)
    set_bottom_edge(t00, 500)
    set_top_edge(t10, 500)
    set_bottom_edge(t01, 600)
    set_top_edge(t11, 600)
    set_right_edge(t10, 700)
    set_left_edge(t11, 999)  # 与 t10 的右边不相等
    # 中央角点是四块共享的同一个物理点，必须取同一个值
    for t, i, j in ((t00, 16, 16), (t01, 16, 0), (t10, 0, 16), (t11, 0, 0)):
        t[i][j] = 555
    staged = {(0, 0): t00, (0, 1): t01, (1, 0): t10, (1, 1): t11}
    chosen, conflicts = plan_publish(2, 2, cur, staged, (0, 0))
    assert chosen is None
    assert conflicts == [((1, 0), (1, 1))]


def test_vertical_propagation():
    cur = current_map(2, 1)
    t0, t1 = tile_grid(100), tile_grid(100)
    set_bottom_edge(t0, 400)
    set_top_edge(t1, 400)
    chosen, conflicts = plan_publish(2, 1, cur, {(0, 0): t0, (1, 0): t1}, (0, 0))
    assert conflicts is None
    assert chosen == {(0, 0), (1, 0)}


def test_uninvolved_staged_tile_not_switched():
    # 最小换块：另一块也有暂存且边吻合，但方案不应带上它
    cur = current_map(2, 2)
    t00 = tile_grid(100)
    t00[1][1] = 5
    t11 = tile_grid(100)
    t11[2][2] = 9
    chosen, conflicts = plan_publish(2, 2, cur, {(0, 0): t00, (1, 1): t11}, (0, 0))
    assert conflicts is None
    assert chosen == {(0, 0)}
