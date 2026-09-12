# 实验室样本交接扫码系统

样本在实验室交接窗口连续扫码时，网络超时会诱发重复提交，稍后抵达的旧请求又可能把
已确认进度推乱。本系统以**服务端裁决的期望序号**保证「操作员永远知道下一次该用哪个
序号」，且重复提交绝不重复计数。

- 前端：React 18 + TypeScript + Vite（构建后由 nginx 托管，`/api` 反代到 API）
- 后端：Python 3.12 + FastAPI + psycopg 3
- 数据库：PostgreSQL 16
- 编排：Docker Compose（web / api / db，另含一次性验收服务 `verify`）

## 运行

```bash
# 默认宿主端口：web 8080，api 8000
docker compose up --build

# 覆盖宿主端口
WEB_PORT=9090 API_PORT=9000 docker compose up --build
```

打开 <http://localhost:8080>：

1. 创建批次：输入 2–20 个唯一条码（换行/空格/逗号分隔），条码集合在服务端**冻结**；
2. 序号从 1 开始，逐条扫码提交；页面始终显示「下一次应使用序号」；
3. 请求超时后输入框中的序号与条码保持不变，直接点「提交 / 超时重试」即可；
4. 迟到或跳号请求会被拒绝，界面显示当前期望序号，进度不动；
5. 刷新/重开页面后，从服务端恢复同一批次的准确进度（批次 id 存于 localStorage）。

## 一次性验收

```bash
docker compose --profile verify run --rm verify
```

验收脚本（`verify/verify.py`，纯标准库）覆盖：

- 用裸 socket 发起提交，**只读到响应正文一个字节就断开**（模拟确认响应被截断），
  随后以相同序号+条码重发：拿到原确认（`replay=true`），确认记录只有 1 条
  —— 界面层面只增加一个样本；
- 低序号但条码不一致（迟到请求）→ `409 stale_conflict`；跳号 → `409 out_of_order`，
  均返回当前 `expected_seq` 且状态不变；
- 非本批次条码 → `409 unknown_barcode`；同一条码二次使用 → `409 barcode_already_accepted`；
- 全部接收后 `state=complete`，新扫描 → `409 completed`，但旧确认仍可幂等重放；
- 12 路相同请求并发：恰好 1 个首次接受、11 个拿到原确认；
- 9 路不同序号并发并按 `expected_seq` 推进重试：最终恰好连续确认 1..10、批次完成；
- 经 web 容器 nginx 反代访问 `/api` 完整链路；刷新 GET 与持久化状态一致；
- 批次创建的 422 校验（少于 2、多于 20、条码重复）与 404。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/batches` | `{"barcodes": ["S1", "S2", ...]}`，2–20 个唯一条码，返回冻结批次，`expected_seq=1` |
| `GET` | `/api/batches/{id}` | 批次快照：状态、期望序号、冻结条码、已确认列表 |
| `POST` | `/api/batches/{id}/submissions` | `{"seq": 1, "barcode": "S1"}` |

提交响应（200，接受或重放）：

```json
{
  "accepted": true,
  "replay": false,
  "reason": "accepted",
  "seq": 1,
  "barcode": "S1",
  "confirmed_at": "2026-09-12T08:00:00+00:00",
  "expected_seq": 2,
  "state": "open",
  "total": 3,
  "confirmed": [ ... ]
}
```

拒绝响应（409，状态不变）：`accepted=false`，`reason` 为
`stale_conflict | out_of_order | unknown_barcode | barcode_already_accepted | completed`，
并携带权威的 `expected_seq` 与当前 `confirmed` 列表。

## 并发原子裁决

对批次的每次提交都在单事务内执行：

1. `SELECT ... FROM batches WHERE id=$1 FOR UPDATE` —— 同批次请求行锁串行化；
2. 先查 `confirmations`：`seq < expected_seq` 且条码一致 → 幂等重放原确认；
3. `seq < expected_seq` 条码不一致、或 `seq > expected_seq` → 回滚并返回期望序号；
4. `seq == expected_seq` 且条码属于冻结批次 → 插入确认、推进 `expected_seq`，
   越过 `total` 时置 `state=complete`。

`confirmations (batch_id, seq)` 与 `(batch_id, barcode)` 的唯一约束、
以及行锁共同保证：任何并发交错下每个序号、每个条码都恰好确认一次。
