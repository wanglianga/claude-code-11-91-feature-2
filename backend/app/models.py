from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    MaintenanceKind,
    ReservationMode,
    ReservationStatus,
    ServiceActionKind,
)


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    connector_type: Mapped[str] = mapped_column(String(16))
    max_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="vehicle")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(128))

    connectors: Mapped[list["Connector"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    code: Mapped[str] = mapped_column(String(16))
    connector_type: Mapped[str] = mapped_column(String(16))
    # 枪口额定/当前可用功率上限（kW）。维护降额通过维护窗口的 effective_power_kw 叠加
    rated_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    position_note: Mapped[str] = mapped_column(String(255), default="")
    # Feature-2 设备运行状态（正常/已排程/隔离/待验收/验收失败/强制停机）。
    # 可售容量仍以时间窗 MaintenanceWindow 为准，该字段用于运营看板与验收闸门。
    operational_status: Mapped[str] = mapped_column(
        String(32), default="OPERATIONAL", server_default="OPERATIONAL"
    )

    site: Mapped[Site] = relationship(back_populates="connectors")
    maintenance_windows: Mapped[list["MaintenanceWindow"]] = relationship(
        back_populates="connector", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_connector_site_code"),)


class MaintenanceWindow(Base):
    """运营端记录的降额/停用/恢复生效时段。

    恢复 = 给此前的降额/停用窗口设置实际结束时刻（ended_at），
    窗口本身保留，作为历史审计与“按真实交集判断影响”的依据。
    """

    __tablename__ = "maintenance_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("connectors.id"))
    kind: Mapped[MaintenanceKind] = mapped_column(Enum(MaintenanceKind))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    planned_end_at: Mapped[datetime] = mapped_column(DateTime)
    # 提前恢复时写入实际结束时刻；为空表示仍在生效
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 降额后的功率；停用窗口为空
    effective_power_kw: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    # Feature-2：由哪张工单排程/作业产生（手工维护窗口为空）
    work_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_orders.id"), nullable=True
    )
    # 同一次排程方案（可能为多枪/合并窗口）产生的窗口共享 plan_id
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedule_plans.id"), nullable=True
    )

    connector: Mapped[Connector] = relationship(back_populates="maintenance_windows")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    mode: Mapped[ReservationMode] = mapped_column(Enum(ReservationMode))
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), default=ReservationStatus.CONFIRMED
    )
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    min_power_kw: Mapped[float] = mapped_column(Numeric(6, 2))
    # 固定枪口模式：确认时锁定；资源池模式：确认时为空，到站分配
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    # 确认时的承诺快照（插头/功率/无障碍/锁定枪口或池名额），后续任何变化不得改写
    confirmation_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )

    vehicle: Mapped[Vehicle] = relationship(back_populates="reservations", foreign_keys=[vehicle_id])
    connector: Mapped[Connector | None] = relationship(foreign_keys=[connector_id])
    events: Mapped[list["ReservationEvent"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="ReservationEvent.id",
    )
    service_actions: Mapped[list["ServiceAction"]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan"
    )


class ReservationEvent(Base):
    """订单生命周期事件流，所有转换与维护影响均留痕。"""

    __tablename__ = "reservation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="events")


class ServiceAction(Base):
    """客服动作：替代枪口/费用调整必须指向当时的订单与设备状态。"""

    __tablename__ = "service_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"))
    kind: Mapped[ServiceActionKind] = mapped_column(Enum(ServiceActionKind))
    # 动作发生时的订单状态与设备状态快照（只读留痕）
    order_status_at_action: Mapped[ReservationStatus] = mapped_column(Enum(ReservationStatus))
    connector_state_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    target_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    amount: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )

    reservation: Mapped[Reservation] = relationship(back_populates="service_actions")
    target_connector: Mapped[Connector | None] = relationship(foreign_keys=[target_connector_id])


class IdempotencyRecord(Base):
    """幂等键 -> 首次请求的响应内容；重复提交原样返回。"""

    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_path: Mapped[str] = mapped_column(String(128))
    response_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )


# ==================== Feature-2 领域模型 ====================


class Equipment(Base):
    """设备拓扑节点：配电柜 / 共用支路 / 断路器 / 隔离开关 / 枪口终端。

    用 parent_id 表达供电路径。隔离某节点会连带断电其全部后代枪口，
    因此断路器 BR-BC 的隔离范围天然等于 {B, C}。
    """

    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(24))  # EquipmentKind
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("equipment.id"), nullable=True
    )
    # 当节点是枪口终端时关联到具体 Connector
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    # 关键运行读数（如断路器温度℃），仅作展示/告警证据
    metric_value: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    metric_unit: Mapped[str] = mapped_column(String(16), default="")
    alarm_threshold: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)

    parent: Mapped["Equipment | None"] = relationship(
        remote_side="Equipment.id", back_populates="children"
    )
    children: Mapped[list["Equipment"]] = relationship(
        back_populates="parent", cascade="all"
    )


