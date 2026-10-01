"""盘点并发测试：盘点与投料/分装/调拨/撤销/盘点交错时绝不超扣、两层余额恒等。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import text

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import DEFAULT_LOCATION_CODE, Batch
from app.schemas import (
    CancelIn,
    ProductionIn,
    RawBatchIn,
    SplitIn,
    StocktakeIn,
    TransferIn,
)


def _qty(v: str) -> Decimal:
    return Decimal(v)


def _register(no: str = "RM-1", qty: str = "100") -> None:
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no=no, unit="kg", quantity=_qty(qty))
        )


def _totals(batch_no: str = "RM-1") -> tuple[Decimal, Decimal]:
    """返回 (全局余量, 全部库位余额之和)。"""
    with session_scope() as s:
        global_qty = s.get(Batch, batch_no).remaining_qty
        loc_sum = s.execute(
            text("SELECT COALESCE(SUM(qty), 0) FROM batch_locations WHERE batch_no=:b"),
            {"b": batch_no},
        ).scalar_one()
        return Decimal(global_qty), Decimal(loc_sum)


def _loc_rows(batch_no: str = "RM-1") -> dict[str, Decimal]:
    with session_scope() as s:
        return {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no=:b"),
                {"b": batch_no},
            ).all()
        }


def _post_state(batch_no: str = "RM-1", extra: str | None = None) -> dict[str, Decimal | str | int | None]:
    """单一连接内读取全部对账所需状态——不得在外层 session_scope 内调用本函数
    （SQLite 写事务持 BEGIN IMMEDIATE 全库写锁，嵌套连接会自死锁）。"""
    with session_scope() as s:
        rows = {
            r[0]: Decimal(r[1])
            for r in s.execute(
                text("SELECT location_code, qty FROM batch_locations WHERE batch_no=:b"),
                {"b": batch_no},
            ).all()
        }
        global_qty = Decimal(s.get(Batch, batch_no).remaining_qty)
        st_count = s.execute(text("SELECT COUNT(*) FROM stocktakes")).scalar_one()
        extra_batch = s.get(Batch, extra) if extra else None
    return {"rows": rows, "global_qty": global_qty,
            "st_count": st_count, "extra_exists": extra_batch is not None}


def _stocktake(no: str, lines: list[tuple[str, str, str, str]]):
    return StocktakeIn(
        stocktake_no=no,
        items=[
            {"batch_no": b, "location_code": loc,
             "book_qty": _qty(book), "actual_qty": _qty(act)}
            for b, loc, book, act in lines
        ],
    )


def _run_concurrent(workers: int, fn) -> list[str]:
    barrier = threading.Barrier(workers)
    results: list[str] = []

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            fn(idx)
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:  # 不应出现死锁/数据库错误
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))
    return results


def test_concurrent_stocktakes_against_same_location_never_overdeduct(engine):
    """8 个线程各对 DEFAULT（100）盘点核销 15（实点 85）：只有第一个能以
    账面 100 核对成功，其余因账面已变动（STOCKTAKE_BOOK_MISMATCH）拒绝；
    恰好扣一次，两层余额恒等。"""
    _register(qty="100")

    def fn(idx: int) -> None:
        with session_scope() as s:
            service.submit_stocktake(
                s,
                _stocktake(f"ST-{idx}", [
                    ("RM-1", DEFAULT_LOCATION_CODE, "100", "85")]),
            )

    results = _run_concurrent(8, fn)
    assert results.count("ok") == 1, results
    assert results.count("STOCKTAKE_BOOK_MISMATCH") == 7, results
    assert not [r for r in results if r.startswith("ERROR")], results
    g, loc = _totals()
    assert g == loc == Decimal("85.0000")
    with session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM stocktakes")).scalar_one() == 1


def test_concurrent_duplicate_stocktake_no_only_one_succeeds(engine):
    """同一盘点单号并发：唯一约束兜底，恰好一笔成功。"""
    _register(qty="100")

    def fn(idx: int) -> None:
        with session_scope() as s:
            service.submit_stocktake(
                s,
                _stocktake("ST-DUP", [
                    ("RM-1", DEFAULT_LOCATION_CODE, "100", str(99 - idx))]),
            )

    results = _run_concurrent(2, fn)
    assert results.count("ok") == 1, results
    # 输家要么单号复用，要么（更晚核对账面时）账面不符——总之不固化第二张单
    assert results.count("ok") + results.count("STOCKTAKE_NO_REUSED") \
        + results.count("STOCKTAKE_BOOK_MISMATCH") == 2, results
    with session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM stocktakes")).scalar_one() == 1
    g, loc = _totals()
    assert g == loc  # 只扣一次


def test_concurrent_stocktake_and_production_same_batch(engine):
    """盘点核销 40（100->60）与指定 DEFAULT 投料 40 并发：
    两者都按账面 100 起步。一成一拒（后核对者账面已变/余额不足），
    绝不超扣；最终库存自洽。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    _stocktake("ST-1", [
                        ("RM-1", DEFAULT_LOCATION_CODE, "100", "60")]),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=_qty("40"),
                        inputs=[{"batch_no": "RM-1", "quantity": _qty("40"),
                                 "location_code": DEFAULT_LOCATION_CODE}],
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, produce_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    state = _post_state("RM-1", extra="FG-1")
    rows = state["rows"]
    st_count = state["st_count"]
    global_qty = state["global_qty"]
    fg1_exists = state["extra_exists"]

    # 两种串行结局：
    # 盘点先（QC 表锁）：DEFAULT 100->60；投料随后仍有 60>=40，再扣 40 -> 20；
    #   FG-1 存在，盘点单 1 张，两笔都成功。
    # 投料先：DEFAULT 100->60（FG-1=40）；盘点取锁后以账面 100 核对当前 60，
    #   STOCKTAKE_BOOK_MISMATCH 整单拒绝、不留单。
    if results["stocktake"] == "ok":
        assert results["produce"] == "ok", results
        assert st_count == 1
        assert fg1_exists
        assert rows == {DEFAULT_LOCATION_CODE: Decimal("20.0000")}
        assert global_qty == Decimal("20.0000")
    else:
        assert results["stocktake"] == "STOCKTAKE_BOOK_MISMATCH", results
        assert results["produce"] == "ok", results
        assert st_count == 0
        assert fg1_exists
        assert rows == {DEFAULT_LOCATION_CODE: Decimal("60.0000")}
        assert global_qty == Decimal("60.0000")
    assert sum(rows.values()) == global_qty


