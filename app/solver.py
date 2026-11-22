"""发版规划器：纯函数实现，不触碰数据库，便于单测。

问题抽象：每块瓦片只有两种选择——保留现行版，或换上该块的最新暂存版。
被点名的块必须换。约束是任意相邻块的共边 17 个点逐点相等；目标是换版块数最少。

关键观察（对每条共边，记现行值为 K，两侧暂存版在该边上的值分别为 A、B）：
  * A == K 且 B == K：两侧任意组合都吻合，无约束；
  * A != K 且 B == K：左侧永远不能换（换了右边接不上，右边又没有能接的暂存版）；
  * A == K 且 B != K：右侧永远不能换；
  * A != K 且 B != K 且 A == B：两侧必须同进退（"等值边"）；
  * A != K 且 B != K 且 A != B：两侧都不能换；
  * 一侧没有暂存版：另一侧只有当 A == K 时才能换。

因此把"等值边"做并查集连边，点名块所在的连通分量就是唯一被强制换版的最小
集合——任何可行方案都必须整个换掉它，而它之外的块全部保留现行版必然可行
（现行底图本身接缝吻合）。对该分量做全量共边校验：通过即得到换块最少的方案；
不通过则不存在任何可行方案（分量外的块换成暂存版也接不上，否则它早被等值边
连进分量了），校验发现的失配边就是要报告的冲突边。
"""

from __future__ import annotations

TILE = 17  # 每块瓦片 17×17 个高程点


class NoStagedVersion(Exception):
    """被点名的块没有任何暂存版。"""

    def __init__(self, tile):
        self.tile = tile
        super().__init__(f"tile {tile} has no staged version")


def _right(g):
    return [row[TILE - 1] for row in g]


def _left(g):
    return [row[0] for row in g]


def _bottom(g):
    return list(g[TILE - 1])


def _top(g):
    return list(g[0])


# 每个无向边只遍历一次的方向（右、下）：(行偏移, 列偏移, 本侧取边, 对侧取边)
_RIGHT_DOWN = ((0, 1, _right, _left), (1, 0, _bottom, _top))


def _in_range(rows, cols, t):
    return 0 <= t[0] < rows and 0 <= t[1] < cols


def plan_publish(rows, cols, current, staged, named):
    """计算换块最少且所有共边逐点吻合的发版方案。

    参数:
        rows, cols: 底图行列数。
        current:    {(row, col): 17×17 现行数据}，覆盖全部瓦片。
        staged:     {(row, col): 17×17 最新暂存数据}，仅含有暂存的块。
        named:      (row, col) 被点名、必须换上最新暂存版的块。

    返回:
        (chosen, None)    —— 可行；chosen 为需换版的瓦片坐标集合（含点名块）。
        (None, conflicts) —— 无解；conflicts 为冲突边列表 [((r1,c1),(r2,c2)), ...]，
                            每个元素是冲突边两侧的瓦片坐标。

    异常:
        NoStagedVersion —— 点名块没有暂存版。
    """
    if named not in staged:
        raise NoStagedVersion(named)

    # 并查集：把"等值边"两侧的块连成同进退的分量
    parent = {named: named}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # 路径压缩
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (r, c), su in staged.items():
        for dr, dc, self_edge, other_edge in _RIGHT_DOWN:
            nb = (r + dr, c + dc)
            sv = staged.get(nb)
            if sv is None or not _in_range(rows, cols, nb):
                continue
            eu, ev = self_edge(su), other_edge(sv)
            # 两侧暂存版共边相等且不等于现行值 => 换版会互相牵连
            if eu == ev and eu != self_edge(current[(r, c)]):
                union((r, c), nb)

    root = find(named)
    chosen = {t for t in current if find(t) == root}

    # 全量校验"分量内换版、分量外保留"这一候选方案的所有共边
    conflicts = []
    for r in range(rows):
        for c in range(cols):
            t = (r, c)
            for dr, dc, self_edge, other_edge in _RIGHT_DOWN:
                nb = (r + dr, c + dc)
                if not _in_range(rows, cols, nb):
                    continue
                eu = self_edge(staged[t]) if t in chosen else self_edge(current[t])
                ev = other_edge(staged[nb]) if nb in chosen else other_edge(current[nb])
                if eu != ev:
                    conflicts.append((t, nb))
    if conflicts:
        return None, conflicts
    return chosen, None


def find_seam_mismatches(rows, cols, tiles):
    """校验整幅底图接缝，返回失配边列表（用于底图录入）。"""
    mismatches = []
    for r in range(rows):
        for c in range(cols):
            t = tiles[(r, c)]
            if c + 1 < cols:
                o = tiles[(r, c + 1)]
                bad = [i for i in range(TILE) if t[i][TILE - 1] != o[i][0]]
                if bad:
                    mismatches.append({
                        "tile_a": {"row": r, "col": c},
                        "tile_b": {"row": r, "col": c + 1},
                        "mismatched_points": bad,
                    })
            if r + 1 < rows:
                o = tiles[(r + 1, c)]
                bad = [j for j in range(TILE) if t[TILE - 1][j] != o[0][j]]
                if bad:
                    mismatches.append({
                        "tile_a": {"row": r, "col": c},
                        "tile_b": {"row": r + 1, "col": c},
                        "mismatched_points": bad,
                    })
    return mismatches
