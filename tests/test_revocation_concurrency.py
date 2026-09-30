"""撤销并发测试：撤销 × 投料 / 污染标记 / 复核 并发时等价于某一串行顺序。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch
from app.schemas import ProductionIn, RawBatchIn, ReviewIn, RevocationIn, SuspectIn


def _produce_payload(out_no: str, source_no: str, qty: str) -> ProductionIn:
    return ProductionIn(
        output_batch_no=out_no,
        unit="kg",
        output_quantity=Decimal(qty),
        inputs=[{"batch_no": source_no, "quantity": Decimal(qty)}],  # type: ignore[list-item]
    )


def _seed_simple_chain() -> None:
    """RM-1(100) --100--> MIX-1(100)。"""
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.submit_production(s, _produce_payload("MIX-1", "RM-1", "100"))


def test_revoke_concurrent_with_consuming_output_equivalent_to_serial(engine):
    """撤销 MIX-1 与“用 MIX-1 投料生产 FG-1”并发：

    - 投料先：MIX-1 已被后续投料 -> 撤销拒绝 OUTPUT_ALREADY_CONSUMED，
      MIX-1 余量 90、FG-1 存在；
    - 撤销先：投料面对已撤销产出 -> BATCH_REVOKED，
      MIX-1 余量 0、FG-1 不存在、RM-1 余量回到 100。
    """
    _seed_simple_chain()
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def revoke_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.revoke_production(
                    s, RevocationIn(output_batch_no="MIX-1", revoke_reason="录错")
                )
            results["revoke"] = "ok"
        except AppError as exc:
            results["revoke"] = exc.code
        except Exception as exc:
            results["revoke"] = f"ERROR:{type(exc).__name__}:{exc}"

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce_payload("FG-1", "MIX-1", "10"))
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [revoke_worker, produce_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        fg1 = s.get(Batch, "FG-1")

        if results["revoke"] == "ok":
            # 撤销先于投料串行
            assert results["produce"] == "BATCH_REVOKED", results
            assert fg1 is None
            assert rm1.remaining_qty == Decimal("100.0000")
            assert mix1.remaining_qty == 0
        else:
            # 投料先于撤销串行
            assert results["revoke"] == "OUTPUT_ALREADY_CONSUMED", results
            assert results["produce"] == "ok", results
            assert fg1 is not None
            assert rm1.remaining_qty == Decimal("0.0000")
            assert mix1.remaining_qty == Decimal("90.0000")


def test_revoke_concurrent_with_consuming_source_never_overuses(engine):
    """撤销（向来源 RM-1 返还 100）与“直接用 RM-1 投料 FG-1”并发：
    两种串行结局下余量都必须自洽，绝不能超用或负余量。"""
    _seed_simple_chain()
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def revoke_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.revoke_production(
                    s, RevocationIn(output_batch_no="MIX-1", revoke_reason="录错")
                )
            results["revoke"] = "ok"
        except AppError as exc:
            results["revoke"] = exc.code
        except Exception as exc:
            results["revoke"] = f"ERROR:{type(exc).__name__}:{exc}"

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce_payload("FG-1", "RM-1", "60"))
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [revoke_worker, produce_worker]))

    assert results["revoke"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        fg1 = s.get(Batch, "FG-1")
        if results["produce"] == "ok":
            # 撤销先 -> RM-1 返还至 100，投料 60 成功：余量 40；MIX-1 清零
            assert fg1 is not None
            assert rm1.remaining_qty == Decimal("40.0000")
            assert mix1.remaining_qty == 0
        else:
            # 投料先 -> RM-1 余量为 0，请求 60 被拒；随后撤销返还至 100
            assert results["produce"] == "INSUFFICIENT_BALANCE", results
            assert fg1 is None
            assert rm1.remaining_qty == Decimal("100.0000")
            assert mix1.remaining_qty == 0


def test_revoke_concurrent_with_flag_on_output_equivalent_to_serial(engine):
    """撤销 MIX-1 与“直接标记 MIX-1 疑似”并发：

    - 标记先：产出当前疑似 -> 撤销拒绝 OUTPUT_IS_SUSPECT，MIX-1 仍疑似/隔离；
    - 撤销先：标记落在已撤销批次上，无有效后代可传播（标记本身仍成功）。
    两种结局均合法且状态自洽。
    """
    _seed_simple_chain()
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def revoke_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.revoke_production(
                    s, RevocationIn(output_batch_no="MIX-1", revoke_reason="录错")
                )
            results["revoke"] = "ok"
        except AppError as exc:
            results["revoke"] = exc.code
        except Exception as exc:
            results["revoke"] = f"ERROR:{type(exc).__name__}:{exc}"

    def flag_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.mark_suspect(s, SuspectIn(batch_no="MIX-1"))
            results["flag"] = "ok"
        except Exception as exc:
            results["flag"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [revoke_worker, flag_worker]))

    assert results["flag"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["revoke"] in ("ok", "OUTPUT_IS_SUSPECT"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        if results["revoke"] == "ok":
            # 撤销先：RM-1 拿到返还；标记后 MIX-1 自身疑似，但撤销边不向 RM-1
            # 上游传播（污染只向下传播），RM-1 不受影响
            assert rm1.remaining_qty == Decimal("100.0000")
            assert mix1.is_suspect is True
            assert rm1.is_quarantined is False
        else:
            # 标记先：撤销被拒，库存不变，MIX-1 疑似隔离
            assert results["revoke"] == "OUTPUT_IS_SUSPECT"
            assert rm1.remaining_qty == 0
            assert mix1.is_suspect is True and mix1.is_quarantined is True


def test_review_concurrent_with_revoke_equivalent_to_serial(engine):
    """对 MIX-1 的“复核撤销疑似”与“撤销生产单”并发。

    预置 MIX-1 被直接标记疑似（当前疑似、尚无复核记录）：
    - 撤销先串行：当时产出疑似 -> 撤销拒绝 OUTPUT_IS_SUSPECT，随后复核成功；
    - 复核先串行：撤销疑似并落下复核记录 -> 撤销以 OUTPUT_HAS_REVIEW 拒绝。
    两种结局都等价于某一串行顺序，库存只在“撤销成功”时才返还，
    而本预置下撤销必然失败，故 RM-1 始终不得被返还。
    """
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.submit_production(s, _produce_payload("MIX-1", "RM-1", "100"))
        service.mark_suspect(s, SuspectIn(batch_no="MIX-1"))

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def revoke_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.revoke_production(
                    s, RevocationIn(output_batch_no="MIX-1", revoke_reason="录错")
                )
            results["revoke"] = "ok"
        except AppError as exc:
            results["revoke"] = exc.code
        except Exception as exc:
            results["revoke"] = f"ERROR:{type(exc).__name__}:{exc}"

    def review_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.review_suspect(
                    s, ReviewIn(batch_no="MIX-1", review_note="误报")
                )
            results["review"] = "ok"
        except AppError as exc:
            results["review"] = exc.code
        except Exception as exc:
            results["review"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [revoke_worker, review_worker]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["review"] == "ok", results  # 复核串行位置一定成功
    assert results["revoke"] in ("OUTPUT_IS_SUSPECT", "OUTPUT_HAS_REVIEW"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        # 复核成功：MIX-1 不再疑似；撤销被拒：库存不变
        assert mix1.is_suspect is False
        assert rm1.remaining_qty == 0
        assert mix1.remaining_qty == Decimal("100.0000")
