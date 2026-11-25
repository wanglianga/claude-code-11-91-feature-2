"""Feature-2 场景：工单/拓扑/备件/持证技师、合并 vs 分开停电、三类回执与容量重算。

每个用例由 conftest.client 重建表并灌种子。种子固定 id：
站点 1；车 V2=2(60kW) V4=4(90+需求)；枪 A=1 B=2 C=3 D=4；
技师 1=全证(班至18:30) 2=仅高压证；备件批次 1=合格1件 2=冻结2件；
工单 1=WO-B-INS，2=WO-BC-BRKR；种子预约 1=R-SEEDA01(A 18:00-18:30)。
"""

from tests.conftest import dt

SITE = 1
V2, V4 = 2, 4
GUN_A, GUN_B, GUN_C = 1, 2, 3
TECH_CERT, TECH_APP = 1, 2
BATCH_OK, BATCH_QUAR = 1, 2
WO_B, WO_BR = 1, 2


def _book(client, vehicle, gun, power, minutes, key, start="18:00"):
    return client.post("/api/reservations", json={
        "vehicle_id": vehicle, "site_id": SITE, "start_at": dt(start),
        "duration_minutes": minutes, "min_power_kw": power,
        "mode": "CONNECTOR", "preferred_connector_id": gun,
        "idempotency_key": key,
    })


def _compare(client):
    r = client.post("/api/work-orders/compare")
    assert r.status_code == 200, r.text
    data = r.json()
    return {o["option_key"]: o for o in data["options"]}, data


def _decision_map(option):
    return {i["ref"]: i["decision"] for i in option["impacts"]}


def _board_cell(board, slice_label, power):
    for row in board["matrix"]:
        if row["slice"] == slice_label and row["min_power_kw"] == power:
            return row
    raise AssertionError(f"board cell {slice_label}/{power} not found")


# ---------- 拓扑与种子工单档案 ----------

def test_topology_shared_breaker_isolates_b_and_c(client):
    data = client.get("/api/topology").json()
    by_code = {n["code"]: n for n in data["nodes"]}
    assert set(by_code["BR-BC"]["affected_connector_codes"]) == {"B", "C"}
    assert by_code["ISO-B"]["affected_connector_codes"] == ["B"]
    # 告警证据：断路器温度超阈值
    assert by_code["BR-BC"]["metric_value"] == 78.0
    assert by_code["BR-BC"]["alarm_threshold"] == 70.0


def test_workorder_records_evidence_risk_deadline_isolation_steps(client):
    wos = client.get("/api/work-orders").json()
    by_ref = {w["ref"]: w for w in wos}
    wb = by_ref["WO-B-INS"]
    assert wb["kind"] == "PREVENTIVE"
    assert wb["status"] == "PENDING"
    assert wb["risk_level"] == "HIGH"
    assert wb["immediate_shutdown_required"] is False
    assert wb["evidence"] and wb["evidence"][0]["value"] == 6.0
    assert wb["deadline_at"].startswith("2026-11-27T18:30")
    assert wb["isolation_scope"]["connector_codes"] == ["B"]
    assert len(wb["steps"]) >= 5
    assert wb["acceptance"] == {} and wb["signoff_by"] is None

    wbr = by_ref["WO-BC-BRKR"]
    assert wbr["kind"] == "ALARM"
    assert set(wbr["isolation_scope"]["connector_codes"]) == {"B", "C"}


def test_pending_workorder_does_not_block_booking(client):
    # 未排程、未达强制停机阈值：B 仍可售卖并被锁定
    r = _book(client, V4, GUN_B, 90, 10, "f2-pending-book-1")
    assert r.status_code == 200, r.text
    assert r.json()["connector_id"] == GUN_B


# ---------- 合并 vs 分开：影响对比 ----------

