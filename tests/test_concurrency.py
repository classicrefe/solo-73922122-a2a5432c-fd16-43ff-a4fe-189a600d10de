"""并发测试：暂存、发版、整图取数同时进行，结果须与某种串行顺序一致。

用真实 uvicorn 服务 + 多线程 httpx 客户端验证：
  A. 两个发版请求竞争同一块的唯一暂存版 —— 必须恰好一个成功、
     另一个报 no_staged_version（对应"先发版；后发版"的串行序）；
  B. 发版整批生效期间并发整图取数 —— 每次取到的必须是完整旧图或
     完整新图，绝不允许看到换了一半的中间态；
  C. 暂存/发版/取数混合压力 —— 不允许出现 5xx，最终状态自洽。
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import create_app
from tests.conftest import full_grid, tile_grid


class LiveServer:
    def __init__(self, app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                log_level="error", loop="asyncio", http="h11")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start in time")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=15)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture()
def server(tmp_path):
    app = create_app(str(tmp_path / "concurrent.db"))
    with LiveServer(app) as s:
        yield s


def _post(client, url, payload):
    try:
        return client.post(url, json=payload, timeout=30)
    except httpx.HTTPError:
        return None


def test_concurrent_publish_same_tile_is_serializable(server):
    """两个线程同时发版同一块：必须一个 200、一个 409 no_staged。"""
    with httpx.Client() as c:
        assert c.post(f"{server.url}/basemap",
                      json={"grid": full_grid(1, 1)}, timeout=30).status_code == 201
        t = tile_grid(100)
        t[3][3] = 5
        assert c.post(f"{server.url}/tiles/0/0/staged",
                      json={"data": t}, timeout=30).status_code == 201

    barrier = threading.Barrier(2)
    results = []

    def publish():
        with httpx.Client() as c:
            barrier.wait()
            r = _post(c, f"{server.url}/publish", {"row": 0, "col": 0})
            results.append(r)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    assert all(r is not None for r in results)
    ok = [r for r in results if r.status_code == 200]
    no_staged = [r for r in results
                 if r.status_code == 409
                 and r.json()["detail"]["error"] == "no_staged_version"]
    # 唯一符合串行语义的结局：一个消费了暂存版，另一个到时已无可发
    assert len(ok) == 1 and len(no_staged) == 1, [r.status_code for r in results]
    assert ok[0].json()["changed_tiles"] == [{"row": 0, "col": 0, "version": 2}]


def test_fetch_never_sees_partial_publish(server):
    """发版 4×4 全图换版期间，整图取数必须始终自洽（全旧或全新）。"""
    rows = cols = 4
    with httpx.Client() as c:
        assert c.post(f"{server.url}/basemap",
                      json={"grid": full_grid(rows, cols, 100)},
                      timeout=30).status_code == 201

    stop = threading.Event()
    violations = []

    def reader():
        with httpx.Client() as c:
            while not stop.is_set():
                try:
                    r = c.get(f"{server.url}/basemap/grid", timeout=30)
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    violations.append(f"status {r.status_code}")
                    continue
                body = r.json()
                values = {v for row in body["grid"] for v in row}
                versions = {v for row in body["versions"] for v in row}
                # 每次发版把所有块一起换成同一个新值：
                # 读到混合值或混合版本号即说明看到了中间态
                if len(values) != 1:
                    violations.append(f"mixed grid values: {sorted(values)[:5]}")
                if len(versions) != 1:
                    violations.append(f"mixed versions: {sorted(versions)}")

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for th in readers:
        th.start()

    rounds = 8
    with httpx.Client() as c:
        for k in range(rounds):
            value = 1000 + k
            for r_ in range(rows):
                for c_ in range(cols):
                    resp = _post(c, f"{server.url}/tiles/{r_}/{c_}/staged",
                                 {"data": tile_grid(value)})
                    assert resp is not None and resp.status_code == 201
            resp = _post(c, f"{server.url}/publish", {"row": 0, "col": 0})
            assert resp is not None and resp.status_code == 200, resp.text
            assert len(resp.json()["changed_tiles"]) == rows * cols

    stop.set()
    for th in readers:
        th.join(timeout=60)

    assert violations == []
    with httpx.Client() as c:
        final = c.get(f"{server.url}/basemap/grid", timeout=30).json()
    assert {v for row in final["grid"] for v in row} == {1000 + rounds - 1}
    assert {v for row in final["versions"] for v in row} == {1 + rounds}


def test_mixed_workload_no_5xx_and_final_state_consistent(server):
    """暂存/发版/取数混合并发：不允许 5xx，结束后状态自洽。"""
    rows = cols = 3
    with httpx.Client() as c:
        assert c.post(f"{server.url}/basemap",
                      json={"grid": full_grid(rows, cols, 100)},
                      timeout=30).status_code == 201

    stop = threading.Event()
    problems = []
    deadline = time.time() + 6

    def stager(tid):
        with httpx.Client() as c:
            k = 0
            while time.time() < deadline:
                t = tile_grid(200 + tid)
                t[0][0] = k  # 只动角点内部含义，边上仍是 200+tid
                r = _post(c, f"{server.url}/tiles/{tid % rows}/{tid % cols}/staged",
                          {"data": t})
                if r is None or r.status_code >= 500:
                    problems.append(f"stager{tid}: {r and r.status_code}")
                k += 1

    def publisher():
        with httpx.Client() as c:
            while time.time() < deadline:
                for coord in ((0, 0), (1, 1), (2, 2)):
                    r = _post(c, f"{server.url}/publish",
                              {"row": coord[0], "col": coord[1]})
                    if r is None or r.status_code >= 500:
                        problems.append(f"publisher: {r and r.status_code}")
                    elif r.status_code == 409:
                        err = r.json()["detail"]["error"]
                        if err not in ("no_staged_version", "publish_conflict"):
                            problems.append(f"unexpected 409: {err}")

    def reader():
        with httpx.Client() as c:
            while time.time() < deadline:
                try:
                    r = c.get(f"{server.url}/basemap/grid", timeout=30)
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    problems.append(f"reader: {r.status_code}")

    threads = ([threading.Thread(target=stager, args=(i,)) for i in range(4)]
               + [threading.Thread(target=publisher) for _ in range(2)]
               + [threading.Thread(target=reader) for _ in range(2)])
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)

    assert problems == []

    # 最终状态自洽：底图接缝吻合（逐边核对），且服务仍可正常响应
    with httpx.Client() as c:
        info = c.get(f"{server.url}/basemap", timeout=30).json()
        grid = c.get(f"{server.url}/basemap/grid", timeout=30).json()
        for r_ in range(info["rows"]):
            for c_ in range(info["cols"]):
                tile = c.get(f"{server.url}/tiles/{r_}/{c_}", timeout=30).json()["data"]
                if c_ + 1 < info["cols"]:
                    right = c.get(f"{server.url}/tiles/{r_}/{c_ + 1}",
                                  timeout=30).json()["data"]
                    assert all(tile[i][16] == right[i][0] for i in range(17))
                if r_ + 1 < info["rows"]:
                    down = c.get(f"{server.url}/tiles/{r_ + 1}/{c_}",
                                 timeout=30).json()["data"]
                    assert tile[16] == down[0]
        assert grid["grid_rows"] == 16 * info["rows"] + 1
