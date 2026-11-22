"""高程瓦片供数 API（FastAPI 入口）。

业务流程：录入底图 -> 按块暂存修测 -> 点名一块发版 -> 整图/窗口取数。
交互式文档见 /docs（Swagger UI）。
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse

from .models import BasemapCreate, PublishRequest, StageCreate
from .service import (ElevationService, InvalidShape, ServiceError,
                      tiles_from_grid)

DEFAULT_DB_PATH = "./data/elev.db"


def create_app(db_path: str | None = None) -> FastAPI:
    service = ElevationService(
        db_path or os.environ.get("ELEV_DB_PATH", DEFAULT_DB_PATH))

    app = FastAPI(
        title="高程瓦片供数服务",
        version="1.0.0",
        summary="面向坡度分析的高程供数 API",
        description=(
            "矩形底图按行列划分瓦片，每块 17×17 个整数高程点，相邻块重复存储共边。"
            "数据员按块暂存修测，再点名一块发版：点名块必须换上最新暂存版，"
            "其余块可保留或换上各自最新暂存版，服务自动选出换块最少且所有共边"
            "逐点吻合的方案整批生效；无解时报告冲突边两侧瓦片坐标且不改任何数据。"
            "暂存、发版、整图取数并发时满足可串行化。"))

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail()})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    # ---- 底图 ------------------------------------------------------------

    @app.post("/basemap", status_code=201, tags=["底图"],
              summary="录入矩形底图（接缝必须吻合）")
    def create_basemap(body: BasemapCreate):
        if body.grid is not None:
            rows, cols, tiles = tiles_from_grid(body.grid, body.rows, body.cols)
        else:
            rows, cols = body.rows, body.cols
            tiles = {}
            for t in body.tiles:
                if (t.row, t.col) in tiles:
                    raise InvalidShape(f"瓦片 ({t.row},{t.col}) 重复提供")
                tiles[(t.row, t.col)] = t.data
        return service.create_basemap(rows, cols, tiles)

    @app.delete("/basemap", tags=["底图"], summary="删除底图及全部暂存（重录用）")
    def reset_basemap():
        return service.reset()

    @app.get("/basemap", tags=["底图"], summary="底图概况（尺寸、各块版本号）")
    def get_basemap():
        return service.get_basemap()

    @app.get("/basemap/grid", tags=["取数"],
             summary="整图取数：拼接后的完整高程面")
    def get_grid():
        return service.get_grid()

    @app.get("/basemap/window", tags=["取数"],
             summary="跨瓦片取点：整图坐标系下的矩形窗口（含端点）")
    def get_window(row0: int = Query(ge=0), col0: int = Query(ge=0),
                   row1: int = Query(ge=0), col1: int = Query(ge=0)):
        return service.get_window(row0, col0, row1, col1)

    @app.get("/tiles/{row}/{col}", tags=["取数"],
             summary="单块取数（含现行版本号）")
    def get_tile(row: int, col: int):
        return service.get_tile(row, col)

    # ---- 暂存 ------------------------------------------------------------

    @app.post("/tiles/{row}/{col}/staged", status_code=201, tags=["暂存"],
              summary="暂存一版修测（可多次暂存，发版取最新）")
    def stage(row: int, col: int, body: StageCreate):
        return service.stage(row, col, body.data)

    @app.get("/tiles/{row}/{col}/staged", tags=["暂存"],
             summary="查看某块的暂存版本列表")
    def list_tile_staged(row: int, col: int):
        return service.list_staged(row, col)

    @app.get("/staged", tags=["暂存"], summary="查看全部暂存版本")
    def list_all_staged():
        return service.list_staged()

    # ---- 发版 ------------------------------------------------------------

    @app.post("/publish", tags=["发版"],
              summary="点名一块发版：换块最少且共边吻合的方案整批生效")
    def publish(body: PublishRequest):
        return service.publish(body.row, body.col)

    return app


app = create_app()
