"""Feature-2：预防/告警工单、设备拓扑、备件批次、持证技师排班与停电方案。

核心业务约束（对应题面）：
- 系统先依据绝缘读数阈值研判是否“立即停机”；未达强制阈值的临界设备，
  工单处于待排程时仍可预约，一旦触及强制停机阈值则立即生成停用窗口并硬拦截售卖。
- 主管可：
  * MERGED   —— 共用支路一次停电，完成挂牌/检修/复电测试，B、C 同时退出；
  * SEPARATE —— 先单独更换 B 接头并验收恢复（C 保留），断路器另择窗口，
                需重复校验资质/班次与停电验证。
- 任何排程都必须校验：技师资质与班次、备件批次与数量、拓扑隔离依赖、
  最短冷却时间、最迟完成时点；不安全的排程以 422 拒绝且不落任何数据。
- 延期 / 验收失败 / 预约取消三类回执到达后重算容量并通知；
  绝缘与负载测试未通过的设备绝不允许恢复。
- 替代建议永远只是“候选”（confirmed=False），不会改写订单或锁定枪口；
  未验收/强制停机设备由容量引擎状态硬闸门直接置 0，不可能显示为已确认履约。
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import (
    ACCEPT_INSULATION_MOHM,
    INSULATION_TRIP_MOHM,
    INSULATION_WARN_MOHM,
    MIN_LOAD_TEST_KW,
    MIN_COOLDOWN_MINUTES,
    ReservationStatus,
)
from ..models import (
    Connector,
    Equipment,
    MaintenanceWindow,
    Notification,
    Reservation,
    SchedulePlan,
    SparePart,
    SparePartBatch,
    Technician,
    Vehicle,
    WorkOrder,
)
from ..schemas import (
    AcceptanceRequest,
    DelayRequest,
    ReceiptCreate,
    ScheduleRequest,
    SignoffRequest,
    TriageReadingRequest,
)
from .capacity import build_segments, evaluate_site
from .maintenance import _assess_impacts
from .reservation import cancel as cancel_reservation

SCENARIO_DAY = datetime(2026, 11, 27)  # 周五


def _dt(hm: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-27T{hm}:00")


DAY_END = datetime(2026, 11, 27, 23, 59)

WO_B_REF = "WO-B-INS"
WO_BREAKER_REF = "WO-BC-BRKR"

# 容量看板里逐格核验的 10 分钟窗口
BOARD_SLICES = [
    ("18:00–18:10", "18:00", "18:10"),
    ("18:10–18:20", "18:10", "18:20"),
    ("18:20–18:30", "18:20", "18:30"),
    ("18:25–18:35", "18:25", "18:35"),
]
BOARD_POWERS = [90, 60]


def jloads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def jdumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


_IMPACT_RANK = {
    "UNCHANGED": 1,
    "NEEDS_RESELECTION": 2,
    "MANUAL_CONTACT": 3,
    "PROTECTED_CHARGING": 4,
}


def merge_impacts(*lists: list[dict]) -> list[dict]:
    """同一预约被多个停电窗口评估时，保留最严重的判定与对应原因。"""
    best: dict[int, dict] = {}
    for impacts in lists:
        for i in impacts:
            rid = i["reservation_id"]
            cur = best.get(rid)
            if cur is None or _IMPACT_RANK.get(i["decision"], 0) > _IMPACT_RANK.get(
                cur["decision"], 0
            ):
                best[rid] = i
    return list(best.values())


# ==================== 通知 ====================

def notify(db: Session, site_id: int, kind: str, title: str, body: str,
           reservation_id: int | None = None, audience: str = "DRIVER",
           channel: str = "SMS") -> Notification:
    n = Notification(
        site_id=site_id,
        reservation_id=reservation_id,
        audience=audience,
        channel=channel,
        kind=kind,
        title=title,
        body=body,
    )
    db.add(n)
    db.flush()
    return n


# ==================== 序列化 ====================

def wo_to_dict(wo: WorkOrder) -> dict:
    return {
        "id": wo.id,
        "ref": wo.ref,
        "site_id": wo.site_id,
        "kind": wo.kind,
        "status": wo.status,
        "risk_level": wo.risk_level,
        "title": wo.title,
        "equipment_id": wo.equipment_id,
        "connector_id": wo.connector_id,
        "required_cert": wo.required_cert,
        "immediate_shutdown_required": wo.immediate_shutdown_required,
        "triage_detail": jloads(wo.triage_detail, {}),
        "evidence": jloads(wo.evidence, []),
        "deadline_at": wo.deadline_at,
        "isolation_scope": jloads(wo.isolation_scope, {}),
        "steps": jloads(wo.steps, []),
        "min_cooldown_minutes": wo.min_cooldown_minutes,
        "strategy": wo.strategy,
        "technician_id": wo.technician_id,
        "spare_part_id": wo.spare_part_id,
        "spare_batch_id": wo.spare_batch_id,
        "spare_qty": wo.spare_qty,
        "planned_start_at": wo.planned_start_at,
        "planned_end_at": wo.planned_end_at,
        "actual_started_at": wo.actual_started_at,
        "acceptance": jloads(wo.acceptance, {}),
        "acceptance_passed": wo.acceptance_passed,
        "signoff_by": wo.signoff_by,
        "signoff_at": wo.signoff_at,
        "delay_count": wo.delay_count,
        "notes": wo.notes,
        "created_at": wo.created_at,
        "updated_at": wo.updated_at,
    }


def technician_to_dict(t: Technician) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "employee_no": t.employee_no,
        "certifications": jloads(t.certifications, []),
        "shift_start": t.shift_start,
        "shift_end": t.shift_end,
        "active": t.active,
    }


def part_to_dict(p: SparePart) -> dict:
    return {
        "id": p.id,
        "sku": p.sku,
        "name": p.name,
        "compatible_connector_type": p.compatible_connector_type,
        "batches": [
            {
                "id": b.id,
                "batch_no": b.batch_no,
                "quality_status": b.quality_status,
                "quantity_available": b.quantity_available,
            }
            for b in p.batches
        ],
    }


# ==================== 拓扑 ====================

def descendant_equipment_ids(db: Session, equipment_id: int) -> list[int]:
    """返回节点自身 + 全部后代设备 id。"""
    nodes = db.scalars(select(Equipment)).all()
    children: dict[int | None, list[Equipment]] = {}
    for n in nodes:
        children.setdefault(n.parent_id, []).append(n)
    out: list[int] = []
    stack = [equipment_id]
    by_id = {n.id: n for n in nodes}
    if equipment_id not in by_id:
        return []
    while stack:
        cur = stack.pop()
        out.append(cur)
        for ch in children.get(cur, []):
            stack.append(ch.id)
    return out


def isolation_connector_ids(db: Session, equipment_id: int) -> list[int]:
    ids = descendant_equipment_ids(db, equipment_id)
    if not ids:
        return []
    rows = db.scalars(
        select(Equipment).where(
            Equipment.id.in_(ids), Equipment.connector_id.is_not(None)
        )
    ).all()
    return sorted({e.connector_id for e in rows if e.connector_id is not None})


def topology_tree(db: Session, site_id: int) -> dict:
    nodes = db.scalars(
        select(Equipment).where(Equipment.site_id == site_id).order_by(Equipment.id)
    ).all()
    connectors = {c.id: c for c in db.scalars(
        select(Connector).where(Connector.site_id == site_id)).all()}

    flat = []
    for n in nodes:
        affected = isolation_connector_ids(db, n.id)
        flat.append({
            "id": n.id,
            "code": n.code,
            "name": n.name,
            "kind": n.kind,
            "parent_id": n.parent_id,
            "connector_id": n.connector_id,
            "metric_value": float(n.metric_value) if n.metric_value is not None else None,
            "metric_unit": n.metric_unit,
            "alarm_threshold": float(n.alarm_threshold) if n.alarm_threshold is not None else None,
            "affected_connector_ids": affected,
            "affected_connector_codes": [
                connectors[cid].code for cid in affected if cid in connectors
            ],
        })

    def depth_of(nid: int | None) -> int:
        d = 0
        cur = nid
        by_id = {n.id: n for n in nodes}
        while cur is not None and cur in by_id:
            d += 1
            cur = by_id[cur].parent_id
        return d

    for f in flat:
        f["depth"] = depth_of(f["parent_id"])

    return {"site_id": site_id, "nodes": flat}


# ==================== 容量看板 ====================

def _synth_vehicle(max_kw: float) -> SimpleNamespace:
    return SimpleNamespace(
        connector_type="CCS", accessible=False, max_power_kw=max_kw
    )


def board(db: Session, site_id: int = 1) -> dict:
    connectors = db.scalars(
        select(Connector).where(Connector.site_id == site_id).order_by(Connector.id)
    ).all()
    conn_out = [
        {
            "id": c.id,
            "code": c.code,
            "connector_type": c.connector_type,
            "rated_power_kw": float(c.rated_power_kw),
            "accessible": c.accessible,
            "operational_status": c.operational_status,
        }
        for c in connectors
    ]

    matrix: list[dict] = []
    for label, h0, h1 in BOARD_SLICES:
        s, e = _dt(h0), _dt(h1)
        for power in BOARD_POWERS:
            veh = _synth_vehicle(max(power, 60) + 60)  # 车辆受电上限高于请求功率
            ev = evaluate_site(db, site_id, veh, s, e, float(power))
            sellable = [x.connector.code for x in ev.connector_evals if x.eligible]
            matrix.append({
                "slice": label,
                "start_at": s,
                "end_at": e,
                "min_power_kw": power,
                "sellable_connector_codes": sellable,
                "pool_slots": ev.confirmable_pool_slots,
            })

    peak: list[dict] = []
    for power in BOARD_POWERS:
        veh = _synth_vehicle(max(power, 60) + 60)
        ev = evaluate_site(db, site_id, veh, _dt("18:00"), _dt("18:30"), float(power))
        peak.append({
            "min_power_kw": power,
            "sellable_connector_codes": [x.connector.code for x in ev.connector_evals if x.eligible],
            "pool_slots": ev.confirmable_pool_slots,
        })

    segs = build_segments(db, site_id, _dt("17:30"), _dt("19:00"))
    segments_out = [
        {
            "start_at": s.start_at,
            "end_at": s.end_at,
            "power": {str(k): v for k, v in s.power.items()},
            "locked_by": {str(k): v for k, v in s.locked_by.items()},
            "pool_demand": s.pool_demand,
        }
        for s in segs
    ]
    return {
        "site_id": site_id,
        "connectors": conn_out,
        "peak_1800_1830": peak,
        "matrix": matrix,
        "segments_1730_1900": segments_out,
    }


# ==================== 替代建议（永远是候选，不确认） ====================

def suggest_for_reservation(db: Session, r: Reservation) -> list[dict]:
    vehicle = db.get(Vehicle, r.vehicle_id)
    duration = r.end_at - r.start_at
    min_power = float(r.min_power_kw)
    suggestions: list[dict] = []

    def codes_of(ev) -> list[str]:
        return [x.connector.code for x in ev.connector_evals if x.eligible]

    # 1) 当前时段其他合格枪
    ev = evaluate_site(db, r.site_id, vehicle, r.start_at, r.end_at, min_power)
    now_codes = codes_of(ev)
    if now_codes:
        suggestions.append({
            "kind": "OTHER_CONNECTOR_NOW",
            "confirmed": False,
            "meets_original_requirement": True,
            "connector_codes": now_codes,
            "start_at": r.start_at,
            "end_at": r.end_at,
            "title": f"当前时段可改派：{'/'.join(now_codes)}",
            "detail": "仅为候选替代枪，尚未锁定或确认；需运营员执行替代枪口动作后才成立。",
        })
    if ev.confirmable_pool_slots >= 1:
        suggestions.append({
            "kind": "POOL",
            "confirmed": False,
            "meets_original_requirement": True,
            "pool_slots": ev.confirmable_pool_slots,
            "title": f"资源池尚有 {ev.confirmable_pool_slots} 个名额",
            "detail": "候选池名额，不会为该单自动确认。",
        })

    # 2) 顺延到设备恢复后的候选时刻
    candidate_starts = [_dt(h) for h in ("18:10", "18:20", "18:25", "18:30", "18:40")]
    open_windows = db.scalars(
        select(MaintenanceWindow).where(MaintenanceWindow.ended_at.is_(None))
    ).all()
    for w in open_windows:
        if w.planned_end_at and w.planned_end_at.date() == SCENARIO_DAY.date():
            candidate_starts.append(w.planned_end_at)
    seen = set()
    shifts_found = 0
    for s in sorted(x for x in candidate_starts if x >= r.start_at):
        if s in seen:
            continue
        seen.add(s)
        e = s + duration
        ev2 = evaluate_site(db, r.site_id, vehicle, s, e, min_power)
        codes = codes_of(ev2)
        if codes and shifts_found < 3:
            shifts_found += 1
            suggestions.append({
                "kind": "SHIFT_TIME",
                "confirmed": False,
                "meets_original_requirement": True,
                "connector_codes": codes,
                "start_at": s,
                "end_at": e,
                "title": f"顺延至 {s:%H:%M}–{e:%H:%M} 可由 {'/'.join(codes)} 承接",
                "detail": "候选时段，未生成新订单、未锁定容量。",
            })

    # 3) 降功率：60kW 需求可由 C 承接（若车辆受电允许）
    if min_power > 60 and float(vehicle.max_power_kw) >= 60:
        ev3 = evaluate_site(db, r.site_id, vehicle, r.start_at, r.end_at, 60.0)
        codes60 = codes_of(ev3)
        if codes60:
            suggestions.append({
                "kind": "LOWER_POWER",
                "confirmed": False,
                "meets_original_requirement": False,
                "connector_codes": codes60,
                "power_kw": 60,
                "start_at": r.start_at,
                "end_at": r.end_at,
                "title": f"降低功率要求至 60kW，可使用 {'/'.join(codes60)}（充电更慢）",
                "detail": "不满足原 90kW 时效承诺，需客户同意；仅为候选。",
            })

    if not suggestions:
        suggestions.append({
            "kind": "WAIT_RECOVERY",
            "confirmed": False,
            "meets_original_requirement": False,
            "title": "晚高峰内无安全候选，建议改约或退费补偿",
            "detail": "没有任何合格/已验收容量，不能把候选枪或未验收设备当作已确认履约。",
        })
    return suggestions


# ==================== 方案步骤模板 ====================

def merged_timeline(start: datetime, cooldown: int) -> list[dict]:
    fixed = [
        (4, "共用支路停电、验电、一次挂牌上锁（LOTO）", "LOTO"),
        (10, "更换 B 枪 CCS 接头总成", "REPAIR"),
        (3, "B 枪绝缘电阻测试（停电态）", "INSULATION"),
        (20, "共用断路器检查/测温/紧固（B、C 持续断电 ≥20 分钟）", "BREAKER"),
        (2, "撤牌、合断路器复电", "ENERGIZE"),
        (3, "B 枪负载测试（加载 ≥90kW）", "LOAD_TEST"),
        (3, "B/C 验收读数、联合签字与复电验证", "SIGNOFF"),
    ]
    total_fixed = sum(m for m, _, _ in fixed)
    t = start
    out = []
    for m, title, kind in fixed:
        out.append({"start_at": t, "end_at": t + timedelta(minutes=m),
                    "title": title, "kind": kind, "minutes": m})
        t += timedelta(minutes=m)
        if kind == "LOAD_TEST":
            # 负载测试后插入最短冷却稳定段
            out.append({"start_at": t, "end_at": t + timedelta(minutes=cooldown),
                        "title": f"负载后冷却、读数稳定（最短 {MIN_COOLDOWN_MINUTES} 分钟）",
                        "kind": "COOLDOWN", "minutes": cooldown})
            t += timedelta(minutes=cooldown)
    # total_fixed 不含冷却；最终结束 = start + total_fixed + cooldown
    return out


# ==================== 约束校验 ====================

def _tech_certs(t: Technician) -> set[str]:
    return set(jloads(t.certifications, []))


def validate_option(db: Session, spec: dict) -> tuple[list[dict], list[dict]]:
    """返回 (checks, violations)。每个 check：{code,label,ok,detail}。"""
    checks: list[dict] = []
    violations: list[dict] = []

    def add(code: str, label: str, ok: bool, detail: str):
        c = {"code": code, "label": label, "ok": ok, "detail": detail}
        checks.append(c)
        if not ok:
            violations.append({"code": code, "message": f"{label}：{detail}"})

    tech = db.get(Technician, spec["technician_id"]) if spec.get("technician_id") else None
    if tech is None or not tech.active:
        add("TECH_CERT", "技师资质", False, "指定技师不存在或已停用")
        return checks, violations

    needed_certs = {"HV_ELECTRICAL"}
    for a in spec["assignments"]:
        wo = a["wo"]
        if wo.required_cert:
            needed_certs.add(wo.required_cert)
    certs = _tech_certs(tech)
    missing = sorted(c for c in needed_certs if c not in certs)
    add("TECH_CERT", "技师资质", not missing,
        f"{tech.name} 持有 {sorted(certs)}；需要 {sorted(needed_certs)}"
        + (f"，缺少 {missing}" if missing else "，全部满足"))

    # 班次：每个作业区间都必须落在班次内
    shift_bad = []
    for a in spec["assignments"]:
        if a["start"] < tech.shift_start or a["end"] > tech.shift_end:
            shift_bad.append(
                f"{a['wo'].ref} {a['start']:%H:%M}-{a['end']:%H:%M} "
                f"超出班次 {tech.shift_start:%H:%M}-{tech.shift_end:%H:%M}"
            )
    add("TECH_SHIFT", "技师班次", not shift_bad,
        "全部作业在持证技师班次内" if not shift_bad else "；".join(shift_bad))

    # 同一技师的作业区间不能重叠（分开方案若硬塞同一人同一晚）。
    # MERGED 策略下两张工单共享同一个联合窗口，属于一次连续作业，不算重复排班。
    overlaps = []
    if spec.get("strategy") != "MERGED":
        asg = sorted(spec["assignments"], key=lambda a: a["start"])
        for a, b in zip(asg, asg[1:]):
            if a["end"] > b["start"]:
                overlaps.append(f"{a['wo'].ref} 与 {b['wo'].ref} 时间重叠")
    add("TECH_DOUBLE_BOOK", "技师不可同时两处作业", not overlaps,
        "作业时段互不重叠" if not overlaps else "；".join(overlaps))

    # 最迟完成时点
    late = []
    for a in spec["assignments"]:
        if a["end"] > a["wo"].deadline_at:
            late.append(
                f"{a['wo'].ref} 预计 {a['end']:%H:%M} 完成，"
                f"晚于最迟时点 {a['wo'].deadline_at:%H:%M}"
            )
    add("DEADLINE", "最迟完成时点", not late,
        "均在最迟完成时点前" if not late else "；".join(late))

    # 冷却时间
    cooldown = spec.get("cooldown", MIN_COOLDOWN_MINUTES)
    cd_ok = cooldown >= MIN_COOLDOWN_MINUTES
    add("COOLDOWN", "最短冷却时间", cd_ok,
        f"排程预留冷却 {cooldown} 分钟（要求 ≥{MIN_COOLDOWN_MINUTES} 分钟）"
        if cd_ok else
        f"排程冷却仅 {cooldown} 分钟，低于强制最短 {MIN_COOLDOWN_MINUTES} 分钟，读数/复电无效")

    # 备件批次
    spare = spec.get("spare")
    if spare and spare.get("qty", 0) > 0:
        batch = None
        bid = spare.get("batch_id")
        if bid is not None:
            batch = db.get(SparePartBatch, bid)
            if batch is None:
                add("SPARE_BATCH", "备件批次", False, f"批次 #{bid} 不存在")
        else:
            batch = db.scalars(
                select(SparePartBatch).where(
                    SparePartBatch.part_id == spare["part_id"],
                    SparePartBatch.quality_status == "RELEASED",
                    SparePartBatch.quantity_available >= spare["qty"],
                ).order_by(SparePartBatch.id)
            ).first()
        if batch is not None:
            part = db.get(SparePart, batch.part_id)
            if batch.quality_status != "RELEASED":
                add("SPARE_BATCH", "备件批次", False,
                    f"批次 {batch.batch_no} 状态 {batch.quality_status}，冻结不可领用")
            elif batch.quantity_available < spare["qty"]:
                add("SPARE_BATCH", "备件数量", False,
                    f"合格批次 {batch.batch_no} 仅 {batch.quantity_available} 件，"
                    f"需要 {spare['qty']} 件（接头备件仅一件）")
            else:
                add("SPARE_BATCH", "备件批次", True,
                    f"锁定合格批次 {batch.batch_no}，可用 {batch.quantity_available} 件，"
                    f"领用 {spare['qty']} 件（{part.sku if part else ''}）")
        elif not any(c["code"] == "SPARE_BATCH" for c in checks if not c["ok"]):
            add("SPARE_BATCH", "备件批次", False,
                f"没有数量 ≥{spare['qty']} 的合格（RELEASED）批次；接头备件仅一件")

    # 拓扑依赖：每张工单的停电块必须覆盖其拓扑隔离范围（不能声称断路器只停 B）
    for block in spec["blocks"]:
        for wo_id in block.get("wo_ids", []):
            wo = db.get(WorkOrder, wo_id)
            if wo is None or wo.equipment_id is None:
                continue
            expected = set(isolation_connector_ids(db, wo.equipment_id))
            actual = set(block["connector_ids"])
            if wo.ref == WO_BREAKER_REF or expected >= {2, 3} or len(expected) > 1:
                ok = expected.issubset(actual)
                add(
                    f"TOPOLOGY_{wo.ref}", "设备依赖/隔离范围", ok,
                    f"{wo.ref} 拓扑隔离枪口={sorted(expected)}，"
                    f"本次停电块={sorted(actual)}"
                    + ("" if ok else "，停电范围不足以安全作业（共用支路必须 B、C 同时断电）"),
                )

    # 分开方案的两块停电区间不能重叠，否则 C 在 B 作业期间也被停电，“保留 C”不成立
    if spec.get("strategy") == "SEPARATE" and len(spec["blocks"]) >= 2:
        blk = sorted(spec["blocks"], key=lambda b: b["start"])
        overlap_bad = []
        for x, y in zip(blk, blk[1:]):
            if x["end"] > y["start"]:
                overlap_bad.append(
                    f"{x['start']:%H:%M}-{x['end']:%H:%M} 与 "
                    f"{y['start']:%H:%M}-{y['end']:%H:%M} 重叠"
                )
        add("SEPARATION_OVERLAP", "分开作业须独立停电窗口", not overlap_bad,
            "B 与断路器窗口互不重叠，C 仅在断路器窗口退出" if not overlap_bad
            else "；".join(overlap_bad) + "：重叠会使 C 在 B 作业期间也退出，分开方案不成立")

    return checks, violations


# ==================== Dry-run 影响 + 容量 + 候选 ====================

def dry_run_option(db: Session, site_id: int, spec: dict) -> dict:
    """在 SAVEPOINT 内临时写入停电窗口、重算影响/容量/候选，随后回滚，不留数据。"""
    sp = db.begin_nested()
    impacts: list[dict] = []
    board_data: dict = {}
    alternatives: list[dict] = []
    try:
        created: list[MaintenanceWindow] = []
        # 先把本次方案的全部停电窗口落库，确保随后任一窗口做影响评估时，
        # 拓扑上关联的其他枪口停电（如合并方案里的 C）也已计入。
        for block in spec["blocks"]:
            for cid in block["connector_ids"]:
                mw = MaintenanceWindow(
                    connector_id=cid,
                    kind="OUTAGE",
                    start_at=block["start"],
                    planned_end_at=block["end"],
                    reason=block.get("reason", "排程预演"),
                    work_order_id=block.get("primary_wo_id"),
                )
                db.add(mw)
                db.flush()
                created.append(mw)
        for mw in created:
            impacts.extend(_assess_impacts(db, mw))
        impacts = merge_impacts(impacts)

        board_data = board(db, site_id)

        for i in impacts:
            r = db.get(Reservation, i["reservation_id"])
            if r is not None:
                alternatives.append({
                    "reservation_id": r.id,
                    "ref": r.ref,
                    "decision": i["decision"],
                    "suggestions": suggest_for_reservation(db, r),
                })
    finally:
        sp.rollback()
        db.expire_all()

    return {"impacts": impacts, "board": board_data, "alternatives": alternatives}


# ==================== 构造三个 canonical 方案 ====================

def _get_wo(db: Session, ref: str) -> WorkOrder:
    wo = db.scalar(select(WorkOrder).where(WorkOrder.ref == ref))
    if wo is None:
        raise HTTPException(500, f"种子工单 {ref} 不存在，请先重置种子数据")
    return wo


def _default_batch_id(db: Session, part_id: int | None) -> int | None:
    if part_id is None:
        return None
    b = db.scalars(
        select(SparePartBatch).where(
            SparePartBatch.part_id == part_id,
            SparePartBatch.quality_status == "RELEASED",
            SparePartBatch.quantity_available >= 1,
        ).order_by(SparePartBatch.id)
    ).first()
    return b.id if b else None


def build_compare(db: Session) -> dict:
    wo_b = _get_wo(db, WO_B_REF)
    wo_br = _get_wo(db, WO_BREAKER_REF)
    tech = db.scalars(select(Technician).order_by(Technician.id)).first()
    site_id = wo_b.site_id
    batch_id = _default_batch_id(db, wo_b.spare_part_id)

    b_start = _dt("17:40")
    b_end = _dt("18:10")
    merged_start = _dt("17:40")
    merged_end = _dt("18:30")
    br_start = _dt("18:10")
    br_end = _dt("18:40")  # 20 分钟核心检查 + 两端挂牌/复电共 30 分钟

    options_specs = []

    # 方案一：合并窗口 17:40-18:30（恰好落在班次/截止边界），B、C 全停
    options_specs.append({
        "option_key": "MERGED_1740",
        "strategy": "MERGED",
        "scope": "MERGED",
        "title": "合并为一次停电窗口（17:40–18:30，B、C 同时退出）",
        "technician_id": tech.id if tech else None,
        "cooldown": MIN_COOLDOWN_MINUTES,
        "assignments": [
            {"wo": wo_b, "start": merged_start, "end": merged_end},
            {"wo": wo_br, "start": merged_start, "end": merged_end},
        ],
        "blocks": [{
            "wo_ids": [wo_b.id, wo_br.id],
            "primary_wo_id": wo_br.id,
            "connector_ids": [2, 3],
            "start": merged_start,
            "end": merged_end,
            "reason": "合并窗口：共用支路一次停电，完成 B 接头更换与断路器检查",
        }],
        "spare": {"part_id": wo_b.spare_part_id, "batch_id": batch_id,
                  "qty": wo_b.spare_qty or 1},
        "timeline": merged_timeline(merged_start, MIN_COOLDOWN_MINUTES),
    })

    # 方案二：分开——只做 B（17:40-18:10），C 保留；断路器另择窗口
    options_specs.append({
        "option_key": "SEPARATE_B_ONLY",
        "strategy": "SEPARATE",
        "scope": "B_ONLY",
        "title": "分开作业·今晚先换 B 接头（17:40–18:10，C 继续服务）",
        "technician_id": tech.id if tech else None,
        "cooldown": MIN_COOLDOWN_MINUTES,
        "assignments": [{"wo": wo_b, "start": b_start, "end": b_end}],
        "blocks": [{
            "wo_ids": [wo_b.id],
            "primary_wo_id": wo_b.id,
            "connector_ids": [2],
            "start": b_start,
            "end": b_end,
            "reason": "单独更换 B 接头：仅隔离 B 本地开关，C 保持运行",
        }],
        "spare": {"part_id": wo_b.spare_part_id, "batch_id": batch_id,
                  "qty": wo_b.spare_qty or 1},
        "timeline": wo_steps_as_timeline(wo_b, b_start),
    })

    # 方案三：分开且硬要今晚接着做断路器 18:10-18:40 —— 班次/截止必然拒绝
    options_specs.append({
        "option_key": "SEPARATE_BOTH_TONIGHT",
        "strategy": "SEPARATE",
        "scope": "BOTH_TONIGHT",
        "title": "分开作业·今晚连续完成 B + 断路器（17:40–18:40，不安全，应拒绝）",
        "technician_id": tech.id if tech else None,
        "cooldown": MIN_COOLDOWN_MINUTES,
        "assignments": [
            {"wo": wo_b, "start": b_start, "end": b_end},
            {"wo": wo_br, "start": br_start, "end": br_end},
        ],
        "blocks": [
            {"wo_ids": [wo_b.id], "primary_wo_id": wo_b.id, "connector_ids": [2],
             "start": b_start, "end": b_end, "reason": "B 接头更换"},
            {"wo_ids": [wo_br.id], "primary_wo_id": wo_br.id, "connector_ids": [2, 3],
             "start": br_start, "end": br_end, "reason": "断路器检查，B、C 同时断电"},
        ],
        "spare": {"part_id": wo_b.spare_part_id, "batch_id": batch_id,
                  "qty": wo_b.spare_qty or 1},
        "timeline": wo_steps_as_timeline(wo_b, b_start)
                    + wo_steps_as_timeline(wo_br, br_start),
    })

    options_out = []
    for spec in options_specs:
        checks, violations = validate_option(db, spec)
        dry = dry_run_option(db, site_id, spec)
        options_out.append({
            "option_key": spec["option_key"],
            "strategy": spec["strategy"],
            "scope": spec["scope"],
            "title": spec["title"],
            "feasible": not violations,
            "checks": checks,
            "violations": violations,
            "blocks": [
                {
                    "connector_ids": b["connector_ids"],
                    "start_at": b["start"],
                    "end_at": b["end"],
                    "reason": b["reason"],
                }
                for b in spec["blocks"]
            ],
            "timeline": spec["timeline"],
            "impacts": dry["impacts"],
            "alternatives": dry["alternatives"],
            "post_plan_board": dry["board"],
        })

    pre_board = board(db, site_id)
    return {
        "site_id": site_id,
        "thresholds": {
            "insulation_warn_mohm": INSULATION_WARN_MOHM,
            "insulation_trip_mohm": INSULATION_TRIP_MOHM,
            "accept_insulation_mohm": ACCEPT_INSULATION_MOHM,
            "min_load_test_kw": MIN_LOAD_TEST_KW,
            "min_cooldown_minutes": MIN_COOLDOWN_MINUTES,
        },
        "pre_plan_board": pre_board,
        "options": options_out,
        "recommendation": (
            "合并窗口只安排一次持证技师与停电验证，但 18:00 高峰 B、C 同时退出，"
            "90kW 与 60kW 需求均受冲击；分开先做 B 可保留 C 对 60kW 需求的服务，"
            "但断路器窗口无法在唯一持证技师 18:30 下班前安全完成，必须另择持证班次。"
        ),
    }


def wo_steps_as_timeline(wo: WorkOrder, start: datetime) -> list[dict]:
    steps = jloads(wo.steps, [])
    t = start
    out = []
    for st in steps:
        m = int(st.get("minutes", 0))
        out.append({
            "start_at": t,
            "end_at": t + timedelta(minutes=m),
            "title": st.get("title", ""),
            "kind": st.get("kind", ""),
            "minutes": m,
        })
        t += timedelta(minutes=m)
    return out


# ==================== 采纳并排程（真正落库） ====================

def apply_schedule(db: Session, req: ScheduleRequest) -> dict:
    wo_b = _get_wo(db, WO_B_REF)
    wo_br = _get_wo(db, WO_BREAKER_REF)
    site_id = wo_b.site_id

    b_start = req.b_start_at or _dt("17:40")
    b_end = b_start + timedelta(minutes=30)
    merged_start = req.merged_start_at or _dt("17:40")
    cooldown = req.cooldown_minutes if req.cooldown_minutes is not None else MIN_COOLDOWN_MINUTES

    if req.strategy == "MERGED":
        merged_end = merged_start + timedelta(minutes=45 + cooldown)
        spec = {
            "strategy": "MERGED",
            "scope": "MERGED",
            "technician_id": req.technician_id,
            "cooldown": cooldown,
            "assignments": [
                {"wo": wo_b, "start": merged_start, "end": merged_end},
                {"wo": wo_br, "start": merged_start, "end": merged_end},
            ],
            "blocks": [{
                "wo_ids": [wo_b.id, wo_br.id], "primary_wo_id": wo_br.id,
                "connector_ids": [2, 3], "start": merged_start, "end": merged_end,
                "reason": "合并停电窗口：B 接头更换 + 共用断路器检查",
            }],
            "spare": {"part_id": wo_b.spare_part_id,
                      "batch_id": req.spare_batch_id or _default_batch_id(db, wo_b.spare_part_id),
                      "qty": wo_b.spare_qty or 1},
        }
    else:
        if req.scope == "BOTH_TONIGHT":
            br_start = req.breaker_start_at or _dt("18:10")
            br_end = br_start + timedelta(minutes=30)
            spec = {
                "strategy": "SEPARATE",
                "scope": "BOTH_TONIGHT",
                "technician_id": req.technician_id,
                "cooldown": cooldown,
                "assignments": [
                    {"wo": wo_b, "start": b_start, "end": b_end},
                    {"wo": wo_br, "start": br_start, "end": br_end},
                ],
                "blocks": [
                    {"wo_ids": [wo_b.id], "primary_wo_id": wo_b.id, "connector_ids": [2],
                     "start": b_start, "end": b_end, "reason": "B 接头更换"},
                    {"wo_ids": [wo_br.id], "primary_wo_id": wo_br.id, "connector_ids": [2, 3],
                     "start": br_start, "end": br_end, "reason": "断路器检查，B、C 同时断电"},
                ],
                "spare": {"part_id": wo_b.spare_part_id,
                          "batch_id": req.spare_batch_id or _default_batch_id(db, wo_b.spare_part_id),
                          "qty": wo_b.spare_qty or 1},
            }
        else:
            spec = {
                "strategy": "SEPARATE",
                "scope": "B_ONLY",
                "technician_id": req.technician_id,
                "cooldown": cooldown,
                "assignments": [{"wo": wo_b, "start": b_start, "end": b_end}],
                "blocks": [{
                    "wo_ids": [wo_b.id], "primary_wo_id": wo_b.id, "connector_ids": [2],
                    "start": b_start, "end": b_end, "reason": "单独更换 B 接头，仅隔离 B",
                }],
                "spare": {"part_id": wo_b.spare_part_id,
                          "batch_id": req.spare_batch_id or _default_batch_id(db, wo_b.spare_part_id),
                          "qty": wo_b.spare_qty or 1},
            }

    checks, violations = validate_option(db, spec)
    if violations:
        raise HTTPException(422, {"message": "排程未通过安全校验，已拒绝且未写入任何排程/容量",
                                 "violations": violations, "checks": checks})

    # 留痕方案
    plan = SchedulePlan(
        site_id=site_id, strategy=f"{req.strategy}:{req.scope}",
        feasible=True, applied=True,
        request_json=jdumps(req.model_dump(mode="json")),
        result_json=jdumps({"checks": checks}),
    )
    db.add(plan)
    db.flush()

    # 领用备件批次（扣减仅一件的库存）
    spare = spec.get("spare") or {}
    if spare.get("qty", 0) > 0 and spare.get("batch_id"):
        batch = db.get(SparePartBatch, spare["batch_id"])
        batch.quantity_available -= spare["qty"]

    # 更新工单 + 设备状态 + 建停电窗口
    wo_by_id = {wo_b.id: wo_b, wo_br.id: wo_br}
    touched_connectors: set[int] = set()
    impacts: list[dict] = []
    for a in spec["assignments"]:
        wo = a["wo"]
        wo.status = "SCHEDULED"
        wo.strategy = req.strategy
        wo.technician_id = req.technician_id
        wo.planned_start_at = a["start"]
        wo.planned_end_at = a["end"]
        if wo.id == wo_b.id:
            wo.spare_batch_id = spare.get("batch_id")
    for block in spec["blocks"]:
        for cid in block["connector_ids"]:
            c = db.get(Connector, cid)
            if c is not None:
                c.operational_status = "SCHEDULED"
                touched_connectors.add(cid)
            mw = MaintenanceWindow(
                connector_id=cid, kind="OUTAGE",
                start_at=block["start"], planned_end_at=block["end"],
                reason=block["reason"], work_order_id=block.get("primary_wo_id"),
                plan_id=plan.id,
            )
            db.add(mw)
    db.flush()
    # 全部窗口落库后再评估，保证合并方案的关联停电枪口已参与判定
    created_windows = db.scalars(
        select(MaintenanceWindow).where(MaintenanceWindow.plan_id == plan.id)
    ).all()
    impact_lists = [_assess_impacts(db, mw) for mw in created_windows]
    impacts = merge_impacts(*impact_lists)

    tech = db.get(Technician, req.technician_id)
    notify(db, site_id, "SCHEDULE_CONFIRMED",
           f"停电排程已采纳：{req.strategy}/{req.scope}",
           f"技师 {tech.name if tech else ''}；停电枪口 {sorted(touched_connectors)}；"
           f"共评估 {len(impacts)} 笔受影响预约。",
           audience="OPS")
    for i in impacts:
        if i["decision"] in ("NEEDS_RESELECTION", "MANUAL_CONTACT"):
            notify(db, site_id, "MAINTENANCE_SCHEDULED",
                   f"预约 {i['ref']} 受停电窗口影响（{i['decision']}）",
                   i["reason"], reservation_id=i["reservation_id"])

    db.flush()
    alternatives = []
    for i in impacts:
        r = db.get(Reservation, i["reservation_id"])
        if r is not None:
            alternatives.append({
                "reservation_id": r.id, "ref": r.ref,
                "decision": i["decision"],
                "suggestions": suggest_for_reservation(db, r),
            })

    return {
        "plan_id": plan.id,
        "strategy": req.strategy,
        "scope": spec.get("scope", req.scope),
        "checks": checks,
        "violations": [],
        "work_orders": [wo_to_dict(wo_b), wo_to_dict(wo_br)],
        "impacts": impacts,
        "alternatives": alternatives,
        "board": board(db, site_id),
        "notifications": notifications_out(db, site_id),
    }


def notifications_out(db: Session, site_id: int) -> list[dict]:
    rows = db.scalars(
        select(Notification).where(Notification.site_id == site_id)
        .order_by(Notification.id.desc())
    ).all()
    return [
        {
            "id": n.id, "reservation_id": n.reservation_id,
            "audience": n.audience, "channel": n.channel, "kind": n.kind,
            "title": n.title, "body": n.body, "created_at": n.created_at,
        }
        for n in rows
    ]


# ==================== 开工 / 隔离 ====================

def start_work(db: Session, wo_id: int) -> dict:
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise HTTPException(404, "工单不存在")
    if wo.status not in ("SCHEDULED", "PENDING"):
        raise HTTPException(409, f"工单状态 {wo.status} 不能开工")
    wo.status = "IN_PROGRESS"
    wo.actual_started_at = wo.actual_started_at or datetime.now()
    scope = jloads(wo.isolation_scope, {})
    for cid in scope.get("connector_ids", []):
        c = db.get(Connector, cid)
        if c is not None:
            c.operational_status = "ISOLATED"
    notify(db, wo.site_id, "WORK_STARTED", f"{wo.ref} 已挂牌开工",
           f"隔离范围：{scope.get('description', '')}", audience="OPS")
    db.flush()
    return wo_to_dict(wo)


# ==================== 验收 / 签字恢复 ====================

def _evaluate_acceptance(payload: dict) -> tuple[bool, list[str], int]:
    """返回 (是否通过, 原因列表, 实际冷却分钟)。"""
    reasons = []
    ins = float(payload.get("insulation_mohm") or 0)
    kw = float(payload.get("load_test_kw") or 0)
    load_pass = bool(payload.get("load_test_passed"))
    lt = payload.get("load_tested_at")
    fr = payload.get("final_reading_at")
    cooldown_actual = None
    if isinstance(lt, datetime) and isinstance(fr, datetime):
        cooldown_actual = int((fr - lt).total_seconds() // 60)

    ins_ok = ins >= ACCEPT_INSULATION_MOHM
    load_ok = load_pass and kw >= MIN_LOAD_TEST_KW
    cd_ok = cooldown_actual is not None and cooldown_actual >= MIN_COOLDOWN_MINUTES
    if not ins_ok:
        reasons.append(f"绝缘电阻 {ins:g}MΩ < 验收下限 {ACCEPT_INSULATION_MOHM:g}MΩ")
    if not load_ok:
        reasons.append(f"负载测试未通过（加载 {kw:g}kW / 判定 {load_pass}，"
                       f"需 ≥{MIN_LOAD_TEST_KW:g}kW 且合格）")
    if not cd_ok:
        reasons.append(
            f"冷却稳定时间 {cooldown_actual if cooldown_actual is not None else '缺失'} "
            f"分钟 < {MIN_COOLDOWN_MINUTES} 分钟，最终读数无效"
        )
    return (ins_ok and load_ok and cd_ok), reasons, (cooldown_actual or 0)


def record_acceptance(db: Session, wo_id: int, req: AcceptanceRequest | dict,
                      *, external_receipt: bool = False) -> dict:
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise HTTPException(404, "工单不存在")
    data = req.model_dump(mode="python") if isinstance(req, AcceptanceRequest) else dict(req)

    passed, reasons, cd_actual = _evaluate_acceptance(data)
    acceptance = {
        "insulation_mohm": data.get("insulation_mohm"),
        "load_test_kw": data.get("load_test_kw"),
        "load_test_passed": data.get("load_test_passed"),
        "load_tested_at": data.get("load_tested_at"),
        "final_reading_at": data.get("final_reading_at"),
        "cooldown_minutes_actual": cd_actual,
        "readings_note": data.get("readings_note", ""),
        "evaluated_at": datetime.now(),
        "pass": passed,
        "failure_reasons": reasons,
    }
    wo.acceptance = jdumps(acceptance)
    wo.acceptance_passed = passed

    scope = jloads(wo.isolation_scope, {})
    if passed:
        wo.status = "PENDING_ACCEPTANCE"
        for cid in scope.get("connector_ids", []):
            c = db.get(Connector, cid)
            if c is not None and c.operational_status != "FORCED_OUT":
                c.operational_status = "PENDING_ACCEPTANCE"
        notify(db, wo.site_id, "ACCEPTANCE_PENDING",
               f"{wo.ref} 测试读数合格，待签字复电",
               "绝缘与负载测试均通过；签字后才会结束停电窗口并恢复可售。",
               audience="OPS")
    else:
        wo.status = "ACCEPTANCE_FAILED"
        # 关键：绝不结束停电窗口，并把窗口延长到当日运营结束，容量持续为 0
        _hold_windows(db, wo, DAY_END)
        for cid in scope.get("connector_ids", []):
            c = db.get(Connector, cid)
            if c is not None:
                c.operational_status = "ACCEPTANCE_FAILED"
        notify(db, wo.site_id, "ACCEPTANCE_FAILED",
               f"{wo.ref} 验收测试失败，设备禁止恢复",
               "；".join(reasons), audience="OPS")
        # 自动生成返工工单
        rework = WorkOrder(
            ref=f"WO-RW-{wo.id}-{wo.delay_count + 1}",
            site_id=wo.site_id, kind="CORRECTIVE", status="PENDING",
            risk_level="HIGH", title=f"返工：{wo.title}（验收失败）",
            equipment_id=wo.equipment_id, connector_id=wo.connector_id,
            required_cert=wo.required_cert, immediate_shutdown_required=True,
            triage_detail=jdumps({"from": wo.ref, "reasons": reasons}),
            evidence=wo.evidence, deadline_at=DAY_END,
            isolation_scope=wo.isolation_scope, steps=wo.steps,
            min_cooldown_minutes=wo.min_cooldown_minutes,
            spare_part_id=wo.spare_part_id, spare_qty=wo.spare_qty,
        )
        db.add(rework)
        db.flush()
        notify(db, wo.site_id, "REWORK_CREATED",
               f"已自动创建返工工单 {rework.ref}", "验收失败后继续停运，重新排程持证技师与备件。",
               audience="OPS")

    db.flush()
    return {
        "work_order": wo_to_dict(wo),
        "passed": passed,
        "failure_reasons": reasons,
        "cooldown_minutes_actual": cd_actual,
        "board": board(db, wo.site_id),
    }


def signoff(db: Session, wo_id: int, req: SignoffRequest) -> dict:
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise HTTPException(404, "工单不存在")
    if wo.acceptance_passed is not True:
        acc = jloads(wo.acceptance, {})
        raise HTTPException(409, {
            "message": "绝缘及负载测试未全部通过，不能签字恢复设备",
            "failure_reasons": acc.get("failure_reasons", []),
        })
    signed_at = req.signed_at or datetime.now()
    wo.signoff_by = req.signoff_by
    wo.signoff_at = signed_at
    wo.status = "COMPLETED"

    # 结束该工单（及其排程计划）产生的未结束停电窗口
    windows = db.scalars(
        select(MaintenanceWindow).where(
            MaintenanceWindow.work_order_id == wo.id,
            MaintenanceWindow.ended_at.is_(None),
        )
    ).all()
    plan_ids = {w.plan_id for w in windows if w.plan_id}
    for w in windows:
        w.ended_at = signed_at
    # 合并方案：同 plan 的另一张工单窗口若也已验收，可一并结束；否则保留
    if plan_ids:
        siblings = db.scalars(
            select(MaintenanceWindow).where(
                MaintenanceWindow.plan_id.in_(plan_ids),
                MaintenanceWindow.ended_at.is_(None),
            )
        ).all()
        for w in siblings:
            owner = db.get(WorkOrder, w.work_order_id) if w.work_order_id else None
            if owner is not None and owner.id != wo.id and owner.acceptance_passed is True:
                w.ended_at = signed_at
    # autoflush=False：必须先把 ended_at 显式刷库，随后“是否仍有开放窗口”的查询才准确
    db.flush()

    scope = jloads(wo.isolation_scope, {})
    for cid in scope.get("connector_ids", []):
        c = db.get(Connector, cid)
        if c is not None:
            # 只有当该枪已不存在任何未结束窗口时才恢复 OPERATIONAL
            still_open = db.scalars(
                select(MaintenanceWindow).where(
                    MaintenanceWindow.connector_id == cid,
                    MaintenanceWindow.ended_at.is_(None),
                )
            ).first()
            c.operational_status = "SCHEDULED" if still_open else "OPERATIONAL"

    notify(db, wo.site_id, "DEVICE_RESTORED",
           f"{wo.ref} 已由 {req.signoff_by} 签字验收并恢复",
           "绝缘/负载测试与冷却时间均合格；新预约可使用该容量，历史订单状态不自动回滚。",
           audience="OPS")
    db.flush()
    return {"work_order": wo_to_dict(wo), "board": board(db, wo.site_id)}


def _hold_windows(db: Session, wo: WorkOrder, hold_until: datetime) -> list[MaintenanceWindow]:
    wins = db.scalars(
        select(MaintenanceWindow).where(
            MaintenanceWindow.work_order_id == wo.id,
            MaintenanceWindow.ended_at.is_(None),
        )
    ).all()
    plan_ids = {w.plan_id for w in wins if w.plan_id}
    for w in wins:
        if w.planned_end_at < hold_until:
            w.planned_end_at = hold_until
    # 合并方案延期/失败会拖住同一停电计划的全部窗口
    if plan_ids:
        sibs = db.scalars(
            select(MaintenanceWindow).where(
                MaintenanceWindow.plan_id.in_(plan_ids),
                MaintenanceWindow.ended_at.is_(None),
            )
        ).all()
        for w in sibs:
            if w.planned_end_at < hold_until:
                w.planned_end_at = hold_until
    return list(wins)


# ==================== 延期回执 ====================

def receive_delay(db: Session, wo_id: int, req: DelayRequest | dict) -> dict:
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise HTTPException(404, "工单不存在")
    if isinstance(req, DelayRequest):
        delay_minutes = req.delay_minutes
        new_end_req = req.new_planned_end_at
        new_start = req.new_planned_start_at
        reason = req.reason
    else:
        delay_minutes = req.get("delay_minutes")
        new_end_req = req.get("new_planned_end_at")
        new_start = req.get("new_planned_start_at")
        reason = req.get("reason", "工单延期")

    old_end = wo.planned_end_at or wo.deadline_at
    if new_end_req is not None:
        new_end = new_end_req
    elif delay_minutes:
        new_end = old_end + timedelta(minutes=delay_minutes)
    else:
        raise HTTPException(422, "延期回执必须给出 delay_minutes 或 new_planned_end_at")

    wo.delay_count += 1
    wo.planned_end_at = new_end
    if new_start is not None:
        wo.planned_start_at = new_start
    wo.status = "SCHEDULED" if wo.status in ("PENDING", "SCHEDULED") else wo.status
    wo.notes = (wo.notes + f"\n[延期#{wo.delay_count}] {reason} → 新完成时点 {new_end:%H:%M}").strip()

    # 延长未结束窗口（含合并计划兄弟窗口），并重算影响
    wins = db.scalars(
        select(MaintenanceWindow).where(
            MaintenanceWindow.work_order_id == wo.id,
            MaintenanceWindow.ended_at.is_(None),
        )
    ).all()
    plan_ids = {w.plan_id for w in wins if w.plan_id}
    for w in wins:
        w.planned_end_at = new_end
    if plan_ids:
        for w in db.scalars(
            select(MaintenanceWindow).where(
                MaintenanceWindow.plan_id.in_(plan_ids),
                MaintenanceWindow.ended_at.is_(None),
            )
        ).all():
            w.planned_end_at = new_end

    impacts: list[dict] = []
    for w in db.scalars(
        select(MaintenanceWindow).where(
            MaintenanceWindow.work_order_id == wo.id,
            MaintenanceWindow.ended_at.is_(None),
        )
    ).all():
        impacts.extend(_assess_impacts(db, w))

    tech = db.get(Technician, wo.technician_id) if wo.technician_id else None
    breaches = []
    if tech is not None and new_end > tech.shift_end:
        breaches.append(f"新完成时点 {new_end:%H:%M} 超出 {tech.name} 班次结束 {tech.shift_end:%H:%M}")
    if new_end > wo.deadline_at:
        breaches.append(f"晚于最迟完成时点 {wo.deadline_at:%H:%M}，安全期限失守，需升级/强制停运")

    notify(db, wo.site_id, "WORK_DELAYED",
           f"{wo.ref} 延期至 {new_end:%H:%M}",
           f"{reason}。" + ("；".join(breaches) if breaches else "仍在班次与安全期限内。"),
           audience="OPS")
    if breaches:
        notify(db, wo.site_id, "SCHEDULE_BREACH",
               f"{wo.ref} 延期后无法安全完成，需主管决策", "；".join(breaches),
               audience="SUPERVISOR")
    # 新产生的实质影响（忽略 UNCHANGED）
    notified_rids: set[int] = set()
    for i in merge_impacts(impacts):
        if i["decision"] in ("NEEDS_RESELECTION", "MANUAL_CONTACT", "PROTECTED_CHARGING"):
            notified_rids.add(i["reservation_id"])
            notify(db, wo.site_id, "DELAY_NOTICE",
                   f"预约 {i['ref']}：检修延期，容量重新计算", i["reason"],
                   reservation_id=i["reservation_id"])
    # 此前已进入改选/人工联系、不再出现在 HOLDING 评估里的订单，也要同步延期
    prior = db.scalars(
        select(Reservation).where(
            Reservation.site_id == wo.site_id,
            Reservation.status.in_([
                ReservationStatus.NEEDS_RESELECTION,
                ReservationStatus.MANUAL_CONTACT,
            ]),
        )
    ).all()
    for r in prior:
        if r.id not in notified_rids:
            notify(db, wo.site_id, "DELAY_NOTICE",
                   f"预约 {r.ref}：检修延期至 {new_end:%H:%M}，替代方案时刻已重算",
                   f"工单 {wo.ref} 延期，最新预计恢复 {new_end:%H:%M}；候选替代仅为建议，未确认。",
                   reservation_id=r.id)

    db.flush()
    return {
        "work_order": wo_to_dict(wo),
        "breaches": breaches,
        "impacts": impacts,
        "board": board(db, wo.site_id),
        "notifications": notifications_out(db, wo.site_id),
    }


# ==================== 绝缘读数研判 / 强制停机 ====================

def triage_reading(db: Session, req: TriageReadingRequest) -> dict:
    c = db.get(Connector, req.connector_id)
    if c is None:
        raise HTTPException(404, "枪口不存在")
    observed = req.observed_at or _dt("17:30")
    v = req.value_mohm
    if v < INSULATION_TRIP_MOHM:
        level, immediate = "CRITICAL", True
        decision = f"{v:g}MΩ < 强制停机阈值 {INSULATION_TRIP_MOHM:g}MΩ：立即停机，不得继续售卖"
    elif v < INSULATION_WARN_MOHM:
        level, immediate = "HIGH", False
        decision = (f"{v:g}MΩ 处于临界区（跳闸阈值 {INSULATION_TRIP_MOHM:g} / "
                    f"预警 {INSULATION_WARN_MOHM:g}）：尚未强制停机，待排程期间可继续预约，"
                    f"但须在最迟时点前完成")
    else:
        level, immediate = "LOW", False
        decision = f"{v:g}MΩ ≥ 预警值 {INSULATION_WARN_MOHM:g}MΩ：绝缘正常"

    # 把读数追加到 B 绝缘工单（若存在）
    wo = db.scalar(select(WorkOrder).where(WorkOrder.ref == WO_B_REF))
    if wo is not None and req.connector_id == 2:
        ev = jloads(wo.evidence, [])
        ev.append({
            "source": req.source, "metric": "绝缘电阻", "value": v, "unit": "MΩ",
            "threshold_warn": INSULATION_WARN_MOHM,
            "threshold_trip": INSULATION_TRIP_MOHM,
            "observed_at": observed, "note": req.note,
        })
        wo.evidence = jdumps(ev)
        wo.immediate_shutdown_required = immediate
        wo.risk_level = level if level != "LOW" else wo.risk_level
        wo.triage_detail = jdumps({
            "latest_reading_mohm": v, "decision": decision,
            "immediate_shutdown_required": immediate,
        })

    window_created = None
    if immediate:
        c.operational_status = "FORCED_OUT"
        exists = db.scalars(
            select(MaintenanceWindow).where(
                MaintenanceWindow.connector_id == c.id,
                MaintenanceWindow.ended_at.is_(None),
            )
        ).first()
        if exists is None:
            mw = MaintenanceWindow(
                connector_id=c.id, kind="OUTAGE",
                start_at=observed, planned_end_at=DAY_END,
                reason=f"绝缘 {v:g}MΩ 触及强制停机阈值，立即停运",
                work_order_id=wo.id if wo else None,
            )
            db.add(mw)
            db.flush()
            window_created = mw.id
        notify(db, c.site_id, "FORCED_SHUTDOWN",
               f"枪口 {c.code} 触及强制停机阈值，已停止售卖", decision, audience="OPS")

    db.flush()
    return {
        "connector_id": c.id, "connector_code": c.code,
        "value_mohm": v, "risk_level": level,
        "immediate_shutdown_required": immediate,
        "decision": decision, "outage_window_id": window_created,
        "operational_status": c.operational_status,
        "board": board(db, c.site_id),
    }


# ==================== 统一回执 ====================

def receive_receipt(db: Session, req: ReceiptCreate) -> dict:
    if req.type == "WORK_ORDER_DELAYED":
        if not req.work_order_id:
            raise HTTPException(422, "延期回执必须提供 work_order_id")
        delay_req = DelayRequest(
            delay_minutes=req.delay_minutes,
            new_planned_end_at=req.new_planned_end_at,
            reason=req.reason,
            idempotency_key=req.idempotency_key,
        )
        return receive_delay(db, req.work_order_id, delay_req)

    if req.type == "ACCEPTANCE_FAILED_RECEIPT":
        if not req.work_order_id:
            raise HTTPException(422, "测试失败回执必须提供 work_order_id")
        if req.insulation_mohm is None:
            raise HTTPException(422, "验收失败回执必须给出 insulation_mohm")
        acc_req = AcceptanceRequest(
            insulation_mohm=req.insulation_mohm,
            load_test_kw=req.load_test_kw or 0.0,
            load_test_passed=bool(req.load_test_passed),
            load_tested_at=req.load_tested_at,
            final_reading_at=req.final_reading_at,
            readings_note=req.reason,
            idempotency_key=req.idempotency_key,
        )
        return record_acceptance(db, req.work_order_id, acc_req, external_receipt=True)

    if req.type == "RESERVATION_CANCELLED":
        if not req.reservation_id:
            raise HTTPException(422, "预约取消回执必须提供 reservation_id")
        r = cancel_reservation(db, req.reservation_id)
        notify(db, r.site_id, "CANCEL_RECEIPT",
               f"预约 {r.ref} 取消回执已确认，容量释放/重算",
               "取消回执到达；当前可售容量与候选建议已重新计算。",
               reservation_id=r.id)
        db.flush()
        return {
            "reservation_id": r.id, "ref": r.ref, "status": r.status.value,
            "board": board(db, r.site_id),
            "suggestions": suggest_for_reservation(db, r),
            "notifications": notifications_out(db, r.site_id),
        }

    raise HTTPException(422, f"未知回执类型 {req.type}")