class Technician(Base):
    """持证技师及其班次。certifications 存 JSON 数组字符串。"""

    __tablename__ = "technicians"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    employee_no: Mapped[str] = mapped_column(String(32), unique=True)
    certifications: Mapped[str] = mapped_column(Text, default="[]")
    shift_start: Mapped[datetime] = mapped_column(DateTime)
    shift_end: Mapped[datetime] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SparePart(Base):
    """备件型号（如 CCS 120kW 枪线接头总成）。"""

    __tablename__ = "spare_parts"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    compatible_connector_type: Mapped[str] = mapped_column(String(16), default="CCS")
    batches: Mapped[list["SparePartBatch"]] = relationship(
        back_populates="part", cascade="all, delete-orphan"
    )


class SparePartBatch(Base):
    """备件批次：排程必须校验具体批次是否合格、可用数量是否足够。"""

    __tablename__ = "spare_part_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("spare_parts.id"))
    batch_no: Mapped[str] = mapped_column(String(48))
    # 合格批次 RELEASED 才能用于检修；QUARANTINED 冻结不可用
    quality_status: Mapped[str] = mapped_column(String(24), default="RELEASED")
    quantity_available: Mapped[int] = mapped_column(Integer, default=0)

    part: Mapped[SparePart] = relationship(back_populates="batches")

    __table_args__ = (UniqueConstraint("part_id", "batch_no", name="uq_batch_part_no"),)


class WorkOrder(Base):
    """预防/告警/返工工单：证据、风险、截止时点、隔离范围、步骤、验收读数、签字。"""

    __tablename__ = "work_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    kind: Mapped[str] = mapped_column(String(16))   # WorkOrderKind
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    risk_level: Mapped[str] = mapped_column(String(16), default="HIGH")
    title: Mapped[str] = mapped_column(String(200))

    # 主要作业设备 / 主枪口（拓扑隔离范围另存 JSON，可能涉及多枪）
    equipment_id: Mapped[int | None] = mapped_column(
        ForeignKey("equipment.id"), nullable=True
    )
    connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("connectors.id"), nullable=True
    )
    required_cert: Mapped[str] = mapped_column(String(48), default="HV_ELECTRICAL")

    # 系统研判：该故障是否要求立即停机（含阈值与读数依据）
    immediate_shutdown_required: Mapped[bool] = mapped_column(Boolean, default=False)
    triage_detail: Mapped[str] = mapped_column(Text, default="{}")

    # 故障证据：[{source, metric, value, unit, threshold, observed_at, note}]
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    # 最迟完成时点（峰前/安全期限）
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    # 隔离范围：{equipment_ids, equipment_codes, connector_ids, connector_codes, description}
    isolation_scope: Mapped[str] = mapped_column(Text, default="{}")
    # 作业步骤：[{seq, title, minutes}]
    steps: Mapped[str] = mapped_column(Text, default="[]")
    min_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=5)

    # 排程结果
    strategy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    technician_id: Mapped[int | None] = mapped_column(
        ForeignKey("technicians.id"), nullable=True
    )
    spare_part_id: Mapped[int | None] = mapped_column(
        ForeignKey("spare_parts.id"), nullable=True
    )
    spare_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("spare_part_batches.id"), nullable=True
    )
    spare_qty: Mapped[int] = mapped_column(Integer, default=0)
    planned_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    planned_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 验收：{insulation_mohm, load_test_kw, load_test_passed, load_tested_at,
    #        signed_reading_at, cooldown_minutes, readings_note}
    acceptance: Mapped[str] = mapped_column(Text, default="{}")
    acceptance_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    signoff_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signoff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    delay_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(), onupdate=lambda: datetime.now()
    )


class SchedulePlan(Base):
    """一次排程方案对比/决策的留痕（dry-run 对比 applied=False，采纳后 applied=True）。"""

    __tablename__ = "schedule_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    strategy: Mapped[str] = mapped_column(String(16))
    feasible: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )


class Notification(Base):
    """已发出的通知（司机/值班/主管），运营端可核对。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id"), nullable=True
    )
    audience: Mapped[str] = mapped_column(String(32), default="DRIVER")
    channel: Mapped[str] = mapped_column(String(16), default="SMS")
    kind: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now()
    )
