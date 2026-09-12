"""一次性验收脚本（docker compose --profile verify run --rm verify）。

覆盖需求：
- 创建 2–20 唯一条码的冻结批次；
- 先截断一次确认响应（模拟网络超时），再用相同序号+条码重发：界面层面只多一个样本；
- 低序号内容不一致（迟到请求）/ 跳号：返回当前期望序号，状态不变；
- 条码必须属于冻结批次且只能接收一次；
- 全部接收后完成，完成后拒绝新扫描，但仍可重放旧确认；
- 同批次并发请求原子裁决；
- 刷新（重新 GET）后进度与持久化状态一致；
- web 容器真实反联 API（nginx 代理 /api）且页面已构建。
"""
import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API_BASE = os.environ.get("API_BASE", "http://api:8000")
WEB_BASE = os.environ.get("WEB_BASE", "http://web:80")

PASS = 0
FAIL = 0


def check(condition: bool, message: str) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {message}")
    else:
        FAIL += 1
        print(f"  FAIL  {message}")


def wait_ready(base: str, path: str, name: str, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=3) as res:
                if res.status == 200:
                    print(f"[ready] {name}")
                    return
        except Exception as exc:  # noqa: BLE001 - startup polling
            last = str(exc)
            time.sleep(1)
    print(f"[fatal] {name} not ready at {base}{path}: {last}")
    sys.exit(2)


