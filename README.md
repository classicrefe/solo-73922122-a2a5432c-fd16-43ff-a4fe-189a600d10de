# 高程瓦片供数服务

面向坡度分析的高程供数 API。矩形底图按**行列编号**划分瓦片，每块 **17×17 个整数高程点**，相邻块**重复存储共边**上的点，因此跨瓦片取点（如坡度计算）无需额外拼接逻辑。数据员**按块暂存**修测，再**点名一块发版**：服务自动选出**换块最少且所有共边逐点吻合**的方案整批生效；无解时报告冲突边两侧瓦片坐标，现行底图与暂存原样保留。

- 技术栈：FastAPI + SQLite（WAL），Docker Compose 一键启动
- 交互式文档：启动后访问 `http://localhost:8000/docs`

## 快速开始

```bash
# Docker（推荐）
docker compose up --build

# 或本地运行
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库文件路径由环境变量 `ELEV_DB_PATH` 指定（默认 `./data/elev.db`，Compose 中为 `/data/elev.db` 并挂载卷持久化）。

```bash
# 运行测试
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 业务流程与 API

| 步骤 | 方法与路径 | 说明 |
|---|---|---|
| 录入底图 | `POST /basemap` | 两种方式二选一：`tiles` 逐块给出（校验所有共边，失配则 422 并列出失配边与点）；或 `grid` 给出 (16·rows+1)×(16·cols+1) 整图，自动切块、接缝天然吻合 |
| 底图概况 | `GET /basemap` | 行列数、各块现行版本号、各块暂存数量 |
| 暂存修测 | `POST /tiles/{row}/{col}/staged` | 可多次暂存同一块，发版时取 id 最大（最新）的一版 |
| 查看暂存 | `GET /tiles/{row}/{col}/staged`、`GET /staged` | 暂存版本列表 |
| 点名发版 | `POST /publish` | 请求体 `{"row": r, "col": c}`；成功返回换版坐标与清除的暂存 id；冲突返回 409 与冲突边两侧瓦片坐标 |
| 整图取数 | `GET /basemap/grid` | 拼接后的完整高程面 + 各块版本号 |
| 跨瓦片取点 | `GET /basemap/window?row0=&col0=&row1=&col1=` | 整图坐标系下的矩形窗口（含端点） |
| 单块取数 | `GET /tiles/{row}/{col}` | 含现行版本号 |
| 重置 | `DELETE /basemap` | 删除底图与全部暂存（重录/演示用） |

坐标均从 0 开始。瓦片行列数上限 64×64（防止整图响应过大）。

### 发版语义

- **点名块必须**换上其最新暂存版（没有暂存版则 409 `no_staged_version`）；
- 其余每块可**保留现行版**或换上**各自最新暂存版**；
- 约束：发版后所有共边 17 个点逐点相等；目标：**换版块数最少**；
- 成功：整批原子生效，返回 `changed_tiles`（换版坐标与新版本号）与
  `cleared_staged_ids`；**只清除本次用到的暂存**（未涉及的、以及同块的
  更旧暂存版都保留）；
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

## 发版算法（为什么"换块最少"是确定的）

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
- 发版的"读取现行版+暂存 → 规划 → 批量更新+清除暂存"在**同一个写事务**内
  完成，因此两个并发发版不会基于过期快照做决策；
- 读操作（整图/窗口/单块）在只读事务内完成，不会看到发版换了一半的中间态。

`tests/test_concurrency.py` 用真实 uvicorn + 多线程验证：两个发版竞争同一
暂存版时恰好一个成功、另一个报 `no_staged_version`；发版 4×4 全图换版期间
并发整图取数，每次取到的都是完整旧图或完整新图。

## 项目结构

```
app/
  main.py     FastAPI 入口与路由（/docs）
  service.py  业务层：事务边界、发版编排
  solver.py   发版规划器（纯函数）与接缝校验
  db.py       SQLite 连接、建表、WAL
  models.py   请求体模型（17×17 校验）
tests/        pytest：API、规划器单测、并发测试
Dockerfile / docker-compose.yml
```

## 实测结果

- `python -m pytest tests/ -q`：**32 passed**（含 3 个并发用例，重复运行稳定）
- 本地 uvicorn 冒烟：录入 → 暂存 → 冲突 409（冲突边正确）→ 补齐邻块暂存 →
  发版 200（两块同换、暂存按用清除）→ 整图/窗口取数一致 → 再发版报
  `no_staged_version`，全部符合预期
- `/docs` 与 `/openapi.json` 可正常访问，四个核心操作均可在 Swagger UI 中执行

## 待完成项

- 本环境无 Docker，`Dockerfile`/`docker-compose.yml` 未经实际构建验证（已按
  标准实践编写，本地等效命令 `uvicorn app.main:app` 验证通过）
- 无认证与权限控制；暂存、发版未记录操作人
- 仅支持单幅底图（重录需先 `DELETE /basemap`）
- 窗口取数目前整图拼接后切片，瓦片规模放大后可改为按需读取相交瓦片
- 历史暂存版无自动清理策略（发版只清除用到的版本，旧版需人工管理）
- 瓦片尺寸 17×17 为常量（`app/solver.py: TILE`），未参数化
- 仅"供数"，未提供坡度计算端点（消费方自行基于整图/窗口接口计算）
