# 高程瓦片供数服务

面向坡度分析的高程供数 API。矩形底图按**行列编号**划分瓦片，每块取数返回 **17×17 个整数高程点**。存储上**全图每个坐标只保存一份现行高程**——相邻瓦片的共边、四块交会的角点都不再重复，读取时按坐标范围拼出完整瓦片，因此跨瓦片取点（如坡度计算）无需额外拼接逻辑。数据员**按块暂存**修测，再**点名一块发版**：服务自动选出**换块最少且所有共边逐点吻合**的方案整批生效；无解时报告冲突边两侧瓦片坐标，现行底图与暂存原样保留。

- 技术栈：FastAPI + SQLite（WAL），Docker Compose 一键启动
- 交互式文档：启动后访问 `http://localhost:8000/docs`（可直接在 Swagger UI 演示修测与取数）

## 快速开始

```bash
# Docker（推荐）
docker compose up --build

# 或本地运行
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件路径由环境变量 `ELEV_DB_PATH` 指定（默认 `./data/elev.db`，Compose 中为 `/data/elev.db` 并挂载卷持久化）。旧版数据库会在启动时自动升级（见下文“旧库升级”）。

```bash
# 运行测试
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 存储模型

| 表 | 内容 |
|---|---|
| `basemap` | 单幅底图元数据（行数、列数、录入时间） |
| `elevation` | 全图高程点，主键 `(row, col)`；共边点与四块角点只有一行 |
| `tile_versions` | 每块瓦片的现行版本号 |
| `staged` | 按块暂存的 17×17 修测快照（JSON；暂存期间仍带重复共边） |

- 瓦片 `(r, c)` 覆盖全图坐标 `[16r, 16r+16] × [16c, 16c+16]`；单块取数按
  该范围查 `elevation`（主键索引），不扫描整图。
- 录入时校验所有共边（含角点）逐点吻合，重叠坐标落到全图上的值唯一。
- 发版时先把候选方案按整图坐标去重，再只 `UPDATE` 真正变化的高程行，
  并且只更新换版瓦片的版本号。
- 整图/窗口取数直接从 `elevation` 读对应坐标范围。

## 业务流程与 API

| 步骤 | 方法与路径 | 说明 |
|---|---|---|
| 录入底图 | `POST /basemap` | 两种方式二选一：`tiles` 逐块给出（校验所有共边，失配则 422 并列出失配边与点）；或 `grid` 给出 (16·rows+1)×(16·cols+1) 整图，自动切块、接缝天然吻合 |
| 底图概况 | `GET /basemap` | 行列数、各块现行版本号、各块暂存数量 |
| 暂存修测 | `POST /tiles/{row}/{col}/staged` | 可多次暂存同一块，发版时取 id 最大（最新）的一版 |
| 查看暂存 | `GET /tiles/{row}/{col}/staged`、`GET /staged` | 暂存版本列表 |
| 点名发版 | `POST /publish` | 请求体 `{"row": r, "col": c}`；成功返回换版坐标、清除的暂存 id 与本次更新的高程点数；冲突返回 409 与冲突边两侧瓦片坐标 |
| 整图取数 | `GET /basemap/grid` | 拼接后的完整高程面 + 各块版本号 |
| 跨瓦片取点 | `GET /basemap/window?row0=&col0=&row1=&col1=` | 整图坐标系下的矩形窗口（含端点） |
| 单块取数 | `GET /tiles/{row}/{col}` | 完整 17×17（含共边/角点）+ 现行版本号 |
| 重置 | `DELETE /basemap` | 删除底图与全部暂存（重录/演示用） |
| 健康检查 | `GET /health` | 服务与数据库可用性 |

坐标均从 0 开始。瓦片行列数上限 64×64（防止整图响应过大）。

### 发版语义

- **点名块必须**换上其最新暂存版（没有暂存版则 409 `no_staged_version`）；
- 其余每块可**保留现行版**或换上**各自最新暂存版**；
- 约束：发版后所有共边 17 个点逐点相等（四块角点共用一个全图坐标，因此自动一致）；
- 目标：**换块块数最少**；
- 成功：整批原子生效，返回 `changed_tiles`（换版坐标与新版本号）、
  `cleared_staged_ids` 与 `updated_elevation_points`；**只清除本次用到的暂存**
  （未涉及的、以及同块的更旧暂存版都保留）；
- 无解：409 `publish_conflict`，`conflicting_edges` 列出冲突边两侧瓦片
  坐标，**现行底图与暂存均未改动**。

### 示例

```bash
# 录入 1×2 底图（整图方式，全图高程 100）
curl -X POST localhost:8000/basemap -H 'Content-Type: application/json' \
  -d "{\"grid\": $(python -c 'import json;print(json.dumps([[100]*33]*17))')}"

# 暂存 (0,0)：右共边改为 200
python - <<'EOF'
import json, urllib.request
t = [[100]*17 for _ in range(17)]
for row in t: row[16] = 200
req = urllib.request.Request("http://localhost:8000/tiles/0/0/staged",
    data=json.dumps({"data": t}).encode(), headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(req).read().decode())
EOF

# 点名 (0,0) 发版：邻块 (0,1) 没有能接上的暂存版 -> 409 并报告冲突边
curl -X POST localhost:8000/publish -H 'Content-Type: application/json' -d '{"row":0,"col":0}'
```

## 旧库升级

启动时 `init_db` 检查 `PRAGMA user_version`。旧库（`tiles` 表用 JSON 存完整
17×17、共边重复）会在**一个 `BEGIN IMMEDIATE` 事务**内自动升级：

