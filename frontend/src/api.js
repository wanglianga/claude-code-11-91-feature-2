async function request(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    // FastAPI 的 detail 可能是字符串或结构化对象
    const detail = data.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || JSON.stringify(detail || data)
    const err = new Error(message)
    err.status = resp.status
    err.detail = detail
    throw err
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
}

export function newIdempotencyKey() {
  // 前端生成可读幂等键；重复提交时复用同一个键即可回放原结果
  return 'ui-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8)
}

export const STATUS_LABELS = {
  CONFIRMED: '已确认',
  ARRIVED: '已到站',
  CHARGING: '充电中（受保护）',
  COMPLETED: '已完成',
  CANCELLED: '已取消（已释放）',
  NO_SHOW: '迟到失约（已释放）',
  NEEDS_RESELECTION: '需改选',
  MANUAL_CONTACT: '需人工联系',
}

export const MODE_LABELS = {
  CONNECTOR: '固定枪口',
  POOL: '资源池',
}

export const IMPACT_LABELS = {
  UNCHANGED: '继续履约',
  NEEDS_RESELECTION: '进入改选',
  MANUAL_CONTACT: '人工联系',
  PROTECTED_CHARGING: '充电中受保护',
}

export const WO_STATUS_LABELS = {
  PENDING: '待排程（设备仍可预约）',
  SCHEDULED: '已排程',
  IN_PROGRESS: '已挂牌作业中',
  PENDING_ACCEPTANCE: '测试合格待签字',
  ACCEPTANCE_FAILED: '验收失败（禁止恢复）',
  COMPLETED: '已验收恢复',
  CANCELLED: '已取消',
}

export const DEVICE_STATUS_LABELS = {
  OPERATIONAL: '正常可售',
  SCHEDULED: '已排程待作业',
  ISOLATED: '已隔离/作业中',
  PENDING_ACCEPTANCE: '待验收',
  ACCEPTANCE_FAILED: '验收失败·停运',
  FORCED_OUT: '强制停机',
}

export const RISK_LABELS = {
  LOW: '低', MEDIUM: '中', HIGH: '高', CRITICAL: '紧急/严重',
}

export const SUGGESTION_LABELS = {
  OTHER_CONNECTOR_NOW: '当前时段替代枪（候选）',
  SHIFT_TIME: '顺延时段（候选）',
  LOWER_POWER: '降功率方案（候选）',
  WAIT_RECOVERY: '等待恢复/无安全候选',
  POOL: '资源池名额（候选）',
}

export const WO_KIND_LABELS = {
  PREVENTIVE: '预防工单',
  ALARM: '告警工单',
  CORRECTIVE: '返工工单',
}
