import enum


class ConnectorType(str, enum.Enum):
    CCS = "CCS"
    CHADEMO = "CHAdeMO"
    GBT = "GBT"


class MaintenanceKind(str, enum.Enum):
    DERATE = "DERATE"          # 降额：枪口可用但功率下调
    OUTAGE = "OUTAGE"          # 停用：窗口内完全不可用


class ReservationMode(str, enum.Enum):
    CONNECTOR = "CONNECTOR"    # 固定枪口：确认即锁定具体枪口
    POOL = "POOL"              # 资源池：只承诺名额，到站再分配枪口


class ReservationStatus(str, enum.Enum):
    CONFIRMED = "CONFIRMED"                    # 已确认，持有容量
    ARRIVED = "ARRIVED"                        # 已到站（池模式此时分配枪口）
    CHARGING = "CHARGING"                      # 充电中，预约管理不可直接释放
    COMPLETED = "COMPLETED"                    # 已完成
    CANCELLED = "CANCELLED"                    # 已取消，释放容量
    NO_SHOW = "NO_SHOW"                        # 迟到失约，释放容量
    NEEDS_RESELECTION = "NEEDS_RESELECTION"    # 维护影响：需改选
    MANUAL_CONTACT = "MANUAL_CONTACT"          # 维护影响：需人工联系


# 仍持有服务容量、会在容量计算中占位的状态
HOLDING_STATUSES = (
    ReservationStatus.CONFIRMED,
    ReservationStatus.ARRIVED,
    ReservationStatus.CHARGING,
)


class ServiceActionKind(str, enum.Enum):
    ALTERNATIVE_CONNECTOR = "ALTERNATIVE_CONNECTOR"  # 客服指定替代枪口
    FEE_ADJUSTMENT = "FEE_ADJUSTMENT"                # 费用调整
    NOTE = "NOTE"                                    # 人工联系备注


class ImpactDecision(str, enum.Enum):
    UNCHANGED = "UNCHANGED"                      # 仍能履约，订单不动
    NEEDS_RESELECTION = "NEEDS_RESELECTION"      # 进入改选
    MANUAL_CONTACT = "MANUAL_CONTACT"            # 已到站，人工联系
    PROTECTED_CHARGING = "PROTECTED_CHARGING"    # 充电中，受保护不可释放


# ========== Feature-2：工单 / 拓扑 / 备件 / 持证技师 ==========

class EquipmentKind(str, enum.Enum):
    PANEL = "PANEL"                # 配电柜/上级电源
    FEEDER = "FEEDER"              # 支路（B、C 共用）
    BREAKER = "BREAKER"            # 断路器（告警设备）
    ISOLATOR = "ISOLATOR"          # 本地隔离开关（可只隔离单枪）
    CONNECTOR = "CONNECTOR"        # 充电桩/枪口终端


class OperationalStatus(str, enum.Enum):
    OPERATIONAL = "OPERATIONAL"              # 正常可售
    SCHEDULED = "SCHEDULED"                  # 已排程待作业（时段外仍可售）
    ISOLATED = "ISOLATED"                    # 已挂牌隔离/作业中
    PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"  # 检修完成待验收
    ACCEPTANCE_FAILED = "ACCEPTANCE_FAILED"  # 验收失败，禁止恢复
    FORCED_OUT = "FORCED_OUT"                # 触及强制停机阈值，立即停运


class WorkOrderKind(str, enum.Enum):
    PREVENTIVE = "PREVENTIVE"   # 周四巡检产生的预防工单（B 绝缘临界）
    ALARM = "ALARM"             # 告警工单（共用断路器温升）
    CORRECTIVE = "CORRECTIVE"   # 验收失败后的返工工单


class WorkOrderStatus(str, enum.Enum):
    PENDING = "PENDING"                         # 待排程（设备仍可接预约）
    SCHEDULED = "SCHEDULED"                     # 已排程
    IN_PROGRESS = "IN_PROGRESS"                 # 已挂牌，作业中
    PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"   # 作业完成，待验收读数
    ACCEPTANCE_FAILED = "ACCEPTANCE_FAILED"     # 验收失败
    COMPLETED = "COMPLETED"                     # 验收通过 + 签字，已恢复
    CANCELLED = "CANCELLED"


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PlanStrategy(str, enum.Enum):
    MERGED = "MERGED"      # 合并为一次停电窗口（B、C 同时退出）
    SEPARATE = "SEPARATE"  # 分开作业（先 B 后断路器，保留 C）


class ReceiptType(str, enum.Enum):
    WORK_ORDER_DELAYED = "WORK_ORDER_DELAYED"          # 工单延期回执
    ACCEPTANCE_FAILED = "ACCEPTANCE_FAILED_RECEIPT"    # 测试失败回执
    RESERVATION_CANCELLED = "RESERVATION_CANCELLED"    # 预约取消回执


class SuggestionKind(str, enum.Enum):
    OTHER_CONNECTOR_NOW = "OTHER_CONNECTOR_NOW"    # 当前时段其他合格枪
    SHIFT_TIME = "SHIFT_TIME"                      # 顺延到设备恢复后
    LOWER_POWER = "LOWER_POWER"                    # 降功率需求可由 C 承接
    WAIT_RECOVERY = "WAIT_RECOVERY"                # 等待恢复
    POOL = "POOL"                                  # 资源池名额


# 验收硬性阈值（演示用安全规程，单位 MΩ / 判定布尔）
INSULATION_WARN_MOHM = 10.0      # 低于此值：绝缘临界，高风险预防工单、峰前必须处理
INSULATION_TRIP_MOHM = 5.0       # 低于此值（严格 <）：触及强制停机阈值，立即停运
ACCEPT_INSULATION_MOHM = 50.0    # 复电验收绝缘电阻下限
MIN_LOAD_TEST_KW = 90.0          # 负载测试需达到的最低加载功率
MIN_COOLDOWN_MINUTES = 5         # 负载测试后到最终读数/签字复电的最短冷却稳定时间