def test_compare_merged_takes_b_and_c_separate_preserves_c(client):
    rb = _book(client, V4, GUN_B, 90, 30, "f2-cmp-b-1")
    rc = _book(client, V2, GUN_C, 60, 30, "f2-cmp-c-1")
    ref_b, ref_c = rb.json()["ref"], rc.json()["ref"]

    opts, data = _compare(client)
    assert set(opts) == {"MERGED_1740", "SEPARATE_B_ONLY", "SEPARATE_BOTH_TONIGHT"}

    merged = opts["MERGED_1740"]
    assert merged["feasible"] is True
    dm = _decision_map(merged)
    assert dm[ref_b] == "NEEDS_RESELECTION"
    assert dm[ref_c] == "NEEDS_RESELECTION"      # 合并：C 也退出，60kW 单受冲击
    # 合并后高峰 90/60 都没有可售枪（A 被种子单占用，B/C 停电，D 接口不符）
    peak = {p["min_power_kw"]: p["sellable_connector_codes"]
            for p in merged["post_plan_board"]["peak_1800_1830"]}
    assert peak[90] == [] and peak[60] == []

    sep = opts["SEPARATE_B_ONLY"]
    assert sep["feasible"] is True
    ds = _decision_map(sep)
    assert ds[ref_b] == "NEEDS_RESELECTION"
    assert ds[ref_c] == "UNCHANGED"             # 分开先做 B：C 保留，60kW 单继续履约
    cell = _board_cell(sep["post_plan_board"], "18:10–18:20", 90)
    assert cell["sellable_connector_codes"] == ["B"]  # B 18:10 验收恢复后可接高峰后段

    both = opts["SEPARATE_BOTH_TONIGHT"]
    assert both["feasible"] is False
    codes = {v["code"] for v in both["violations"]}
    assert "TECH_SHIFT" in codes and "DEADLINE" in codes


# ---------- 不安全排程必须拒绝且不留任何排程/库存副作用 ----------

def test_unsafe_schedules_are_rejected_without_side_effects(client):
    cases = [
        {"strategy": "SEPARATE", "scope": "BOTH_TONIGHT", "technician_id": TECH_CERT},
        {"strategy": "SEPARATE", "scope": "B_ONLY", "technician_id": TECH_APP},
        {"strategy": "SEPARATE", "scope": "B_ONLY", "technician_id": TECH_CERT,
         "spare_batch_id": BATCH_QUAR},
        {"strategy": "SEPARATE", "scope": "B_ONLY", "technician_id": TECH_CERT,
         "cooldown_minutes": 2},
    ]
    for idx, body in enumerate(cases):
        body["idempotency_key"] = f"f2-reject-{idx}-0001"
        r = client.post("/api/work-orders/schedule", json=body)
        assert r.status_code == 422, (idx, r.status_code, r.text)
        detail = r.json()["detail"]
        assert detail["violations"], idx

    assert client.get("/api/work-orders/1").json()["status"] == "PENDING"
    parts = client.get("/api/spare-parts").json()
    released = [b for p in parts for b in p["batches"] if b["id"] == BATCH_OK][0]
    assert released["quantity_available"] == 1
    assert client.get("/api/maintenance").json() == []


# ----------  canonical 三回执：延期 → 验收失败 → 取消 ----------

