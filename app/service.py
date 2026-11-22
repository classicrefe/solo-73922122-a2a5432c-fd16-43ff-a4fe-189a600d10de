"""业务层：所有读写都在 SQLite 事务内完成。

存储模型：全图每个坐标（包括相邻瓦片共边上的点、四块交会的角点）在
elevation 表中只有一行；tile_versions 表保存各块现行版本号；staged 表保存
按块暂存的 17×17 修测快照。单块取数只按该块的坐标范围查询 elevation，
不扫描整图。

并发约定（可串行化）：
  * 写操作（录入底图、暂存、发版、重置）一律 BEGIN IMMEDIATE —— 一开始就拿到
    写锁，事务期间其它写者进不来，读者（WAL 模式）读的是一致的旧快照；
  * 读操作（整图/窗口/单块取数）用普通只读事务，看到的是一个一致性快照；
  * 发版的高程更新、版本号更新、暂存清除在同一个事务里提交，读者看到的高程
    和版本必然来自同一次提交；
  * 因此任意并发的暂存、发版、取数，其结果总等价于某种依次执行的顺序。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

from .db import connect, init_db
from .solver import TILE, NoStagedVersion, find_seam_mismatches, plan_publish


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceError(Exception):
    """业务错误，由 API 层统一转成 HTTP 响应。"""

    status_code = 500
    code = "internal_error"

    def __init__(self, message, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def detail(self):
        return {"error": self.code, "message": self.message, **self.extra}


class NoBasemap(ServiceError):
    status_code = 409
    code = "no_basemap"


class BasemapExists(ServiceError):
    status_code = 409
    code = "basemap_exists"


class TileOutOfRange(ServiceError):
    status_code = 404
    code = "tile_out_of_range"


class WindowOutOfRange(ServiceError):
    status_code = 422
    code = "window_out_of_range"


class InvalidShape(ServiceError):
    status_code = 422
    code = "invalid_shape"


class SeamMismatch(ServiceError):
    status_code = 422
    code = "seam_mismatch"


class NoStaged(ServiceError):
    status_code = 409
    code = "no_staged_version"


class PublishConflict(ServiceError):
    status_code = 409
    code = "publish_conflict"


def _check_tile_shape(data):
    if (not isinstance(data, list) or len(data) != TILE
            or any(not isinstance(r, list) or len(r) != TILE for r in data)):
        raise InvalidShape(f"tile data must be {TILE}x{TILE}")


def _grid_shape(grid):
    """校验整图并返回 (行数, 列数)。"""
    if not grid or any(not isinstance(r, list) or len(r) == 0 for r in grid):
        raise InvalidShape("grid 必须是非空的二维数组")
    gh, gw = len(grid), len(grid[0])
    if any(len(r) != gw for r in grid):
        raise InvalidShape("grid 各行长度不一致")
    return gh, gw


def _dimensions(gh, gw):
    """由整图尺寸推算瓦片行列数。"""
    if (gh - 1) % 16 != 0 or (gw - 1) % 16 != 0 or gh < 17 or gw < 17:
        raise InvalidShape("grid 尺寸必须是 (16*rows+1) × (16*cols+1)")
    return (gh - 1) // 16, (gw - 1) // 16


def _slice_tile(grid, row, col):
    return [line[16 * col:16 * col + TILE]
            for line in grid[16 * row:16 * row + TILE]]


def tiles_from_grid(grid, rows, cols):
    """把 (16·rows+1)×(16·cols+1) 的整图切成带共边重复的瓦片（仅录入时用）。"""
    gh, gw = _grid_shape(grid)
    dr, dc = _dimensions(gh, gw)
    if (rows is not None and rows != dr) or (cols is not None and cols != dc):
        raise InvalidShape(f"rows/cols 与整图尺寸不符，整图推算为 {dr}×{dc}")
    tiles = {}
    for r in range(dr):
        for c in range(dc):
            tiles[(r, c)] = _slice_tile(grid, r, c)
    return dr, dc, tiles


class ElevationService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        init_db(db_path)

    # ---- 连接/事务 -------------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self):
        """写事务：BEGIN IMMEDIATE，异常回滚、正常提交。"""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def _read(self):
        """只读事务：整个读取过程看到同一快照。"""
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("ROLLBACK")

    @staticmethod
    def _basemap_row(conn):
        return conn.execute(
            "SELECT rows, cols, created_at FROM basemap WHERE id = 1").fetchone()

    def _require_basemap(self, conn):
        bm = self._basemap_row(conn)
        if bm is None:
            raise NoBasemap("尚未录入底图")
        return bm

    @staticmethod
    def _check_coords(bm, row, col):
        if not (0 <= row < bm["rows"] and 0 <= col < bm["cols"]):
            raise TileOutOfRange(
                f"瓦片 ({row},{col}) 超出底图范围 {bm['rows']}×{bm['cols']}",
                row=row, col=col)

    @staticmethod
    def _versions(conn, rows, cols):
        versions = {(r, c): 0 for r in range(rows) for c in range(cols)}
        for t in conn.execute("SELECT row, col, version FROM tile_versions"):
            versions[(t["row"], t["col"])] = t["version"]
        return versions

    @staticmethod
    def _version_matrix(conn, rows, cols):
        versions = [[0] * cols for _ in range(rows)]
        for t in conn.execute("SELECT row, col, version FROM tile_versions"):
            versions[t["row"]][t["col"]] = t["version"]
        return versions

    @staticmethod
    def _current_tiles(conn, rows, cols):
        """把全图高程还原成各块 17×17，供发版规划器使用。"""
        grid = [[0] * (16 * cols + 1) for _ in range(16 * rows + 1)]
        for p in conn.execute("SELECT row, col, value FROM elevation"):
            grid[p["row"]][p["col"]] = p["value"]
        return {(r, c): _slice_tile(grid, r, c)
                for r in range(rows) for c in range(cols)}

    # ---- 底图 ------------------------------------------------------------

    def create_basemap(self, rows, cols, tiles):
        """录入底图。tiles: {(row,col): 17×17}，必须铺满且接缝吻合。"""
        expected = {(r, c) for r in range(rows) for c in range(cols)}
        if set(tiles) != expected:
            raise InvalidShape(
                "瓦片集合未铺满矩形底图",
                missing=[list(t) for t in sorted(expected - set(tiles))][:8],
                unexpected=[list(t) for t in sorted(set(tiles) - expected)][:8])
        mismatches = find_seam_mismatches(rows, cols, tiles)
        if mismatches:
            raise SeamMismatch("相邻瓦片共边不吻合，拒绝录入", mismatches=mismatches)

        # 接缝已逐点吻合，因此重叠坐标落到全图上的值唯一。
        points = {}
        for (r, c), tile in tiles.items():
            for i in range(TILE):
                for j in range(TILE):
                    points[(16 * r + i, 16 * c + j)] = tile[i][j]
        with self._write() as conn:
            if self._basemap_row(conn) is not None:
                raise BasemapExists("底图已存在；如需重录请先 DELETE /basemap")
            conn.execute(
                "INSERT INTO basemap (id, rows, cols, created_at) VALUES (1,?,?,?)",
                (rows, cols, _now()))
            conn.executemany(
                "INSERT INTO elevation (row, col, value) VALUES (?,?,?)",
                [(gr, gc, points[(gr, gc)]) for (gr, gc) in sorted(points)])
            conn.executemany(
                "INSERT INTO tile_versions (row, col, version) VALUES (?,?,1)",
                [(r, c) for (r, c) in sorted(tiles)])
        return {"rows": rows, "cols": cols, "tiles": rows * cols,
                "elevation_points": len(points)}

    def reset(self):
        with self._write() as conn:
            conn.execute("DELETE FROM staged")
            conn.execute("DELETE FROM elevation")
            conn.execute("DELETE FROM tile_versions")
            conn.execute("DELETE FROM basemap")
        return {"status": "reset"}

    def get_basemap(self):
        with self._read() as conn:
            bm = self._require_basemap(conn)
            rows, cols = bm["rows"], bm["cols"]
            versions = self._version_matrix(conn, rows, cols)
            staged_counts = [[0] * cols for _ in range(rows)]
            for s in conn.execute(
                    "SELECT row, col, COUNT(*) AS n FROM staged GROUP BY row, col"):
                staged_counts[s["row"]][s["col"]] = s["n"]
        return {"rows": rows, "cols": cols, "created_at": bm["created_at"],
                "versions": versions, "staged_counts": staged_counts}

    def get_tile(self, row, col):
        """单块取数：只查该块的坐标范围，返回完整 17×17（含共边/角点）。"""
        with self._read() as conn:
            bm = self._require_basemap(conn)
            self._check_coords(bm, row, col)
            version = conn.execute(
                "SELECT version FROM tile_versions WHERE row = ? AND col = ?",
                (row, col)).fetchone()["version"]
            points = conn.execute(
                "SELECT row, col, value FROM elevation "
                "WHERE row BETWEEN ? AND ? AND col BETWEEN ? AND ?",
                (16 * row, 16 * row + TILE - 1,
                 16 * col, 16 * col + TILE - 1)).fetchall()
        data = [[0] * TILE for _ in range(TILE)]
        for p in points:
            data[p["row"] - 16 * row][p["col"] - 16 * col] = p["value"]
        return {"row": row, "col": col, "version": version, "data": data}

    def get_grid(self):
        """整图取数：直接读全图高程表，拼成 (16·rows+1)×(16·cols+1)。"""
        with self._read() as conn:
            bm = self._require_basemap(conn)
            rows, cols = bm["rows"], bm["cols"]
            gh, gw = 16 * rows + 1, 16 * cols + 1
            grid = [[0] * gw for _ in range(gh)]
            for p in conn.execute("SELECT row, col, value FROM elevation"):
                grid[p["row"]][p["col"]] = p["value"]
            versions = self._version_matrix(conn, rows, cols)
        return {"rows": rows, "cols": cols, "grid_rows": gh, "grid_cols": gw,
                "versions": versions, "grid": grid}

    def get_window(self, row0, col0, row1, col1):
        """跨瓦片取点：只读窗口覆盖的矩形区域，返回 [row0..row1]×[col0..col1]。"""
        with self._read() as conn:
            bm = self._require_basemap(conn)
            max_r, max_c = 16 * bm["rows"], 16 * bm["cols"]
            if not (0 <= row0 <= row1 <= max_r and 0 <= col0 <= col1 <= max_c):
                raise WindowOutOfRange(
                    f"窗口 ({row0},{col0})-({row1},{col1}) 超出整图范围 "
                    f"{max_r + 1}×{max_c + 1}")
            grid = [[0] * (col1 - col0 + 1) for _ in range(row1 - row0 + 1)]
            for p in conn.execute(
                    "SELECT row, col, value FROM elevation "
                    "WHERE row BETWEEN ? AND ? AND col BETWEEN ? AND ?",
                    (row0, row1, col0, col1)):
                grid[p["row"] - row0][p["col"] - col0] = p["value"]
        return {"row0": row0, "col0": col0, "row1": row1, "col1": col1,
                "grid": grid}

    # ---- 暂存 ------------------------------------------------------------

    def stage(self, row, col, data):
        _check_tile_shape(data)
        with self._write() as conn:
            bm = self._require_basemap(conn)
            self._check_coords(bm, row, col)
            cur = conn.execute(
                "INSERT INTO staged (row, col, data, created_at) VALUES (?,?,?,?)",
                (row, col, json.dumps(data), _now()))
            staged_id = cur.lastrowid
        return {"staged_id": staged_id, "row": row, "col": col}

    def list_staged(self, row=None, col=None):
        with self._read() as conn:
            self._require_basemap(conn)
            if row is None:
                rows = conn.execute(
                    "SELECT id, row, col, created_at FROM staged ORDER BY id"
                ).fetchall()
            else:
                bm = self._require_basemap(conn)
                self._check_coords(bm, row, col)
                rows = conn.execute(
                    "SELECT id, row, col, created_at FROM staged "
                    "WHERE row = ? AND col = ? ORDER BY id", (row, col)).fetchall()
        items = [{"staged_id": s["id"], "row": s["row"], "col": s["col"],
                  "created_at": s["created_at"]} for s in rows]
        return {"count": len(items), "items": items}

    # ---- 发版 ------------------------------------------------------------

    def publish(self, row, col):
        """点名一块发版：整批原子生效；无解时报冲突边且不改任何状态。

        只更新换版瓦片的版本号，以及高程真正发生变化的全图坐标；
        未换版瓦片的版本和未受影响的高程点保持原样。
        """
        with self._write() as conn:
            bm = self._require_basemap(conn)
            rows, cols = bm["rows"], bm["cols"]
            self._check_coords(bm, row, col)

            current = self._current_tiles(conn, rows, cols)
            versions = self._versions(conn, rows, cols)

            # 现行全图高程（点 -> 值），用于只写真正变化的坐标
            current_points = {}
            for (r, c), data in current.items():
                for i in range(TILE):
                    for j in range(TILE):
                        current_points[(16 * r + i, 16 * c + j)] = data[i][j]

            staged, staged_ids = {}, {}
            for s in conn.execute(
                    "SELECT s.row, s.col, s.id, s.data FROM staged s "
                    "JOIN (SELECT row, col, MAX(id) AS mid FROM staged "
                    "      GROUP BY row, col) m "
                    "ON m.row = s.row AND m.col = s.col AND m.mid = s.id"):
                staged[(s["row"], s["col"])] = json.loads(s["data"])
                staged_ids[(s["row"], s["col"])] = s["id"]

            try:
                chosen, conflicts = plan_publish(
                    rows, cols, current, staged, (row, col))
            except NoStagedVersion:
                raise NoStaged(f"瓦片 ({row},{col}) 没有暂存版，无法点名发版",
                               tile={"row": row, "col": col})
            if conflicts:
                # 事务随异常回滚：现行底图与暂存原样保留
                raise PublishConflict(
                    "不存在共边全部吻合的发版方案，现行底图与暂存均未改动",
                    conflicting_edges=[
                        {"tile_a": {"row": a[0], "col": a[1]},
                         "tile_b": {"row": b[0], "col": b[1]}}
                        for a, b in conflicts])

            # 汇总换版瓦片覆盖的全图坐标。共边点与角点只落一个键；规划器已
            # 保证相邻换版瓦片在这些点上取值相同，且与未换版一侧的现行值
            # 一致时无需写入。
            affected = {}
            for (r, c) in chosen:
                data = staged[(r, c)]
                for i in range(TILE):
                    for j in range(TILE):
                        point = (16 * r + i, 16 * c + j)
                        affected[point] = data[i][j]

            updates = []
            for (gr, gc), value in affected.items():
                old = current_points[(gr, gc)]
                if old != value:
                    updates.append((value, gr, gc))

            if updates:
                conn.executemany(
                    "UPDATE elevation SET value = ? WHERE row = ? AND col = ?",
                    updates)

            changed, cleared = [], []
            for (r, c) in sorted(chosen):
                new_version = versions[(r, c)] + 1
                conn.execute(
                    "UPDATE tile_versions SET version = ? "
                    "WHERE row = ? AND col = ?", (new_version, r, c))
                sid = staged_ids[(r, c)]
                conn.execute("DELETE FROM staged WHERE id = ?", (sid,))
                changed.append({"row": r, "col": c, "version": new_version})
                cleared.append(sid)

        return {"status": "published",
                "named_tile": {"row": row, "col": col},
                "changed_tiles": changed,
                "cleared_staged_ids": sorted(cleared),
                "updated_elevation_points": len(updates)}
