"""Lab sample handover API.

裁决模型（同一批次全部在一个事务内完成）：
1. SELECT batches ... FOR UPDATE 对同批次请求做行锁串行化；
2. seq == expected_seq 且条码属于本冻结批次：写入确认、expected_seq += 1；
3. seq < expected_seq 且与原确认条码一致：返回原确认（replay，幂等）；
4. seq < expected_seq 但条码不一致，或 seq > expected_seq（跳号）：
   只回当前 expected_seq，状态不变；
5. expected_seq 越过 total 后 state=complete，仅仍允许第 3 类重放。
"""
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .db import init_database, session

app = FastAPI(title="Lab Sample Handover", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BatchCreate(BaseModel):
    barcodes: list[str] = Field(min_length=2, max_length=20)


class SubmitPayload(BaseModel):
    seq: int = Field(ge=1)
    barcode: str = Field(min_length=1)


@app.on_event("startup")
def _startup() -> None:
    init_database()


@app.get("/api/health")
def health() -> dict[str, str]:
    with session() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.post("/api/batches", status_code=201)
def create_batch(payload: BatchCreate) -> dict[str, Any]:
    codes = [code.strip() for code in payload.barcodes]
    if any(not code for code in codes):
        raise HTTPException(status_code=422, detail="条码不能为空")
    if len(set(codes)) != len(codes):
        raise HTTPException(status_code=422, detail="批次内条码必须唯一")

    batch_id = uuid.uuid4()
    with session() as conn:
        conn.execute(
            "INSERT INTO batches (id, total) VALUES (%s, %s)",
            (batch_id, len(codes)),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO batch_items (batch_id, position, barcode) "
                "VALUES (%s, %s, %s)",
                [(batch_id, index, code) for index, code in enumerate(codes, start=1)],
            )
        conn.commit()

    return {
        "id": str(batch_id),
        "state": "open",
        "expected_seq": 1,
        "total": len(codes),
        "barcodes": codes,
        "confirmed": [],
    }


def _state(conn, batch_id: uuid.UUID) -> dict[str, Any]:
    batch = conn.execute(
        "SELECT id, state, expected_seq, total FROM batches WHERE id = %s",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")

    items = conn.execute(
        "SELECT barcode FROM batch_items WHERE batch_id = %s ORDER BY position",
        (batch_id,),
    ).fetchall()
    confirmed = conn.execute(
        """
        SELECT seq, barcode, confirmed_at
          FROM confirmations
         WHERE batch_id = %s
         ORDER BY seq
        """,
        (batch_id,),
    ).fetchall()
    return {
        "id": str(batch["id"]),
        "state": batch["state"],
        "expected_seq": batch["expected_seq"],
        "total": batch["total"],
        "barcodes": [item["barcode"] for item in items],
        "confirmed": [
            {
                "seq": row["seq"],
                "barcode": row["barcode"],
                "confirmed_at": row["confirmed_at"].isoformat(),
            }
            for row in confirmed
        ],
    }


def _parse_batch_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="批次 id 格式无效")


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    parsed = _parse_batch_id(batch_id)
    with session() as conn:
        return _state(conn, parsed)


def _conflict(reason: str, state: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "replay": False,
            "accepted": False,
            "reason": reason,
            "expected_seq": state["expected_seq"],
            "state": state["state"],
            "total": state["total"],
            "confirmed": state["confirmed"],
        },
    )


@app.post("/api/batches/{batch_id}/submissions")
def submit(batch_id: str, payload: SubmitPayload) -> JSONResponse:
    parsed = _parse_batch_id(batch_id)
    barcode = payload.barcode.strip()
    if not barcode:
        raise HTTPException(status_code=422, detail="条码不能为空")
    seq = payload.seq

    with session() as conn:
        # 行锁：同批次的并发提交在此串行裁决，读-判-写在同一事务中原子完成。
        batch = conn.execute(
            "SELECT state, expected_seq, total FROM batches WHERE id = %s FOR UPDATE",
            (parsed,),
        ).fetchone()
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")

        expected = batch["expected_seq"]
        total = batch["total"]

        # 已确认的序号：条码一致则幂等重放原确认；不一致则拒绝。
        prior = None
        if seq < expected:
            prior = conn.execute(
                "SELECT seq, barcode, confirmed_at FROM confirmations "
                "WHERE batch_id = %s AND seq = %s",
                (parsed, seq),
            ).fetchone()

        if prior is not None and prior["barcode"] == barcode:
            conn.rollback()  # 只读重放，释放行锁
            return JSONResponse(
                content={
                    "replay": True,
                    "accepted": True,
                    "reason": "replay",
                    "seq": prior["seq"],
                    "barcode": prior["barcode"],
                    "confirmed_at": prior["confirmed_at"].isoformat(),
                    **{
                        "expected_seq": expected,
                        "state": batch["state"],
                        "total": total,
                        "confirmed": _state(conn, parsed)["confirmed"],
                    },
                }
            )

        # 批次完成后只接受旧确认重放（上面已处理），拒绝一切新扫描。
        if batch["state"] == "complete":
            conn.rollback()
            return _conflict("completed", _state(conn, parsed))

        if seq < expected:
            conn.rollback()  # 低序号但内容不一致（迟到的旧请求）
            return _conflict("stale_conflict", _state(conn, parsed))

        if seq > expected:
            conn.rollback()  # 跳号
            return _conflict("out_of_order", _state(conn, parsed))

        # seq == expected：条码必须属于创建时冻结的批次。
        member = conn.execute(
            "SELECT 1 FROM batch_items WHERE batch_id = %s AND barcode = %s",
            (parsed, barcode),
        ).fetchone()
        if member is None:
            conn.rollback()
            return _conflict("unknown_barcode", _state(conn, parsed))

        # 每个条码只能接收一次（唯一约束兜底；正常路径不会触发）。
        duplicate = conn.execute(
            "SELECT 1 FROM confirmations WHERE batch_id = %s AND barcode = %s",
            (parsed, barcode),
        ).fetchone()
        if duplicate is not None:
            conn.rollback()
            return _conflict("barcode_already_accepted", _state(conn, parsed))

        new_row = conn.execute(
            "INSERT INTO confirmations (batch_id, seq, barcode) "
            "VALUES (%s, %s, %s) RETURNING confirmed_at",
            (parsed, seq, barcode),
        ).fetchone()

        new_expected = expected + 1
        new_state = "complete" if new_expected > total else "open"
        conn.execute(
            "UPDATE batches SET expected_seq = %s, state = %s WHERE id = %s",
            (new_expected, new_state, parsed),
        )
        conn.commit()

        return JSONResponse(
            content={
                "replay": False,
                "accepted": True,
                "reason": "accepted",
                "seq": seq,
                "barcode": barcode,
                "confirmed_at": new_row["confirmed_at"].isoformat(),
                "expected_seq": new_expected,
                "state": new_state,
                "total": total,
                "confirmed": _state(conn, parsed)["confirmed"],
            }
        )