def post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(base: str, path: str) -> tuple[int, dict | str]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as res:
            raw = res.read()
            ctype = res.headers.get("Content-Type", "")
            return res.status, (json.loads(raw) if "json" in ctype else raw.decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def truncated_post_then_close(base: str, path: str, payload: dict) -> int:
    """发送 POST，只读到状态行/响应头与正文的极小一部分即粗暴关连接。

    服务端在发送响应前已提交事务，这精确模拟“确认已落库但响应在网络上被截断”。
    """
    host_port = base.removeprefix("http://")
    host, _, port = host_port.partition(":")
    port = int(port or "80")
    body = json.dumps(payload).encode()
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(
            (
                f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            + body
        )
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        # 只读正文一个字节，随后直接关闭 —— 客户端丢失了绝大部分确认响应
        if rest:
            sock.recv(1)
        else:
            try:
                sock.recv(1)
            except OSError:
                pass
        status_line = head.split(b"\r\n", 1)[0].decode()
        return int(status_line.split()[1])


def make_batch(codes: list[str]) -> dict:
    status, body = post(API_BASE, "/api/batches", {"barcodes": codes})
    assert status == 201, f"create batch failed: {status} {body}"
    return body


def main() -> int:
    wait_ready(API_BASE, "/api/health", "api")
    wait_ready(WEB_BASE, "/api/health", "web -> api proxy")

    print("\n[1] web 容器提供真实页面并同源联调 API")
    status, index = get(WEB_BASE, "/")
    check(status == 200 and isinstance(index, str), "GET / 返回构建后的 index.html")
    check('<div id="app"></div>' in index, "页面挂载点 #app 存在")
    check("<script" in index and "/assets/" in index, "页面引用打包后的 JS 资源")

    print("\n[2] 创建批次校验：2–20 个唯一条码")
    for label, codes in (
        ("仅 1 个条码", ["X1"]),
        ("条码重复", ["A", "A"]),
        ("21 个条码", [f"C{i:02d}" for i in range(21)]),
    ):
        status, body = post(API_BASE, "/api/batches", {"barcodes": codes})
        check(status == 422, f"{label} 被拒绝 422（实际 {status} {str(body)[:80]}）")
    status, body = get(API_BASE, "/api/batches/11111111-1111-1111-1111-111111111111")
    check(status == 404, "不存在的批次返回 404")

    print("\n[3] 截断确认响应后用相同序号重发：只增加一个样本")
    batch = make_batch(["T1", "T2", "T3"])
    bid = batch["id"]
    check(batch["expected_seq"] == 1 and batch["state"] == "open", "新批次期望序号为 1、状态 open")

    code = truncated_post_then_close(
        API_BASE, f"/api/batches/{bid}/submissions", {"seq": 1, "barcode": "T1"}
    )
    check(code == 200, f"被截断的首次提交服务端已处理（HTTP {code}）")

    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 1, "barcode": "T1"})
    check(status == 200 and body["replay"] is True, "相同序号+相同条码重发返回原确认（replay=true）")
    check(body["expected_seq"] == 2, "重发后期望序号仍为 2，未重复计数")
    check(len(body["confirmed"]) == 1 and body["confirmed"][0]["barcode"] == "T1",
          "确认记录只有 1 条（界面只会增加一个样本）")

    status, state = get(API_BASE, f"/api/batches/{bid}")
    check(isinstance(state, dict) and state["confirmed"] == body["confirmed"],
          "重新 GET（等同刷新页面）进度与重放后完全一致")

    print("\n[4] 迟到请求（低序号内容不一致）与跳号：显示期望序号且状态不变")
    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 1, "barcode": "T2"})
    check(status == 409 and body["reason"] == "stale_conflict", "低序号但条码不同 → 409 stale_conflict")
    check(body["expected_seq"] == 2 and len(body["confirmed"]) == 1, "迟到请求不改变状态")

    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 5, "barcode": "T2"})
    check(status == 409 and body["reason"] == "out_of_order", "高序号（跳号）→ 409 out_of_order")
    check(body["expected_seq"] == 2 and len(body["confirmed"]) == 1, "跳号请求不改变状态")

    print("\n[5] 条码必须属于冻结批次，且每个只能接收一次")
    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 2, "barcode": "ZZZ"})
    check(status == 409 and body["reason"] == "unknown_barcode", "非本批次条码 → 409 unknown_barcode")
    check(body["expected_seq"] == 2, "被拒后期望序号仍为 2")

    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 2, "barcode": "T2"})
    check(status == 200 and body["accepted"] and not body["replay"], "seq 2 / T2 正常确认")
    check(body["expected_seq"] == 3, "期望序号推进到 3")

    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 3, "barcode": "T2"})
    check(status == 409 and body["reason"] == "barcode_already_accepted",
          "已接收条码再次使用 → 409 barcode_already_accepted")
    check(body["expected_seq"] == 3, "重复条码不推进序号")

    print("\n[6] 全部接收后完成，拒绝新扫描，但旧确认仍可幂等重放")
    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 3, "barcode": "T3"})
    check(status == 200 and body["state"] == "complete" and body["expected_seq"] == 4,
          "最后一条确认后批次 complete，期望序号 = total + 1")

    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 4, "barcode": "T1"})
    check(status == 409 and body["reason"] == "completed", "完成后新扫描 → 409 completed")
    check(body["expected_seq"] == 4 and len(body["confirmed"]) == 3, "完成态确认记录仍为 3 条")

    status, body = post(API_BASE, f"/api/batches/{bid}/submissions", {"seq": 2, "barcode": "T2"})
    check(status == 200 and body["replay"] is True, "完成后重放旧确认仍返回原确认")
    check(len(body["confirmed"]) == 3, "重放不增加确认数")

    print("\n[7] 同一批次并发请求原子裁决（同序号并发，恰好一个生效）")
    cbatch = make_batch([f"P{i}" for i in range(1, 11)])
    cid = cbatch["id"]
    barrier = threading.Barrier(12)

    def fire(seq: int, barcode: str) -> tuple[int, dict]:
        barrier.wait()
        return post(API_BASE, f"/api/batches/{cid}/submissions",
                    {"seq": seq, "barcode": barcode})

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: fire(1, "P1"), range(12)))
    accepted = [r for s, r in results if s == 200 and r.get("accepted") and not r.get("replay")]
    replayed = [r for s, r in results if s == 200 and r.get("replay")]
    check(len(accepted) == 1, f"12 个相同并发提交中仅 1 个被首次接受（实际 {len(accepted)}）")
    check(len(replayed) == 11, f"其余 11 个全部拿到原确认重放（实际 {len(replayed)}）")
    status, state = get(API_BASE, f"/api/batches/{cid}")
    check(isinstance(state, dict) and state["expected_seq"] == 2 and len(state["confirmed"]) == 1,
          "并发裁决后持久化状态：期望序号 2、确认 1 条")

    print("\n[8] 乱序并发压力后状态仍然连续准确")
    barrier2 = threading.Barrier(9)

    def fire2(item: tuple[int, str]) -> tuple[int, dict]:
        # 各线程持自己固定的 (序号, 条码) 同时发起；若轮到自己之前被锁竞争
        # 推后（409 out_of_order），则短暂等待后重试 —— 模拟扫码端的真实行为。
        seq, barcode = item
        barrier2.wait()
        for _ in range(200):
            status, resp = post(API_BASE, f"/api/batches/{cid}/submissions",
                                {"seq": seq, "barcode": barcode})
            if status == 200 and resp.get("accepted"):
                return status, resp
            if resp.get("reason") not in ("out_of_order", "stale_conflict"):
                return status, resp  # 意外结果，交给断言暴露
            time.sleep(0.02)
        return 409, {"reason": "gave_up"}

    with ThreadPoolExecutor(max_workers=9) as pool:
        list(pool.map(fire2, [(i, f"P{i}") for i in range(2, 11)]))
    status, state = get(API_BASE, f"/api/batches/{cid}")
    seqs = sorted(c["seq"] for c in state["confirmed"])
    check(seqs == list(range(1, 11)), f"并发+重试后恰好连续确认 1..10（实际 {seqs}）")
    check(isinstance(state, dict) and state["state"] == "complete"
          and state["expected_seq"] == 11, "并发结束后批次完成、期望序号 11")
    check([c["barcode"] for c in sorted(state["confirmed"], key=lambda c: c["seq"])]
          == [f"P{i}" for i in range(1, 11)], "每条冻结条码恰好确认一次")

    print("\n[9] 通过 web 容器反代走一遍提交链路（页面真实联调路径）")
    wbatch = make_batch(["W1", "W2"])
    status, body = post(WEB_BASE, f"/api/batches/{wbatch['id']}/submissions",
                        {"seq": 1, "barcode": "W1"})
    check(status == 200 and body["expected_seq"] == 2, "经 nginx /api 代理提交成功")
    status, body = post(WEB_BASE, f"/api/batches/{wbatch['id']}/submissions",
                        {"seq": 1, "barcode": "W1"})
    check(status == 200 and body["replay"] is True, "经代理重放同样幂等")

    total_pass = PASS
    print(f"\n结果：{total_pass} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