def test_delay_then_testfail_then_cancel_recomputes_capacity_and_notifies(client):
    bid = _book(client, V4, GUN_B, 90, 30, "f2-flow-book-1").json()["id"]

    sch = client.post("/api/work-orders/schedule", json={
        "strategy": "SEPARATE", "scope": "B_ONLY",
        "technician_id": TECH_CERT, "idempotency_key": "f2-flow-sched-1",
    })
    assert sch.status_code == 200, sch.text
    # 候选建议永远不是已确认
    for alt in sch.json()["alternatives"]:
        assert all(s["confirmed"] is False for s in alt["suggestions"])

    # 延期 25 分钟 → 18:35，击穿技师班次与最迟时点
    dl = client.post("/api/work-orders/1/delay", json={
        "delay_minutes": 25, "reason": "接头复测排队",
        "idempotency_key": "f2-flow-delay-1",
    })
    assert dl.status_code == 200
    breaches = dl.json()["breaches"]
    assert len(breaches) == 2

    # 测试失败回执：绝缘 8 < 50，负载 40 < 90
    fail = client.post("/api/receipts", json={
        "type": "ACCEPTANCE_FAILED_RECEIPT", "work_order_id": WO_B,
        "reason": "绝缘仍低", "insulation_mohm": 8,
        "load_test_kw": 40, "load_test_passed": False,
        "load_tested_at": dt("18:20"), "final_reading_at": dt("18:26"),
        "idempotency_key": "f2-flow-fail-1",
    })
    assert fail.status_code == 200
    assert fail.json()["passed"] is False
    wb = client.get("/api/work-orders/1").json()
    assert wb["status"] == "ACCEPTANCE_FAILED"

    board = fail.json()["board"]
    b_status = [c for c in board["connectors"] if c["code"] == "B"][0]
    assert b_status["operational_status"] == "ACCEPTANCE_FAILED"
    cell90 = _board_cell(board, "18:20–18:30", 90)
    assert "B" not in cell90["sellable_connector_codes"]
    assert cell90["sellable_connector_codes"] == []

    # 未通过验收不能签字恢复
    bad_sign = client.post("/api/work-orders/1/signoff", json={
        "signoff_by": "王建国", "idempotency_key": "f2-flow-badsign-1"})
    assert bad_sign.status_code == 409

    # 预约取消回执
    cancel = client.post("/api/receipts", json={
        "type": "RESERVATION_CANCELLED", "reservation_id": bid,
        "idempotency_key": "f2-flow-cancel-1",
    })
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "CANCELLED"

    kinds = {n["kind"] for n in client.get("/api/notifications").json()}
    assert {
        "SCHEDULE_CONFIRMED", "SCHEDULE_BREACH", "WORK_DELAYED",
        "ACCEPTANCE_FAILED", "REWORK_CREATED", "CANCEL_RECEIPT",
    }.issubset(kinds)


# ---------- 验收通过 + 冷却合格 + 签字 → 恢复容量 ----------

def test_passing_acceptance_and_signoff_restores_b(client):
    client.post("/api/work-orders/schedule", json={
        "strategy": "SEPARATE", "scope": "B_ONLY",
        "technician_id": TECH_CERT, "idempotency_key": "f2-ok-sched-1",
    })
    acc = client.post("/api/work-orders/1/acceptance", json={
        "insulation_mohm": 80, "load_test_kw": 120, "load_test_passed": True,
        "load_tested_at": dt("18:03"), "final_reading_at": dt("18:09"),
        "idempotency_key": "f2-ok-acc-1",
    })
    assert acc.status_code == 200 and acc.json()["passed"] is True

    so = client.post("/api/work-orders/1/signoff", json={
        "signoff_by": "王建国", "signed_at": dt("18:10"),
        "idempotency_key": "f2-ok-sign-1",
    })
    assert so.status_code == 200, so.text
    body = so.json()
    assert body["work_order"]["status"] == "COMPLETED"
    assert body["work_order"]["signoff_by"] == "王建国"
    b = [c for c in body["board"]["connectors"] if c["code"] == "B"][0]
    assert b["operational_status"] == "OPERATIONAL"
    cell = _board_cell(body["board"], "18:10–18:20", 90)
    assert cell["sellable_connector_codes"] == ["B"]


def test_short_cooldown_acceptance_is_invalid(client):
    client.post("/api/work-orders/schedule", json={
        "strategy": "SEPARATE", "scope": "B_ONLY",
        "technician_id": TECH_CERT, "idempotency_key": "f2-cd-sched-1",
    })
    acc = client.post("/api/work-orders/1/acceptance", json={
        "insulation_mohm": 80, "load_test_kw": 120, "load_test_passed": True,
        "load_tested_at": dt("18:00"), "final_reading_at": dt("18:02"),
        "idempotency_key": "f2-cd-acc-1",
    })
    assert acc.json()["passed"] is False
    assert any("冷却" in r for r in acc.json()["failure_reasons"])
    assert client.get("/api/work-orders/1").json()["status"] == "ACCEPTANCE_FAILED"


# ---------- 强制停机阈值 ----------

