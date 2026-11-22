"""业务层：所有读写都在 SQLite 事务内完成。

并发约定（可串行化）：
  * 写操作（录入底图、暂存、发版、重置）一律 BEGIN IMMEDIATE —— 一开始就拿到
    写锁，事务期间其它写者进不来，读者（WAL 模式）读的是一致的旧快照；
  * 读操作（整图/窗口/单块取数）用普通只读事务，看到的是一个一致性快照；
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


def tiles_from_grid(grid, rows, cols):
    """把 (16·rows+1)×(16·cols+1) 的整图切成带共边重复的瓦片。"""
    if not grid or any(not isinstance(r, list) or len(r) == 0 for r in grid):
        raise InvalidShape("grid 必须是非空的二维数组")
    gh, gw = len(grid), len(grid[0])
    if any(len(r) != gw for r in grid):
        raise InvalidShape("grid 各行长度不一致")
    if (gh - 1) % 16 != 0 or (gw - 1) % 16 != 0 or gh < 17 or gw < 17:
        raise InvalidShape("grid 尺寸必须是 (16*rows+1) × (16*cols+1)")
    dr, dc = (gh - 1) // 16, (gw - 1) // 16
    if (rows is not None and rows != dr) or (cols is not None and cols != dc):
        raise InvalidShape(f"rows/cols 与整图尺寸不符，整图推算为 {dr}×{dc}")
    tiles = {}
    for r in range(dr):
        for c in range(dc):
            tiles[(r, c)] = [row[16 * c:16 * c + TILE] for row in grid[16 * r:16 * r + TILE]]
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
            except Exception:
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
        with self._write() as conn:
            if self._basemap_row(conn) is not None:
                raise BasemapExists("底图已存在；如需重录请先 DELETE /basemap")
            conn.execute(
                "INSERT INTO basemap (id, rows, cols, created_at) VALUES (1,?,?,?)",
                (rows, cols, _now()))
            conn.executemany(
                "INSERT INTO tiles (row, col, version, data) VALUES (?,?,1,?)",
                [(r, c, json.dumps(tiles[(r, c)])) for (r, c) in sorted(tiles)])
        return {"rows": rows, "cols": cols, "tiles": rows * cols}

    def reset(self):
        with self._write() as conn:
            conn.execute("DELETE FROM staged")
            conn.execute("DELETE FROM tiles")
            conn.execute("DELETE FROM basemap")
        return {"status": "reset"}

    def get_basemap(self):
        with self._read() as conn:
            bm = self._require_basemap(conn)
            rows, cols = bm["rows"], bm["cols"]
            versions = [[0] * cols for _ in range(rows)]
            staged_counts = [[0] * cols for _ in range(rows)]
            for t in conn.execute("SELECT row, col, version FROM tiles"):
                versions[t["row"]][t["col"]] = t["version"]
            for s in conn.execute(
                    "SELECT row, col, COUNT(*) AS n FROM staged GROUP BY row, col"):
                staged_counts[s["row"]][s["col"]] = s["n"]
        return {"rows": rows, "cols": cols, "created_at": bm["created_at"],
                "versions": versions, "staged_counts": staged_counts}

    def get_tile(self, row, col):
        with self._read() as conn:
            bm = self._require_basemap(conn)
            self._check_coords(bm, row, col)
            t = conn.execute(
                "SELECT version, data FROM tiles WHERE row = ? AND col = ?",
                (row, col)).fetchone()
        return {"row": row, "col": col, "version": t["version"],
                "data": json.loads(t["data"])}

    def get_grid(self):
        """整图取数：把各瓦片拼成 (16·rows+1)×(16·cols+1) 的完整高程面。"""
        with self._read() as conn:
            bm = self._require_basemap(conn)
            rows, cols = bm["rows"], bm["cols"]
            gh, gw = 16 * rows + 1, 16 * cols + 1
            grid = [[0] * gw for _ in range(gh)]
            versions = [[0] * cols for _ in range(rows)]
            for t in conn.execute("SELECT row, col, version, data FROM tiles"):
                data, r, c = json.loads(t["data"]), t["row"], t["col"]
                versions[r][c] = t["version"]
                for i in range(TILE):
                    dst, src, base_c = grid[16 * r + i], data[i], 16 * c
                    for j in range(TILE):
                        dst[base_c + j] = src[j]
        return {"rows": rows, "cols": cols, "grid_rows": gh, "grid_cols": gw,
                "versions": versions, "grid": grid}

    def get_window(self, row0, col0, row1, col1):
        """跨瓦片取点：返回拼接后整图上 [row0..row1]×[col0..col1]（含端点）。"""
        full = self.get_grid()
        max_r, max_c = full["grid_rows"] - 1, full["grid_cols"] - 1
        if not (0 <= row0 <= row1 <= max_r and 0 <= col0 <= col1 <= max_c):
            raise WindowOutOfRange(
                f"窗口 ({row0},{col0})-({row1},{col1}) 超出整图范围 "
                f"{full['grid_rows']}×{full['grid_cols']}")
        grid = [row[col0:col1 + 1] for row in full["grid"][row0:row1 + 1]]
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
        """点名一块发版：整批原子生效；无解时报冲突边且不改任何状态。"""
        with self._write() as conn:
            bm = self._require_basemap(conn)
            rows, cols = bm["rows"], bm["cols"]
            self._check_coords(bm, row, col)

            current, versions = {}, {}
            for t in conn.execute("SELECT row, col, version, data FROM tiles"):
                current[(t["row"], t["col"])] = json.loads(t["data"])
                versions[(t["row"], t["col"])] = t["version"]

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

            changed, cleared = [], []
            for (r, c) in sorted(chosen):
                new_version = versions[(r, c)] + 1
                conn.execute(
                    "UPDATE tiles SET version = ?, data = ? "
                    "WHERE row = ? AND col = ?",
                    (new_version, json.dumps(staged[(r, c)]), r, c))
                sid = staged_ids[(r, c)]
                conn.execute("DELETE FROM staged WHERE id = ?", (sid,))
                changed.append({"row": r, "col": c, "version": new_version})
                cleared.append(sid)

        return {"status": "published",
                "named_tile": {"row": row, "col": col},
                "changed_tiles": changed,
                "cleared_staged_ids": sorted(cleared)}
