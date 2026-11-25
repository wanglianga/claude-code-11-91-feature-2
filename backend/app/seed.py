"""本地样例数据：东站 A/B/C/D 四枪 + 四辆车 + A 枪 18:00-18:30 有效预约。

运行：python -m app.seed
幂等策略：直接 drop/create 全部表后重灌，仅用于本地演示与重置。
"""

import json
from datetime import datetime

from .database import engine, SessionLocal
from .enums import ReservationMode, ReservationStatus
from .models import (
    Base,
    Connector,
    Equipment,
    Reservation,
    ReservationEvent,
    Site,
    SparePart,
    SparePartBatch,
    Technician,
    Vehicle,
    WorkOrder,
)

# 固定在“周五”这一天（2026-11-27 为周五），演示统一使用朴素本地时间
FRIDAY = "2026-11-27"


def _dt(hm: str) -> datetime:
    return datetime.fromisoformat(f"{FRIDAY}T{hm}:00")


def seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        # ---------- 车辆 ----------
        v1 = Vehicle(
            name="沪A·11111 家用轿车（已持有 A 枪预约）",
            connector_type="CCS", max_power_kw=120, accessible=False,
        )
        v2 = Vehicle(
            name="沪B·22222 小型车（最大受电仅 60kW）",
            connector_type="CCS", max_power_kw=60, accessible=False,
        )
        v3 = Vehicle(
            name="沪C·33333 无障碍车辆（需无障碍车位）",
            connector_type="CCS", max_power_kw=100, accessible=True,
        )
        v4 = Vehicle(
            name="沪D·44444 长续航 SUV（请求 90kW 的第二位车主）",
            connector_type="CCS", max_power_kw=150, accessible=False,
        )
        db.add_all([v1, v2, v3, v4])
        db.flush()

        # ---------- 站点与枪口 ----------
        site = Site(name="东站充电站", location="东站地下停车场 B1 层西侧")
        db.add(site)
        db.flush()

        connectors = [
            Connector(site_id=site.id, code="A", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 1 号位，靠通道，无障碍车位"),
            Connector(site_id=site.id, code="B", connector_type="CCS",
                      rated_power_kw=120, accessible=True,
                      position_note="西侧 2 号位，靠通道，无障碍车位"),
            Connector(site_id=site.id, code="C", connector_type="CCS",
                      rated_power_kw=60, accessible=False,
                      position_note="东侧靠墙，普通车位，额定仅 60kW"),
            Connector(site_id=site.id, code="D", connector_type="CHAdeMO",
                      rated_power_kw=120, accessible=False,
                      position_note="东侧柱后，CHAdeMO 接口，与 CCS 车辆不兼容"),
        ]
        db.add_all(connectors)
        db.flush()
        gun_a, gun_b, gun_c, gun_d = connectors

        # ---------- A 枪 18:00-18:30 已存在的有效预约 ----------
        snapshot = {
            "mode": ReservationMode.CONNECTOR.value,
            "connector_id": gun_a.id,
            "connector_code": "A",
            "connector_type": "CCS",
            "min_power_kw": 90,
            "effective_power_kw": 120.0,
            "accessible": True,
            "promise": "锁定枪口 A，全程有效功率不低于 120kW",
        }
        existing = Reservation(
            ref="R-SEEDA01",
            vehicle_id=v1.id,
            site_id=site.id,
            mode=ReservationMode.CONNECTOR,
            status=ReservationStatus.CONFIRMED,
            start_at=_dt("18:00"),
            end_at=_dt("18:30"),
            min_power_kw=90,
            connector_id=gun_a.id,
            confirmation_snapshot=json.dumps(snapshot, ensure_ascii=False),
        )
        db.add(existing)
        db.flush()
        db.add(ReservationEvent(
            reservation_id=existing.id,
            event_type="CONFIRMED",
            detail=json.dumps({"snapshot": snapshot}, ensure_ascii=False),
        ))

        # ---------- Feature-2：设备拓扑 ----------
        panel = Equipment(site_id=site.id, code="PANEL-EAST",
                          name="东站总配电柜", kind="PANEL")
        db.add(panel)
        db.flush()

        breaker_a = Equipment(site_id=site.id, code="BR-A", name="A 枪支路断路器",
                              kind="BREAKER", parent_id=panel.id)
        feeder_bc = Equipment(site_id=site.id, code="FEEDER-BC",
                              name="B/C 共用支路", kind="FEEDER", parent_id=panel.id)
        breaker_d = Equipment(site_id=site.id, code="BR-D", name="D 枪支路断路器",
                              kind="BREAKER", parent_id=panel.id)
        db.add_all([breaker_a, feeder_bc, breaker_d])
        db.flush()

        breaker_bc = Equipment(
            site_id=site.id, code="BR-BC", name="B/C 支路共用断路器（温升告警）",
            kind="BREAKER", parent_id=feeder_bc.id,
            metric_value=78.0, metric_unit="℃", alarm_threshold=70.0,
        )
        db.add(breaker_bc)
        db.flush()

        iso_b = Equipment(site_id=site.id, code="ISO-B",
                          name="B 枪前级隔离开关（可只隔离 B）",
                          kind="ISOLATOR", parent_id=breaker_bc.id)
        iso_c = Equipment(site_id=site.id, code="ISO-C",
                          name="C 枪前级隔离开关（可只隔离 C）",
                          kind="ISOLATOR", parent_id=breaker_bc.id)
        db.add_all([iso_b, iso_c])
        db.flush()

        db.add_all([
            Equipment(site_id=site.id, code="EQ-A", name="A 枪终端",
                      kind="CONNECTOR", parent_id=breaker_a.id, connector_id=gun_a.id),
            Equipment(site_id=site.id, code="EQ-B", name="B 枪终端",
                      kind="CONNECTOR", parent_id=iso_b.id, connector_id=gun_b.id),
            Equipment(site_id=site.id, code="EQ-C", name="C 枪终端",
                      kind="CONNECTOR", parent_id=iso_c.id, connector_id=gun_c.id),
            Equipment(site_id=site.id, code="EQ-D", name="D 枪终端",
                      kind="CONNECTOR", parent_id=breaker_d.id, connector_id=gun_d.id),
        ])

        # ---------- 持证技师与班次（唯一持全证者只到 18:30） ----------
        tech1 = Technician(
            name="王建国（持证高压电工）", employee_no="EMP-CERT-01",
            certifications=json.dumps([
                "HV_ELECTRICAL", "CONNECTOR_REPLACEMENT",
                "BREAKER_INSPECTION", "INSULATION_TEST",
            ], ensure_ascii=False),
            shift_start=_dt("08:00"), shift_end=_dt("18:30"), active=True,
        )
        tech2 = Technician(
            name="李学徒（仅高压证，无接头/断路器资质）", employee_no="EMP-APP-02",
            certifications=json.dumps(["HV_ELECTRICAL"], ensure_ascii=False),
            shift_start=_dt("10:00"), shift_end=_dt("22:00"), active=True,
        )
        db.add_all([tech1, tech2])

        # ---------- 备件型号与批次（CCS 接头合格件仅 1 件，另有冻结批次） ----------
        part_conn = SparePart(
            sku="CONN-CCS-120", name="CCS2 120kW 枪线接头总成",
            compatible_connector_type="CCS",
        )
        db.add(part_conn)
        db.flush()
        db.add_all([
            SparePartBatch(part_id=part_conn.id, batch_no="LOT-2026-11-A",
                           quality_status="RELEASED", quantity_available=1),
            SparePartBatch(part_id=part_conn.id, batch_no="LOT-2025-Q4-X",
                           quality_status="QUARANTINED", quantity_available=2),
        ])
        db.flush()

        # ---------- 两张工单 ----------
        b_steps = [
            {"seq": 1, "kind": "LOTO", "title": "断开 B 前级隔离开关，验电并挂牌上锁", "minutes": 4},
            {"seq": 2, "kind": "REPAIR", "title": "更换 CCS 接头总成并紧固端子", "minutes": 12},
            {"seq": 3, "kind": "INSULATION", "title": "停电态绝缘电阻测试", "minutes": 3},
            {"seq": 4, "kind": "LOAD_TEST", "title": "复电后负载测试（加载 ≥90kW）", "minutes": 4},
            {"seq": 5, "kind": "COOLDOWN", "title": "负载后冷却、读数稳定", "minutes": 5},
            {"seq": 6, "kind": "SIGNOFF", "title": "填写验收读数、签字并复电", "minutes": 2},
        ]
        br_steps = [
            {"seq": 1, "kind": "LOTO", "title": "BR-BC 停电、上级验电、挂牌上锁（B、C 同时断电）", "minutes": 5},
            {"seq": 2, "kind": "BREAKER", "title": "断路器温升/绝缘/紧固检查（B、C 持续断电 20 分钟）", "minutes": 20},
            {"seq": 3, "kind": "SIGNOFF", "title": "复电、B/C 送电验证并签字", "minutes": 5},
        ]

        wo_b = WorkOrder(
            ref="WO-B-INS", site_id=site.id, kind="PREVENTIVE", status="PENDING",
            risk_level="HIGH",
            title="B 枪绝缘电阻临界——更换 CCS 接头总成（周四巡检预防工单）",
            equipment_id=iso_b.id, connector_id=gun_b.id,
            required_cert="CONNECTOR_REPLACEMENT",
            immediate_shutdown_required=False,
            triage_detail=json.dumps({
                "latest_reading_mohm": 6.0,
                "warn_mohm": 10.0, "trip_mohm": 5.0,
                "decision": "6MΩ 临界但高于强制停机阈值 5MΩ：待排程期间仍可预约，峰前必须完成",
            }, ensure_ascii=False),
            evidence=json.dumps([{
                "source": "周四夜间巡检", "metric": "绝缘电阻", "value": 6.0,
                "unit": "MΩ", "threshold_warn": 10.0, "threshold_trip": 5.0,
                "observed_at": "2026-11-26T21:10:00",
                "note": "B 枪直流绝缘读数被标为临界，未见直接接地；主管要求周五晚高峰前处理",
            }], ensure_ascii=False),
            deadline_at=_dt("18:30"),
            isolation_scope=json.dumps({
                "equipment_ids": [iso_b.id],
                "equipment_codes": ["ISO-B", "EQ-B"],
                "connector_ids": [gun_b.id], "connector_codes": ["B"],
                "description": "断开 B 枪前级隔离开关 ISO-B 并挂牌，仅隔离 B；C 经 BR-BC 仍可运行",
            }, ensure_ascii=False),
            steps=json.dumps(b_steps, ensure_ascii=False),
            min_cooldown_minutes=5,
            spare_part_id=part_conn.id, spare_qty=1,
            planned_start_at=_dt("17:40"), planned_end_at=_dt("18:10"),
        )
        wo_br = WorkOrder(
            ref="WO-BC-BRKR", site_id=site.id, kind="ALARM", status="PENDING",
            risk_level="HIGH",
            title="B/C 共用支路断路器温升告警检查（告警工单，与 B 枪同支路）",
            equipment_id=breaker_bc.id, connector_id=None,
            required_cert="BREAKER_INSPECTION",
            immediate_shutdown_required=False,
            triage_detail=json.dumps({
                "metric": "触头温度", "value_c": 78.0, "alarm_threshold_c": 70.0,
                "decision": "温升告警来自 B、C 共用断路器，不能当作与 B 不相关的独立保养",
            }, ensure_ascii=False),
            evidence=json.dumps([{
                "source": "温控告警", "metric": "断路器触头温度", "value": 78.0,
                "unit": "℃", "threshold_alarm": 70.0,
                "observed_at": "2026-11-27T07:55:00",
                "note": "红外测温与在线温升一致；检查需 BR-BC 停电，B、C 同时退出 20 分钟",
            }], ensure_ascii=False),
            deadline_at=_dt("18:30"),
            isolation_scope=json.dumps({
                "equipment_ids": [breaker_bc.id],
                "equipment_codes": ["BR-BC", "ISO-B", "ISO-C", "EQ-B", "EQ-C"],
                "connector_ids": [gun_b.id, gun_c.id], "connector_codes": ["B", "C"],
                "description": "断开共用断路器 BR-BC 并在 FEEDER-BC 验电挂牌，B、C 同时停电",
            }, ensure_ascii=False),
            steps=json.dumps(br_steps, ensure_ascii=False),
            min_cooldown_minutes=5,
            planned_start_at=_dt("18:10"), planned_end_at=_dt("18:40"),
        )
        db.add_all([wo_b, wo_br])

        db.commit()

        print("种子数据完成：")
        print(f"  站点: {site.name}（id={site.id}），枪口 A/B=120kW CCS，C=60kW CCS，D=120kW CHAdeMO")
        print(f"  车辆: V1 120kW / V2 60kW / V3 无障碍 100kW / V4 150kW（均为 CCS）")
        print(f"  预约: {existing.ref} 锁定 A 枪 {FRIDAY} 18:00-18:30")
        print("  拓扑: BR-BC 为 B/C 共用断路器（隔离范围 B+C）；ISO-B 可单独隔离 B")
        print("  工单: WO-B-INS 接头更换 17:40-18:10（预防）；WO-BC-BRKR 断路器检查（告警，B+C 断电20分钟）")
        print("  资源: 唯一全证技师王建国班次至 18:30；CCS 接头合格批次仅 1 件")
        print("  => 18:00-18:10 的 90kW 请求：B 是唯一剩余合格容量")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
