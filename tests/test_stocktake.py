"""库位盘点：多行核销、两层扣减、整单拒绝、原值固化、隔离/撤销产出可核销。"""
from __future__ import annotations

from decimal import Decimal

from app import service
from app.db import session_scope
from app.models import DEFAULT_LOCATION_CODE, Batch, BatchLocation, Stocktake


# ------------------------------------------------------------ 辅助


def _raw(client, batch_no, qty, unit="kg", key=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty)},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    total = sum((Decimal(str(q)) for _, q, *_ in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [
                {"batch_no": b, "quantity": str(q), **(
                    {"location_code": t[0]} if t and t[0] is not None else {})}
                for b, q, *t in inputs
            ],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _transfer(client, no, batch, src, dst, qty, key=None):
    return client.post(
        "/transfers",
        json={"transfer_no": no, "batch_no": batch,
              "source_location": src, "target_location": dst, "quantity": str(qty)},
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _stocktake(client, no, items, key=None, note=None):
    return client.post(
        "/stocktakes",
        json={"stocktake_no": no, "items": [
            {"batch_no": b, "location_code": loc,
             "book_qty": str(book), "actual_qty": str(act)}
            for b, loc, book, act in items
        ], "note": note},
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _get_stocktake(client, no):
    return client.get(f"/stocktakes/{no}")


def _loc_map(client, batch_no):
    body = client.get(f"/batches/{batch_no}/locations").json()
    return ({row["location_code"]: Decimal(str(row["quantity"]))
             for row in body["locations"]},
            Decimal(str(body["remaining_qty"])))


# ------------------------------------------------------------ 成功路径


def test_stocktake_multi_line_deducts_location_and_global(client):
    """多行盘点：盘亏行扣两层，零差额行不动；库位之和仍等于全局余量。"""
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50", unit="kg")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    # RM-1: DEFAULT=70, A=30；RM-2: DEFAULT=50
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "70", "65"),  # 亏 5
        ("RM-1", "A", "30", "30"),                   # 无亏
        ("RM-2", DEFAULT_LOCATION_CODE, "50", "40"),  # 亏 10
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stocktake_no"] == "ST-1"
    assert body["status"] == "ADJUSTED"
    assert [it["line_no"] for it in body["items"]] == [1, 2, 3]
    assert body["items"] == [
        {"line_no": 1, "batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "unit": "kg", "book_qty": 70.0, "actual_qty": 65.0, "diff_qty": 5.0},
        {"line_no": 2, "batch_no": "RM-1", "location_code": "A",
         "unit": "kg", "book_qty": 30.0, "actual_qty": 30.0, "diff_qty": 0.0},
        {"line_no": 3, "batch_no": "RM-2", "location_code": DEFAULT_LOCATION_CODE,
         "unit": "kg", "book_qty": 50.0, "actual_qty": 40.0, "diff_qty": 10.0},
    ]
    assert "created_at" in body and body["created_at"]
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("65"), "A": Decimal("30")}
    assert _loc_map(client, "RM-2")[0] == {DEFAULT_LOCATION_CODE: Decimal("40")}
    # 两层恒等
    for no in ("RM-1", "RM-2"):
        locs, global_qty = _loc_map(client, no)
        assert sum(locs.values()) == global_qty
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 95.0
    assert client.get("/batches/RM-2").json()["remaining_qty"] == 40.0


def test_stocktake_full_location_loss_to_zero(client):
    """实点量为 0、账面量为正：整库位清零，差额等于账面量。"""
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = _stocktake(client, "ST-1", [
        ("RM-1", "A", "30", "0"),
        ("RM-1", DEFAULT_LOCATION_CODE, "70", "70"),
    ])
    assert r.status_code == 201, r.text
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {"A": Decimal("0"), DEFAULT_LOCATION_CODE: Decimal("70")}
    assert global_qty == Decimal("70")


def test_stocktake_zero_book_zero_actual_allowed_but_needs_another_loss(client):
    """账面量/实点量允许为 0（非负），但整单须另有一行盘亏。"""
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "10")
    # A 已被全部消耗（经投料），库位行余额 0
    _produce(client, "FG-1", [("RM-1", "10", "A")], out_qty=10)
    r = _stocktake(client, "ST-1", [
        ("RM-1", "A", "0", "0"),                     # 零账面零实点，无亏
        ("RM-1", DEFAULT_LOCATION_CODE, "90", "88"),  # 亏 2
    ])
    assert r.status_code == 201, r.text
    assert _loc_map(client, "RM-1")[0] == {
        "A": Decimal("0"), DEFAULT_LOCATION_CODE: Decimal("88")}


def test_stocktake_query_returns_status_lines_and_unit(client):
    _raw(client, "RM-1", "100", unit="L")
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "97"),
    ], note="月度盘点")
    assert r.status_code == 201
    r = _get_stocktake(client, "ST-1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ADJUSTED"
    assert body["note"] == "月度盘点"
    assert body["items"] == [{
        "line_no": 1, "batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
        "unit": "L", "book_qty": 100.0, "actual_qty": 97.0, "diff_qty": 3.0,
    }]


def test_stocktake_record_immutable_after_later_movements(client):
    """后续库存流动不改写盘点单固化的原值；全局余量字段仍表示实存。"""
    _raw(client, "RM-1", "100")
    _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "90"),
    ])
    # 盘点后再调拨与第二次盘点
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "20")
    _stocktake(client, "ST-2", [
        ("RM-1", DEFAULT_LOCATION_CODE, "70", "60"),
        ("RM-1", "A", "20", "20"),
    ])
    first = _get_stocktake(client, "ST-1").json()
    assert first["items"][0]["book_qty"] == 100.0
    assert first["items"][0]["actual_qty"] == 90.0
    assert first["items"][0]["diff_qty"] == 10.0
    locs, global_qty = _loc_map(client, "RM-1")
    assert locs == {DEFAULT_LOCATION_CODE: Decimal("60"), "A": Decimal("20")}
    assert global_qty == Decimal("80")
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 80.0