def test_concurrent_stocktake_and_split_different_batches(engine):
    """盘点 RM-1 与分装 RM-2 互不相关：均成功，各自扣减、两层恒等。"""
    _register("RM-1", "100")
    _register("RM-2", "100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    _stocktake("ST-1", [
                        ("RM-1", DEFAULT_LOCATION_CODE, "100", "70")]),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def split_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.split_batch(
                    s, "RM-2",
                    SplitIn(splits=[{"batch_no": "RM-2-S", "quantity": _qty("30")}]),
                )
            results["split"] = "ok"
        except AppError as exc:
            results["split"] = exc.code
        except Exception as exc:
            results["split"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, split_worker]))

    assert results == {"stocktake": "ok", "split": "ok"}, results
    g1, l1 = _totals("RM-1")
    assert g1 == l1 == Decimal("70.0000")
    g2, l2 = _totals("RM-2")
    assert g2 == l2 == Decimal("70.0000")
    assert _loc_rows("RM-2-S") == {DEFAULT_LOCATION_CODE: Decimal("30.0000")}


def test_concurrent_stocktake_and_transfer_book_changes(engine):
    """盘点按账面 100 核对，与 DEFAULT->A 调拨 10 并发：
    - 盘点先提交：随后调拨成功（DEFAULT 90->80，A=10），全局 90；
    - 调拨先提交：盘点以账面 100 核对失败（账面已变动），库存只被调拨。
    两种结局都不超扣、两层恒等，不出现负余额。"""
    _register(qty="100")
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    _stocktake("ST-1", [
                        ("RM-1", DEFAULT_LOCATION_CODE, "100", "90")]),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    def transfer_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.transfer_stock(
                    s,
                    TransferIn(transfer_no="T1", batch_no="RM-1",
                               source_location=DEFAULT_LOCATION_CODE,
                               target_location="A", quantity=_qty("10")),
                )
            results["transfer"] = "ok"
        except AppError as exc:
            results["transfer"] = exc.code
        except Exception as exc:
            results["transfer"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [stocktake_worker, transfer_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["transfer"] == "ok", results
    rows = _loc_rows()
    with session_scope() as s:
        global_qty = Decimal(s.get(Batch, "RM-1").remaining_qty)
        st_count = s.execute(text("SELECT COUNT(*) FROM stocktakes")).scalar_one()
    if results["stocktake"] == "ok":
        assert st_count == 1
        # 盘点先：DEFAULT 90 -> 调拨 80；A 10；全局 90
        assert rows == {DEFAULT_LOCATION_CODE: Decimal("80.0000"),
                        "A": Decimal("10.0000")}, rows
        assert global_qty == Decimal("90.0000")
    else:
        assert results["stocktake"] == "STOCKTAKE_BOOK_MISMATCH", results
        assert st_count == 0
        # 调拨先：DEFAULT 90，A 10，全局 100
        assert rows == {DEFAULT_LOCATION_CODE: Decimal("90.0000"),
                        "A": Decimal("10.0000")}, rows
        assert global_qty == Decimal("100.0000")
    assert sum(rows.values()) == global_qty
def test_concurrent_stocktake_and_cancel_interleave(engine):
    """撤销生产单与盘点产出批次并发：
    - 撤销先：产出仍 40（撤销不扣产出），盘点按账面 40 核销成功（已撤销产出
      可核销）；再撤销已被 PRODUCTION_ALREADY_CANCELLED 拒绝；
    - 盘点先且发生盘亏：撤销被 OUTPUT_HAS_STOCKTAKE_LOSS 拒绝，来源不返还。
    任一结局都不超扣、两层恒等，不会“既撤销又记录盘亏后仍撤销成功”。"""
    _register(qty="100")
    with session_scope() as s:
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=_qty("40"),
                inputs=[{"batch_no": "RM-1", "quantity": _qty("40")}],
            ),
        )
    # 起始：RM-1=60(DEFAULT)，MIX-1=40(DEFAULT)

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def cancel_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.cancel_production(
                    s, "MIX-1", CancelIn(cancel_reason="录入错误")
                )
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    def stocktake_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_stocktake(
                    s,
                    _stocktake("ST-1", [
                        ("MIX-1", DEFAULT_LOCATION_CODE, "40", "30")]),
                )
            results["stocktake"] = "ok"
        except AppError as exc:
            results["stocktake"] = exc.code
        except Exception as exc:
            results["stocktake"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [cancel_worker, stocktake_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["stocktake"] == "ok", results
    with session_scope() as s:
        cancelled = service.get_production_record(s, "MIX-1").is_cancelled
        rm1 = Decimal(s.get(Batch, "RM-1").remaining_qty)
        mix = Decimal(s.get(Batch, "MIX-1").remaining_qty)

    if results["cancel"] == "ok":
        # 撤销先：RM-1 返还 40 -> 100；盘点随后把 MIX-1 40->30（已撤销可核销）
        assert cancelled is True
        assert rm1 == Decimal("100.0000"), rm1
        assert mix == Decimal("30.0000"), mix
    else:
        # 盘点先：撤销被盘亏阻止；RM-1 维持 60，MIX-1 核销到 30
        assert results["cancel"] == "OUTPUT_HAS_STOCKTAKE_LOSS", results
        assert cancelled is False
        assert rm1 == Decimal("60.0000"), rm1
        assert mix == Decimal("30.0000"), mix

    g_mix, l_mix = _totals("MIX-1")
    assert g_mix == l_mix == mix
    g_rm1, l_rm1 = _totals("RM-1")
    assert g_rm1 == l_rm1 == rm1