def test_reading_below_trip_threshold_forces_out_and_blocks_sale(client):
    r = client.post("/api/work-orders/triage-reading", json={
        "connector_id": GUN_B, "value_mohm": 2.0,
        "observed_at": dt("17:20"), "idempotency_key": "f2-trip-1",
    })
    assert r.status_code == 200
    assert r.json()["immediate_shutdown_required"] is True
    assert r.json()["operational_status"] == "FORCED_OUT"

    deny = _book(client, V4, GUN_B, 90, 10, "f2-trip-book-1", start="18:00")
    assert deny.status_code == 409


def test_critical_but_above_trip_threshold_remains_sellable(client):
    r = client.post("/api/work-orders/triage-reading", json={
        "connector_id": GUN_B, "value_mohm": 6.0,
        "idempotency_key": "f2-warn-1",
    })
    assert r.json()["immediate_shutdown_required"] is False
    ok = _book(client, V4, GUN_B, 90, 10, "f2-warn-book-1")
    assert ok.status_code == 200


# ---------- 候选替代不是已确认；取消 A 单后 A 成为候选，须显式动作才确认 ----------

def test_candidate_alternative_requires_explicit_confirmation(client):
    bid = _book(client, V4, GUN_B, 90, 30, "f2-alt-book-1").json()["id"]
    client.post("/api/work-orders/schedule", json={
        "strategy": "SEPARATE", "scope": "B_ONLY",
        "technician_id": TECH_CERT, "idempotency_key": "f2-alt-sched-1",
    })
    r0 = client.get(f"/api/reservations/{bid}/alternatives").json()
    assert all(s["confirmed"] is False for s in r0["suggestions"])
    assert client.get(f"/api/reservations/{bid}").json()["status"] == "NEEDS_RESELECTION"

    # 取消占用 A 的种子单，A 容量释放
    cancel_a = client.post("/api/reservations/1/cancel",
                           json={"idempotency_key": "f2-alt-cancela-1"})
    assert cancel_a.status_code == 200

    alt = client.get(f"/api/reservations/{bid}/alternatives").json()
    now_a = [s for s in alt["suggestions"] if s["kind"] == "OTHER_CONNECTOR_NOW"]
    assert now_a and "A" in now_a[0]["connector_codes"]
    # 候选不会自动确认：订单仍是改选状态、枪仍是 B
    cur = client.get(f"/api/reservations/{bid}").json()
    assert cur["status"] == "NEEDS_RESELECTION" and cur["connector_id"] == GUN_B

    # 显式客服替代动作后才确认到 A
    act = client.post("/api/operations/service-actions", json={
        "reservation_id": bid, "kind": "ALTERNATIVE_CONNECTOR",
        "target_connector_id": GUN_A, "note": "客户同意改 A",
        "idempotency_key": "f2-alt-action-1",
    })
    assert act.status_code == 200, act.text
    after = client.get(f"/api/reservations/{bid}").json()
    assert after["status"] == "CONFIRMED" and after["connector_id"] == GUN_A


# ---------- 合并排程：C 退出、备件扣减、幂等重放不重复扣库存 ----------

def test_merged_schedule_takes_c_and_idempotent_replay(client):
    body = {
        "strategy": "MERGED", "technician_id": TECH_CERT,
        "idempotency_key": "f2-merged-sched-1",
    }
    r1 = client.post("/api/work-orders/schedule", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["scope"] == "MERGED"
    assert r1.json()["plan_id"]

    c = [x for x in r1.json()["board"]["connectors"] if x["code"] == "C"][0]
    assert c["operational_status"] == "SCHEDULED"

    # 同键重放：返回同一结果，合格备件仍为 0（不二次扣减）
    r2 = client.post("/api/work-orders/schedule", json=body)
    assert r2.status_code == 200
    assert r2.json()["plan_id"] == r1.json()["plan_id"]
    parts = client.get("/api/spare-parts").json()
    released = [b for p in parts for b in p["batches"] if b["id"] == BATCH_OK][0]
    assert released["quantity_available"] == 0