1. 建新表 `elevation` / `tile_versions`；
2. 逐块读取旧 `tiles`，校验瓦片集合完整、坐标在范围内、形状为 17×17、
   高程为整数；把 17×17 展开成全图坐标，重复坐标必须值相同（否则视为库损坏）；
3. 写入全图高程与各块版本号，保留 `basemap`、`staged` 原样；
4. `DROP TABLE tiles`，置 `user_version = 1`，提交。

任何一步失败都整体 `ROLLBACK`：旧 `tiles`、底图、版本、暂存保持原样，
`user_version` 不变，旧库仍可修复后重试。`user_version` 高于程序支持版本、
或库结构无法识别时，服务拒绝启动并报 `SchemaUpgradeError`，避免误改数据。

## 发版算法（为什么“换块最少”是确定的）

对每条共边，记现行值为 K、两侧最新暂存版在该边上的值为 A、B（现行底图
本身接缝吻合，两侧 K 相同）：

| 情形 | 结论 |
|---|---|
| A=K 且 B=K | 无约束 |
| A≠K 且 B=K | 左侧永远不能换（换了右侧接不上） |
| A=K 且 B≠K | 右侧永远不能换 |
| A≠K、B≠K、A=B | **等值边**：两侧必须同进退 |
| A≠K、B≠K、A≠B | 两侧都不能换 |
| 一侧无暂存 | 另一侧仅当 A=K 时可换 |

把等值边做**并查集**连边，点名块所在连通分量就是唯一被强制换版的最小集合：
任何可行方案都必须整个换掉它，而分量之外全部保留现行版必然可行。对该分量
做全量共边校验：通过即为换块最少的方案（唯一）；不通过则不存在任何可行方案
（分量外的块即使换版也接不上，否则它早被等值边连进分量），校验发现的失配边
就是要报告的冲突边。算法复杂度 O(瓦片数 × 17)。实现见 `app/solver.py`。

## 并发与可串行化

暂存、发版、整图取数并发时，结果与某种依次执行的顺序一致：

- SQLite **WAL** 模式：读者不阻塞写者，读事务看到一致性快照；
- 写操作（录入/暂存/发版/重置）一律 `BEGIN IMMEDIATE`：事务开始即拿写锁，
  写者之间严格串行，`busy_timeout=30s` 兜底等待；
- 发版的“读取现行高程+暂存 → 规划 → 批量更新高程/版本 + 清除暂存”在
  **同一个写事务**内完成并一次提交，因此并发发版不会基于过期快照决策，
  读者看到的高程和版本必然来自同一次提交；
- 读操作（整图/窗口/单块）在只读事务内完成，不会看到发版换了一半的中间态；
- 发版失败（冲突/无暂存）时异常触发 `ROLLBACK`，现行底图与暂存不变。

`tests/test_concurrency.py` 用真实 uvicorn + 多线程验证：两个发版竞争同一
暂存版时恰好一个成功、另一个报 `no_staged_version`；发版 4×4 全图换版期间
并发整图取数，每次取到的都是完整旧图或完整新图。

## 项目结构

```
app/
  main.py     FastAPI 入口与路由（/docs、/health）
  service.py  业务层：事务边界、发版编排、按坐标取数
  solver.py   发版规划器（纯函数）与接缝校验
  db.py       SQLite 连接、建表、旧库自动升级
  models.py   请求体模型（17×17 校验）
tests/        pytest：API、存储模型、旧库升级、规划器单测、并发测试
Dockerfile / docker-compose.yml
```

## 实测结果

- `python -m pytest tests/ -q`：**46 passed**
  （原 32 个用例 + 存储模型 5 个 + 旧库升级 9 个）
- 存储模型测试直接查 SQLite：2×3 底图的 `elevation` 恰为 33×49 行、无重复
  坐标；`(16,16)` 这类四块角点只有一行；单块查询走主键索引
  （`SEARCH elevation USING ...`，无全表扫描）
- 发版写入量用 SQLite 触发器实测：只改内部一点时 `elevation` 仅 1 次
  UPDATE、`tile_versions` 仅 1 次；整条共边 17 点全部变化时
  `elevation` 17 次、版本 2 次；四块同改角点时角点只写 1 次
- 旧库升级：预置旧版 `tiles` 库（含非 1 版本号与暂存），启动后版本/底图/
  暂存保留、旧表删除、`user_version=1`；共边/形状/缺块/坏 JSON 四类损坏库
  升级失败后旧库逐字节状态（表、行数、`user_version`）不变，修复后重试成功
- 本地 uvicorn 冒烟：录入 → 暂存 → 冲突 409（冲突边正确）→ 补齐邻块暂存 →
  发版 200（两块同换、暂存按用清除）→ 整图/窗口/单块取数一致 → 再发版报
  `no_staged_version`，全部符合预期
- `/docs`、`/openapi.json`、`/health` 可正常访问，四个核心操作均可在
  Swagger UI 中执行

## 待完成项

- 本环境无 Docker，`Dockerfile`/`docker-compose.yml` 未经实际构建验证（已按
  标准实践编写，本地等效命令 `uvicorn app.main:app` 验证通过）；首次在装有
  Docker 的机器上执行 `docker compose up --build` 时请确认构建与卷挂载
- 升级过程需要一次性在单事务内重写全图：64×64 上限（约 105 万点）时启动会
  短暂变慢，但不会阻塞服务启动完成前的请求（服务在升级成功后才开始接收）
- 无认证与权限控制；暂存、发版未记录操作人
- 仅支持单幅底图（重录需先 `DELETE /basemap`）
- 历史暂存版无自动清理策略（发版只清除用到的版本，旧版需人工管理）
- 瓦片尺寸 17×17 为常量（`app/solver.py: TILE`），未参数化
- 仅“供数”，未提供坡度计算端点（消费方自行基于整图/窗口接口计算）
