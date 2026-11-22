"""请求体的 Pydantic 模型。"""

from __future__ import annotations

from typing import Annotated, Optional

from pydantic import BaseModel, Field, model_validator

from .solver import TILE

MAX_DIM = 64  # 底图行列数上限，防止整图取数响应过大

Row17 = Annotated[list[int], Field(min_length=TILE, max_length=TILE)]
Grid17 = Annotated[list[Row17], Field(min_length=TILE, max_length=TILE)]


class TileInput(BaseModel):
    row: int = Field(ge=0, description="瓦片行号（从 0 开始）")
    col: int = Field(ge=0, description="瓦片列号（从 0 开始）")
    data: Grid17 = Field(description="17×17 整数高程点")


class BasemapCreate(BaseModel):
    """录入矩形底图。两种方式二选一：

    * tiles：逐块给出 17×17 数据，服务会校验所有共边逐点吻合；
    * grid：直接给出拼接好的整图 (16·rows+1)×(16·cols+1)，服务自动切块，
            接缝天然吻合。rows/cols 可省略（由整图尺寸推算）。
    """

    rows: Optional[int] = Field(default=None, ge=1, le=MAX_DIM)
    cols: Optional[int] = Field(default=None, ge=1, le=MAX_DIM)
    tiles: Optional[list[TileInput]] = None
    grid: Optional[list[list[int]]] = None

    @model_validator(mode="after")
    def _check(self):
        if (self.tiles is None) == (self.grid is None):
            raise ValueError("tiles 与 grid 必须且只能提供一个")
        if self.tiles is not None and (self.rows is None or self.cols is None):
            raise ValueError("使用 tiles 录入时必须给出 rows 和 cols")
        return self


class StageCreate(BaseModel):
    data: Grid17 = Field(description="该块修测后的 17×17 整数高程点")


class PublishRequest(BaseModel):
    row: int = Field(ge=0, description="被点名发版的瓦片行号")
    col: int = Field(ge=0, description="被点名发版的瓦片列号")