def test_stocktake_quarantined_batch_allowed(client):
    """隔离批次（含疑似）仍可盘点核销。"""
    _raw(client, "RM-1", "100")
    r = client.post("/contamination", json={"batch_no": "RM-1", "note": "疑似"},
                    headers={"Idempotency-Key": "f1"})
    assert r.status_code == 200
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "80"),
    ])
    assert r.status_code == 201, r.text
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 80.0


def test_stocktake_cancelled_output_allowed(client):
    """已撤销产出批次仍可盘点核销（撤销只禁止投料，不禁止盘点）。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    assert client.post("/productions/MIX-1/cancellation",
                       json={"cancel_reason": "录入错误"},
                       headers={"Idempotency-Key": "c1"}).status_code == 200
    r = _stocktake(client, "ST-1", [
        ("MIX-1", DEFAULT_LOCATION_CODE, "40", "25"),
    ])
    assert r.status_code == 201, r.text
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 25.0


# ------------------------------------------------------------ 整单拒绝


def test_stocktake_duplicate_line_rejected(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "98"),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_STOCKTAKE_LINE"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_actual_exceeds_book_rejected(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "100.5"),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCKTAKE_ACTUAL_EXCEEDS_BOOK"
    # 第一行合法、第二行实点高于账面：整单拒绝
    _raw(client, "RM-2", "50")
    r = _stocktake(client, "ST-2", [
        ("RM-2", DEFAULT_LOCATION_CODE, "50", "49"),
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "101"),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCKTAKE_ACTUAL_EXCEEDS_BOOK"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert client.get("/batches/RM-2").json()["remaining_qty"] == 50.0


def test_stocktake_without_any_loss_rejected(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "50")
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "100"),
        ("RM-2", DEFAULT_LOCATION_CODE, "50", "50"),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "STOCKTAKE_NO_LOSS"
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_unknown_batch_rejected_entirely(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),
        ("GHOST", DEFAULT_LOCATION_CODE, "1", "0"),
    ])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    # 合法的第一行也不扣减
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_unknown_location_rejected_entirely(client):
    _raw(client, "RM-1", "100")
    r = _stocktake(client, "ST-1", [
        ("RM-1", "NOPE", "0", "0"),  # 该批次从未在此库位有余额行
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),  # 本行有亏，使整单进入核对
    ])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "LOCATION_NOT_FOUND"
    details = r.json()["error"]["details"]
    assert details == {"batch_no": "RM-1", "location_code": "NOPE"}
    assert _get_stocktake(client, "ST-1").status_code == 404
    # 第二行虽合法也不扣减
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_stocktake_book_mismatch_after_transfer_rejected(client):
    """账面已变动（被调拨改过）：申报旧账面量 -> STOCKTAKE_BOOK_MISMATCH。"""
    _raw(client, "RM-1", "100")
    _transfer(client, "T1", "RM-1", DEFAULT_LOCATION_CODE, "A", "30")
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "90"),  # 实存已为 70
    ])
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "STOCKTAKE_BOOK_MISMATCH"
    assert body["error"]["details"] == {
        "batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
        "submitted_book_qty": "100", "current_qty": "70.0000"}
    # 余额不变、不留单
    assert _loc_map(client, "RM-1")[0] == {
        DEFAULT_LOCATION_CODE: Decimal("70"), "A": Decimal("30")}
    assert _get_stocktake(client, "ST-1").status_code == 404


def test_stocktake_book_mismatch_after_concurrent_production(client):
    """账面已变动（被投料改过）：同样整单拒绝。"""
    _raw(client, "RM-1", "100")
    _produce(client, "FG-1", [("RM-1", "40", None)], out_qty=40)
    r = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),
    ])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STOCKTAKE_BOOK_MISMATCH"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 60.0


def test_stocktake_no_reuse_rejected(client):
    _raw(client, "RM-1", "100")
    r1 = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "100", "99"),
    ])
    assert r1.status_code == 201
    # 同单号、不同内容、不同幂等键：单号唯一拒绝
    r2 = _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "99", "98"),
    ], key="other-key")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "STOCKTAKE_NO_REUSED"
    # 只扣一次
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 99.0


def test_stocktake_negative_and_malformed_qty_rejected(client):
    _raw(client, "RM-1", "100")
    for book, act in [("-1", "0"), ("100", "-0.1"), ("abc", "1")]:
        r = client.post(
            "/stocktakes",
            json={"stocktake_no": f"ST-{book}-{act}", "items": [
                {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
                 "book_qty": book, "actual_qty": act}]},
            headers={"Idempotency-Key": f"k-{book}-{act}"},
        )
        assert r.status_code == 422, (book, act, r.text)
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_stocktake_empty_items_rejected(client):
    _raw(client, "RM-1", "100")
    r = client.post("/stocktakes", json={"stocktake_no": "ST-1", "items": []},
                    headers={"Idempotency-Key": "k1"})
    assert r.status_code == 422


def test_stocktake_requires_idempotency_key(client):
    _raw(client, "RM-1", "100")
    r = client.post("/stocktakes", json={"stocktake_no": "ST-1", "items": [
        {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "book_qty": "100", "actual_qty": "99"}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_stocktake_get_unknown_404(client):
    r = _get_stocktake(client, "GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "STOCKTAKE_NOT_FOUND"


# ------------------------------------------------------------ 幂等


def test_stocktake_idempotent_replay(client):
    _raw(client, "RM-1", "100")
    payload = {"stocktake_no": "ST-1", "items": [
        {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "book_qty": "100", "actual_qty": "90"}]}
    r1 = client.post("/stocktakes", json=payload, headers={"Idempotency-Key": "k1"})
    r2 = client.post("/stocktakes", json=payload, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == r2.status_code == 201
    # 时间戳一致（首次固化响应原样返回）
    assert r1.json() == r2.json()
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_stocktake_idempotency_conflict_on_different_body(client):
    _raw(client, "RM-1", "100")
    r1 = client.post("/stocktakes", json={"stocktake_no": "ST-1", "items": [
        {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "book_qty": "100", "actual_qty": "90"}]},
        headers={"Idempotency-Key": "shared"})
    assert r1.status_code == 201
    r2 = client.post("/stocktakes", json={"stocktake_no": "ST-2", "items": [
        {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "book_qty": "90", "actual_qty": "80"}]},
        headers={"Idempotency-Key": "shared"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 第二笔未生效
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_failed_stocktake_not_stored_can_retry_same_key(client):
    """失败请求不固化幂等记录：修正后可用同一 key 重试成功。"""
    _raw(client, "RM-1", "100")
    # 首次账面不符失败
    r = client.post("/stocktakes", json={"stocktake_no": "ST-1", "items": [
        {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "book_qty": "99", "actual_qty": "98"}]},
        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 409
    # 同 key、修正为正确账面后成功（非同键异体冲突）
    r = client.post("/stocktakes", json={"stocktake_no": "ST-1", "items": [
        {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
         "book_qty": "100", "actual_qty": "98"}]},
        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201, r.text
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 98.0


# ------------------------------------------------------------ 撤销联动


def test_cancel_blocked_when_output_has_stocktake_loss(client):
    """发生盘亏的生产产出不得撤销原生产单。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    r = _stocktake(client, "ST-1", [
        ("MIX-1", DEFAULT_LOCATION_CODE, "40", "35"),
    ])
    assert r.status_code == 201
    r = client.post("/productions/MIX-1/cancellation",
                    json={"cancel_reason": "录入错误"},
                    headers={"Idempotency-Key": "c1"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "OUTPUT_HAS_STOCKTAKE_LOSS"
    assert body["error"]["details"] == {"batch_no": "MIX-1",
                                        "stocktake_nos": ["ST-1"]}
    # 未返还、未撤销
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 60.0
    assert client.get("/productions/MIX-1").json()["is_cancelled"] is False


def test_cancel_allowed_when_output_stocktake_has_zero_diff(client):
    """盘点差额为 0（无盘亏）不阻止撤销：历史投料与谱系不因盘点改变。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    r = _stocktake(client, "ST-1", [
        # 另一批次有真实盘亏，MIX-1 本行无亏
        ("RM-1", DEFAULT_LOCATION_CODE, "60", "55"),
        ("MIX-1", DEFAULT_LOCATION_CODE, "40", "40"),
    ])
    assert r.status_code == 201
    r = client.post("/productions/MIX-1/cancellation",
                    json={"cancel_reason": "录入错误"},
                    headers={"Idempotency-Key": "c1"})
    assert r.status_code == 200, r.text
    # 撤销返还 RM-1 原投料 40（盘点扣减不受影响：55+40=95）
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 95.0


def test_source_batch_stocktake_loss_does_not_block_its_consumers_cancel(client):
    """仅当盘亏发生在产出自身时阻止撤销；来源批次盘亏不影响。"""
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    # 来源 RM-1 盘亏
    assert _stocktake(client, "ST-1", [
        ("RM-1", DEFAULT_LOCATION_CODE, "60", "50"),
    ]).status_code == 201
    r = client.post("/productions/MIX-1/cancellation",
                    json={"cancel_reason": "录入错误"},
                    headers={"Idempotency-Key": "c1"})
    assert r.status_code == 200, r.text
    # 返还原投料 40：RM-1 50+40=90（盘点核销不因撤销回滚）
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_stocktake_after_cancel_then_second_cancel_remains_rejected(client):
    _raw(client, "RM-1", "100")
    _produce(client, "MIX-1", [("RM-1", "40", None)], out_qty=40)
    client.post("/productions/MIX-1/cancellation",
                json={"cancel_reason": "录入错误"},
                headers={"Idempotency-Key": "c1"})
    assert _stocktake(client, "ST-1", [
        ("MIX-1", DEFAULT_LOCATION_CODE, "40", "30"),
    ]).status_code == 201
    r = client.post("/productions/MIX-1/cancellation",
                    json={"cancel_reason": "又错了"},
                    headers={"Idempotency-Key": "c2"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRODUCTION_ALREADY_CANCELLED"


# ------------------------------------------------------------ 服务层：两层恒等


def test_service_stocktake_persists_immutable_rows(engine):
    """服务层直测：盘点行固化原值，DB 层检查约束生效。"""
    from app.schemas import RawBatchIn, StocktakeIn

    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity=Decimal("100"))
        )
        service.submit_stocktake(s, StocktakeIn(stocktake_no="ST-1", items=[
            {"batch_no": "RM-1", "location_code": DEFAULT_LOCATION_CODE,
             "book_qty": Decimal("100"), "actual_qty": Decimal("83.5")},
        ]))
    with session_scope() as s:
        st = s.get(Stocktake, 1)
        assert st.status == "ADJUSTED"
        row = st.items[0]
        assert (row.book_qty, row.actual_qty, row.diff_qty) == (
            Decimal("100.0000"), Decimal("83.5000"), Decimal("16.5000"))
        assert row.unit == "kg"
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("83.5000")
        loc = s.get(BatchLocation, ("RM-1", DEFAULT_LOCATION_CODE))
        assert loc.qty == Decimal("83.5000")
