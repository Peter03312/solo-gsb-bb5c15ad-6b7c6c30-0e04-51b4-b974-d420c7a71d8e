"""生产单撤销 API 端到端测试。

覆盖：返还来源投料（来源已隔离也返还）、撤销前置条件、重复撤销、
已撤销产出禁投、撤销边退出谱系与污染传播、幂等重放/冲突、原单据留痕查询。
"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": qty, "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    from decimal import Decimal

    total = sum((Decimal(str(q)) for _, q in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [{"batch_no": b, "quantity": str(q)} for b, q in inputs],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _revoke(client, output_no, reason="录单错误", key=None):
    return client.post(
        f"/productions/{output_no}/revoke",
        json={"output_batch_no": output_no, "revoke_reason": reason},
        headers={"Idempotency-Key": key or f"rev-{output_no}"},
    )


def _flag(client, batch_no, key, note=None):
    return client.post(
        "/contamination",
        json={"batch_no": batch_no, "note": note},
        headers={"Idempotency-Key": key},
    )


def _review(client, batch_no, note, key):
    return client.post(
        "/reviews",
        json={"batch_no": batch_no, "review_note": note},
        headers={"Idempotency-Key": key},
    )


# ------------------------------------------------------------- 正常撤销与返还


def test_revoke_returns_original_input_quantities(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "60")
    r = _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "55")], out_qty=90, key="p1")
    assert r.status_code == 201

    r = _revoke(client, "MIX-1", "投料量录入错误，撤销重开", "v1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output_batch_no"] == "MIX-1"
    assert body["status"] == "REVOKED"
    assert body["revoke_reason"] == "投料量录入错误，撤销重开"
    assert body["output_quantity"] == 90.0
    assert body["unit"] == "kg"
    assert body["revoked_at"]
    # 返还明细：按原单据投料行顺序，逐笔原量返还
    assert body["returns"] == [
        {"batch_no": "RM-A", "quantity": 40.0},
        {"batch_no": "RM-B", "quantity": 55.0},
    ]

    # 各来源余量在同一事务内回到撤销前 + 原投料量
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 60.0
    out = client.get("/batches/MIX-1").json()
    assert out["remaining_qty"] == 0.0
    assert out["is_revoked"] is True
    # 原料批次不是产出，标记为 False（既有字段行为保持）
    assert client.get("/batches/RM-A").json()["is_revoked"] is False


def test_revoke_restores_even_when_source_is_quarantined(client):
    # 来源批次已隔离（RM-A 疑似 -> MIX-1 作为后代被连带隔离，但其自身并不疑似）：
    # 撤销仍须在同一事务内返还 RM-A 的原投料量。
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "60")], key="p1")
    assert _flag(client, "RM-A", "c1").status_code == 200
    assert client.get("/batches/MIX-1").json()["is_quarantined"] is True
    assert client.get("/batches/MIX-1").json()["is_suspect"] is False

    r = _revoke(client, "MIX-1", "来源隔离也要返还", "v1")
    assert r.status_code == 200, r.text
    assert r.json()["returns"] == [
        {"batch_no": "RM-A", "quantity": 40.0},
        {"batch_no": "RM-B", "quantity": 60.0},
    ]
    # 隔离不影响返还：余量照样退回（隔离批次的余量只是不可再投料）
    rm_a = client.get("/batches/RM-A").json()
    assert rm_a["remaining_qty"] == 100.0
    assert rm_a["is_quarantined"] is True
    assert client.get("/batches/RM-B").json()["remaining_qty"] == 100.0
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 0.0


def test_revoked_output_number_cannot_be_reused(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _revoke(client, "MIX-1", key="v1").status_code == 200
    # 编号保留、不得复用
    r = _produce(client, "MIX-1", [("RM-A", "10")], key="p2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NO_REUSED"
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


# ------------------------------------------------------------- 前置条件拒绝


def test_revoke_nonexistent_number_rejected_without_change(client):
    _raw(client, "RM-A", "100")
    r = _revoke(client, "GHOST", key="v1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_revoke_non_output_batch_rejected(client):
    _raw(client, "RM-A", "100")
    r = _revoke(client, "RM-A", key="v1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_revoke_suspect_output_rejected_without_change(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    # 直接把产出标记为疑似
    assert _flag(client, "MIX-1", "c1").status_code == 200

    r = _revoke(client, "MIX-1", key="v1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_IS_SUSPECT"
    # 不改库存、未撤销
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
    d = client.get("/batches/MIX-1").json()
    assert d["remaining_qty"] == 40.0 and d["is_revoked"] is False


def test_revoke_output_with_review_record_rejected(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    # 产出曾被标记疑似、又经复核撤销 -> 存在复核记录，即使当前不疑似也不得撤销
    _flag(client, "MIX-1", "c1")
    assert _review(client, "MIX-1", "留样合格", "r1").status_code == 200

    r = _revoke(client, "MIX-1", key="v1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_HAS_REVIEW"
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
    assert client.get("/batches/MIX-1").json()["is_revoked"] is False


def test_revoke_consumed_output_rejected(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="p2")

    r = _revoke(client, "MIX-1", key="v1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_ALREADY_CONSUMED"
    # 余量都不变
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 30.0
    assert client.get("/batches/FG-1").json()["remaining_qty"] == 10.0


def test_revoked_consuming_edge_allows_parent_revoke(client):
    # MIX-1 曾被 FG-1 投料，但 FG-1 自身被撤销后该投料边失效，
    # MIX-1 重新满足“从未被后续（有效）生产投料”，可撤销。
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="p2")
    assert _revoke(client, "FG-1", key="v2").status_code == 200
    # FG-1 撤销时已把 10 返还给 MIX-1
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 40.0

    assert _revoke(client, "MIX-1", key="v1").status_code == 200
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 0.0


def test_duplicate_revoke_rejected_without_double_refund(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _revoke(client, "MIX-1", key="v1").status_code == 200

    r = _revoke(client, "MIX-1", key="v-other")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRODUCTION_ALREADY_REVOKED"
    # 没有二次返还：余量维持 100（40 只返还一次）
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 0.0


# ------------------------------------------------------------- 撤销后行为


def test_revoked_output_cannot_be_consumed(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _revoke(client, "MIX-1", key="v1").status_code == 200
    # 即便撤销把来源余量退回、看上去“够料”，已撤销产出也不得投料
    r = _produce(client, "FG-1", [("MIX-1", "5")], key="p2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_REVOKED"
    assert client.get("/batches/FG-1").status_code == 404
    assert client.get("/batches/MIX-1").json()["remaining_qty"] == 0.0


def test_revoked_edge_excluded_from_lineage(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")  # 保留
    _produce(client, "MIX-2", [("RM-A", "20")], key="p2")  # 将撤销
    _produce(client, "FG-1", [("MIX-1", "10")], key="p3")
    assert _revoke(client, "MIX-2", key="v2").status_code == 200

    # 来源视角：MIX-2 与 RM-A->MIX-2 边不再进入有效谱系
    lin = client.get("/batches/RM-A/lineage").json()
    assert {n["batch_no"] for n in lin["downstream"]} == {"MIX-1", "FG-1"}
    edges = {(e["from"], e["to"]) for e in lin["edges"]}
    assert ("RM-A", "MIX-1") in edges
    assert ("MIX-1", "FG-1") in edges
    assert ("RM-A", "MIX-2") not in edges

    # 已撤销产出自身的有效谱系为空，但批次仍可按编号查询
    lin2 = client.get("/batches/MIX-2/lineage").json()
    assert lin2["upstream"] == [] and lin2["downstream"] == [] and lin2["edges"] == []


def test_revoked_edge_excluded_from_contamination_propagation(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-KEEP", [("RM-A", "40")], key="p1")
    _produce(client, "MIX-GONE", [("RM-A", "20")], key="p2")
    assert _revoke(client, "MIX-GONE", key="v1").status_code == 200

    r = _flag(client, "RM-A", "c1")
    assert r.status_code == 200
    # 撤销边不传播：MIX-GONE 不在受影响后代中，也不被隔离
    assert r.json()["affected_batch_nos"] == ["MIX-KEEP"]
    assert client.get("/batches/MIX-GONE").json()["is_quarantined"] is False
    assert client.get("/batches/MIX-GONE").json()["is_suspect"] is False
    assert client.get("/batches/MIX-KEEP").json()["is_quarantined"] is True


def test_contamination_before_revoke_does_not_revive_revoked_edge(client):
    # 先污染传播隔离过，之后撤销：撤销边同样退出谱系，
    # 撤销不改变来源隔离状态（不误放行仍疑似的来源）。
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _flag(client, "RM-A", "c1")
    assert client.get("/batches/MIX-1").json()["is_quarantined"] is True
    # MIX-1 当前非疑似（仅连带隔离），允许撤销
    assert _revoke(client, "MIX-1", key="v1").status_code == 200
    rm_a = client.get("/batches/RM-A").json()
    assert rm_a["is_suspect"] is True and rm_a["is_quarantined"] is True
    assert rm_a["remaining_qty"] == 100.0  # 返还不受隔离影响


# ------------------------------------------------------------- 留痕查询


def test_get_production_detail_active_and_revoked(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "60")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "55")], out_qty=90, key="p1")

    r = client.get("/productions/MIX-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACTIVE"
    assert body["revoke_reason"] is None and body["revoked_at"] is None
    assert body["output_batch_no"] == "MIX-1"
    assert body["unit"] == "kg" and body["output_quantity"] == 90.0
    assert body["inputs"] == [
        {"batch_no": "RM-A", "quantity": 40.0},
        {"batch_no": "RM-B", "quantity": 55.0},
    ]

    r = _revoke(client, "MIX-1", "录错了", "v1")
    revoked_at = r.json()["revoked_at"]
    r = client.get("/productions/MIX-1")
    assert r.status_code == 200
    body = r.json()
    # 原单据内容完整保留
    assert body["output_quantity"] == 90.0
    assert body["inputs"] == [
        {"batch_no": "RM-A", "quantity": 40.0},
        {"batch_no": "RM-B", "quantity": 55.0},
    ]
    assert body["status"] == "REVOKED"
    assert body["revoke_reason"] == "录错了"
    assert body["revoked_at"] == revoked_at


def test_get_production_detail_not_found(client):
    assert client.get("/productions/GHOST").status_code == 404
    _raw(client, "RM-A", "100")
    # 原料批次没有生产单
    assert client.get("/productions/RM-A").status_code == 404


# ------------------------------------------------------------- 幂等与入参


def test_revoke_idempotent_replay_returns_first_result(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    payload = {"output_batch_no": "MIX-1", "revoke_reason": "录单错误"}
    r1 = client.post(
        "/productions/MIX-1/revoke",
        json=payload,
        headers={"Idempotency-Key": "V1"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/productions/MIX-1/revoke",
        json=payload,
        headers={"Idempotency-Key": "V1"},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # 重放不重复返还
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_revoke_same_key_different_content_conflicts(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _produce(client, "MIX-2", [("RM-A", "10")], key="p2")
    # 同路径 + 同键 + 不同请求体（撤销原因不同）-> 异体冲突
    p1 = {"output_batch_no": "MIX-1", "revoke_reason": "原因一"}
    p2 = {"output_batch_no": "MIX-1", "revoke_reason": "原因二"}
    assert (
        client.post(
            "/productions/MIX-1/revoke",
            json=p1,
            headers={"Idempotency-Key": "K"},
        ).status_code
        == 200
    )
    r = client.post(
        "/productions/MIX-1/revoke", json=p2, headers={"Idempotency-Key": "K"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 冲突无副作用：首次撤销原因保持“原因一”，MIX-1 只返还 40
    # （RM-A 经历 100 -> 60 -> 50，撤销 MIX-1 后回到 90）
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 90.0
    assert (
        client.get("/productions/MIX-1").json()["revoke_reason"] == "原因一"
    )
    # 幂等键按 (method, path, key) 区分：不同路径的同键各自独立、均可成功
    r = client.post(
        "/productions/MIX-2/revoke",
        json={"output_batch_no": "MIX-2", "revoke_reason": "另一张单"},
        headers={"Idempotency-Key": "K"},
    )
    assert r.status_code == 200, r.text
    assert client.get("/batches/MIX-2").json()["is_revoked"] is True
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 100.0


def test_revoke_requires_idempotency_key(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    r = client.post(
        "/productions/MIX-1/revoke",
        json={"output_batch_no": "MIX-1", "revoke_reason": "无键"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert client.get("/batches/MIX-1").json()["is_revoked"] is False


def test_revoke_blank_reason_rejected(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    r = client.post(
        "/productions/MIX-1/revoke",
        json={"output_batch_no": "MIX-1", "revoke_reason": "   "},
        headers={"Idempotency-Key": "V1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/batches/MIX-1").json()["is_revoked"] is False
    assert client.get("/batches/RM-A").json()["remaining_qty"] == 60.0


def test_revoke_path_body_number_mismatch_rejected(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    r = client.post(
        "/productions/MIX-1/revoke",
        json={"output_batch_no": "MIX-2", "revoke_reason": "编号不一致"},
        headers={"Idempotency-Key": "V1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/batches/MIX-1").json()["is_revoked"] is False
