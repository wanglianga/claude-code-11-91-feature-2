<template>
  <div class="stack">
    <!-- 场景说明 + 全局操作 -->
    <div class="panel">
      <h2>🛠 运维工单 · 设备拓扑 · 停电排程 · 验收闸门</h2>
      <p class="muted" style="margin:0 0 10px">
        周四巡检把 <b>B 枪绝缘读数 6MΩ 标为临界</b>（未达 5MΩ 强制停机阈值，待排程仍可预约）；
        共用 <b>BR-BC 断路器温升 78℃ 告警</b>来自 B、C 共用支路。A 枪 18:00–18:30 已被 90kW 单占用，
        B 是该时段唯一剩余 90kW 合格枪，C 仅 60kW，D 接口不兼容。唯一全证技师班次至 18:30，合格 CCS 接头备件仅 1 件。
      </p>
      <div class="row" style="align-items:flex-end">
        <label class="field" style="margin:0">
          <span>排程技师</span>
          <select v-model.number="sched.technician_id">
            <option v-for="t in techs" :key="t.id" :value="t.id">
              {{ t.name }}（至 {{ fmtHM(t.shift_end) }}）
            </option>
          </select>
        </label>
        <label class="field" style="margin:0">
          <span>备件批次</span>
          <select v-model.number="sched.spare_batch_id">
            <option v-for="b in allBatches" :key="b.id" :value="b.id">
              {{ b.batch_no }} · {{ b.quality_status }} · 余 {{ b.quantity_available }}
            </option>
          </select>
        </label>
        <label class="field" style="margin:0;max-width:150px">
          <span>冷却分钟(≥5)</span>
          <input type="number" v-model.number="sched.cooldown_minutes" />
        </label>
        <button class="secondary" @click="runCompare">🔍 重新计算两种方案影响</button>
        <button class="ghost" @click="loadAll">刷新</button>
      </div>
      <div v-if="notice" :class="['notice', notice.kind]" style="margin-top:12px;margin-bottom:0">
        <div>{{ notice.text }}</div>
        <ul v-if="notice.violations" style="margin:6px 0 0;padding-left:18px">
          <li v-for="(v, i) in notice.violations" :key="i">{{ v.message }}</li>
        </ul>
      </div>
    </div>

    <!-- 方案对比 -->
    <div class="panel" v-if="compare">
      <h2>① 合并窗口 vs 分开作业（Dry-run，不落数据）</h2>
      <p class="muted">{{ compare.recommendation }}</p>
      <div class="opt-grid">
        <div v-for="opt in compare.options" :key="opt.option_key"
             :class="['opt-card', opt.feasible ? 'feasible' : 'infeasible']">
          <div class="opt-head">
            <strong>{{ opt.title }}</strong>
            <span :class="['badge', opt.feasible ? 'ok' : 'bad']">
              {{ opt.feasible ? '安全校验通过' : '不可安全排程·应拒绝' }}
            </span>
          </div>

          <ul class="checks">
            <li v-for="c in opt.checks" :key="c.code" :class="c.ok ? 'ok' : 'bad'">
              {{ c.ok ? '✓' : '✗' }} <b>{{ c.label }}</b>：<span class="muted">{{ c.detail }}</span>
            </li>
          </ul>

          <div v-if="opt.blocks?.length" class="chip-row">
            <span v-for="(b, i) in opt.blocks" :key="i" class="seg">
              停电 {{ fmtHM(b.start_at) }}–{{ fmtHM(b.end_at) }} · 枪 {{ gunIdsToCodes(b.connector_ids).join('/') }}
            </span>
          </div>

          <table v-if="opt.impacts?.length" style="margin-top:8px">
            <thead><tr><th>订单</th><th>判定</th></tr></thead>
            <tbody>
              <tr v-for="i in opt.impacts.filter(x => x.decision !== 'UNCHANGED')" :key="i.reservation_id">
                <td class="mono">{{ i.ref }}</td>
                <td><span :class="['badge', impactBadge(i.decision)]">{{ IMPACT_LABELS[i.decision] || i.decision }}</span></td>
              </tr>
              <tr v-if="!opt.impacts.some(x => x.decision !== 'UNCHANGED')">
                <td colspan="2" class="muted">无受实质影响订单（先在下方建立 18:00 预约后再对比）</td>
              </tr>
            </tbody>
          </table>

          <div v-if="opt.post_plan_board" class="mini-board">
            <span v-for="p in opt.post_plan_board.peak_1800_1830" :key="p.min_power_kw" class="seg">
              高峰 18:00–18:30 ≥{{ p.min_power_kw }}kW 可售：
              <b :class="p.sellable_connector_codes.length ? 'ok-txt' : 'bad-txt'">
                {{ p.sellable_connector_codes.length ? p.sellable_connector_codes.join('/') : '无' }}
              </b>
            </span>
          </div>

          <button class="small" :class="opt.feasible ? '' : 'danger-btn'"
                  style="margin-top:10px" @click="applyOption(opt)">
            {{ opt.feasible ? '采纳并排程（写入停电窗口/扣备件）' : '尝试排程（系统必须拒绝）' }}
          </button>
        </div>
      </div>
    </div>

    <div class="grid two">
      <!-- 左列：工单 + 回执 + 预约替代 -->
      <section class="stack">
        <div class="panel" v-for="wo in wos" :key="wo.id">
          <h2>
            {{ wo.ref }}
            <span class="badge info">{{ WO_KIND_LABELS[wo.kind] }}</span>
            <span :class="['badge', woStatusBadge(wo.status)]">{{ WO_STATUS_LABELS[wo.status] }}</span>
            <span :class="['badge', riskBadge(wo.risk_level)]">风险 {{ RISK_LABELS[wo.risk_level] }}</span>
          </h2>
          <p style="margin:4px 0 8px"><b>{{ wo.title }}</b></p>
          <div class="kv">
            <span class="k">故障证据</span>
            <span>
              <span v-for="(e, i) in wo.evidence" :key="i" class="seg">
                {{ e.metric }} {{ e.value }}{{ e.unit }}（阈值 {{ e.threshold_trip || e.threshold_alarm || e.threshold_warn }}）
              </span>
            </span>
            <span class="k">立即停机研判</span>
            <span :class="wo.immediate_shutdown_required ? 'bad-txt' : 'ok-txt'">
              {{ wo.immediate_shutdown_required ? '是·已强制停运' : '否·临界待排程仍可售' }}
              <span class="muted">{{ wo.triage_detail?.decision }}</span>
            </span>
            <span class="k">最迟完成</span><span class="mono">{{ fmt(wo.deadline_at) }}</span>
            <span class="k">计划窗口</span>
            <span class="mono">{{ fmtHM(wo.planned_start_at) }}–{{ fmtHM(wo.planned_end_at) }}</span>
            <span class="k">隔离范围</span><span>{{ wo.isolation_scope?.description }}</span>
            <span class="k">作业步骤</span>
            <span>
              <ol style="margin:0;padding-left:18px">
                <li v-for="s in wo.steps" :key="s.seq">{{ s.title }} <span class="muted">({{ s.minutes }}′)</span></li>
              </ol>
            </span>
            <span class="k">验收读数</span>
            <span v-if="wo.acceptance_passed === null" class="muted">尚未验收</span>
            <span v-else>
              <span :class="wo.acceptance_passed ? 'ok-txt' : 'bad-txt'">
                {{ wo.acceptance_passed ? '测试合格' : '验收未通过' }}
              </span>
              绝缘 {{ wo.acceptance?.insulation_mohm }}MΩ /
              负载 {{ wo.acceptance?.load_test_kw }}kW /
              实际冷却 {{ wo.acceptance?.cooldown_minutes_actual }}′
              <ul v-if="wo.acceptance?.failure_reasons?.length" class="reasons">
                <li v-for="(r, i) in wo.acceptance.failure_reasons" :key="i">{{ r }}</li>
              </ul>
            </span>
            <span class="k">签字人</span>
            <span>{{ wo.signoff_by ? wo.signoff_by + ' · ' + fmt(wo.signoff_at) : '—' }}</span>
          </div>

          <!-- 作业操作 -->
          <div class="row" style="margin-top:10px" v-if="wo.kind !== 'CORRECTIVE'">
            <button class="small secondary" @click="startWork(wo)"
                    :disabled="!['PENDING','SCHEDULED'].includes(wo.status)">挂牌开工</button>
          </div>

          <!-- 验收录入 -->
          <div v-if="accForm[wo.id]" style="margin-top:10px;border-top:1px dashed var(--line);padding-top:10px">
            <h3 style="margin-top:0">录入验收读数（绝缘≥50MΩ、负载≥90kW、冷却≥5′）</h3>
            <div class="row">
              <label class="field"><span>绝缘 MΩ</span>
                <input type="number" v-model.number="accForm[wo.id].insulation_mohm" /></label>
              <label class="field"><span>负载 kW</span>
                <input type="number" v-model.number="accForm[wo.id].load_test_kw" /></label>
              <label class="field" style="max-width:120px"><span>负载判定</span>
                <select v-model="accForm[wo.id].load_test_passed">
                  <option :value="true">通过</option><option :value="false">不通过</option>
                </select></label>
            </div>
            <div class="row">
              <label class="field"><span>负载测试时刻</span>
                <input type="datetime-local" v-model="accForm[wo.id].load_tested_at" /></label>
              <label class="field"><span>最终读数时刻</span>
                <input type="datetime-local" v-model="accForm[wo.id].final_reading_at" /></label>
            </div>
            <button class="small" @click="submitAcceptance(wo)">提交验收读数</button>

            <div class="row" style="margin-top:10px;align-items:flex-end">
              <label class="field" style="margin:0"><span>签字人</span>
                <input v-model="signName[wo.id]" placeholder="如：王建国" /></label>
              <button class="small" @click="signoff(wo)">签字并恢复设备</button>
            </div>
          </div>
        </div>

        <!-- 三类回执 -->
        <div class="panel">
          <h2>② 三类外部回执到达</h2>
          <div class="row" style="align-items:flex-end">
            <label class="field" style="margin:0">
              <span>延期工单</span>
              <select v-model.number="delay.wo_id">
                <option v-for="wo in wos.filter(w=>w.kind!=='CORRECTIVE')" :key="wo.id" :value="wo.id">{{ wo.ref }}</option>
              </select>
            </label>
            <label class="field" style="margin:0;max-width:130px">
              <span>延期分钟</span>
              <input type="number" v-model.number="delay.minutes" />
            </label>
            <button class="small warn-btn" @click="sendDelayReceipt">工单延期回执</button>
          </div>
          <div class="row" style="margin-top:10px;align-items:center">
            <button class="small danger-btn" @click="sendFailReceipt">
              测试失败回执（绝缘8 / 负载40，B 禁止恢复）
            </button>
            <button class="small secondary" @click="sendPassReceipt">
              测试合格回执（绝缘80 / 负载120）
            </button>
          </div>
          <div class="row" style="margin-top:10px;align-items:flex-end">
            <label class="field" style="margin:0">
              <span>预约取消回执</span>
              <select v-model.number="cancelId">
                <option v-for="r in reservations" :key="r.id" :value="r.id">
                  {{ r.ref }} — {{ STATUS_LABELS[r.status] }}
                </option>
              </select>
            </label>
            <button class="small" @click="sendCancelReceipt">预约取消回执</button>
          </div>
        </div>

        <!-- 研判 + 预约候选替代 -->
        <div class="panel">
          <h2>③ 绝缘阈值研判与候选替代（候选 ≠ 已确认）</h2>
          <div class="row" style="align-items:flex-end">
            <label class="field" style="margin:0;max-width:160px">
              <span>B 枪最新绝缘 MΩ</span>
              <input type="number" step="0.1" v-model.number="triageValue" />
            </label>
            <button class="small danger-btn" @click="triage(2.0)">模拟恶化到 2.0（强制停机）</button>
            <button class="small secondary" @click="triage(6.0)">读数 6.0（临界可售）</button>
          </div>

          <h3>在途预约与候选替代</h3>
          <table>
            <thead><tr><th>订单</th><th>状态</th><th>时间</th><th>枪</th><th></th></tr></thead>
            <tbody>
              <tr v-for="r in reservations" :key="r.id">
                <td class="mono">{{ r.ref }}<br /><span class="muted">≥{{ r.min_power_kw }}kW</span></td>
                <td><span :class="['badge', statusBadge(r.status)]">{{ STATUS_LABELS[r.status] }}</span></td>
                <td class="mono">{{ fmtHM(r.start_at) }}–{{ fmtHM(r.end_at) }}</td>
                <td>{{ r.connector_id ? gunCode(r.connector_id) : '池' }}</td>
                <td><button class="small ghost" @click="loadAlt(r)">候选替代</button></td>
              </tr>
            </tbody>
          </table>

          <div v-for="r in reservations" :key="'alt-'+r.id">
            <div v-if="altMap[r.id]" style="margin-top:8px">
              <h3 style="margin-bottom:4px">{{ r.ref }} 的候选建议</h3>
              <div v-for="(s, i) in altMap[r.id].suggestions" :key="i" class="candidate">
                <div class="head">
                  <span class="badge warn">候选·未确认</span>
                  <b>{{ SUGGESTION_LABELS[s.kind] || s.kind }}</b>
                </div>
                <div>{{ s.title }}</div>
                <div class="muted" style="font-size:12px">{{ s.detail }}</div>
                <button v-if="s.kind==='OTHER_CONNECTOR_NOW' && s.connector_codes?.length"
                        class="small secondary" style="margin-top:6px"
                        @click="confirmAlternative(r, s)">
                  执行客服改派并确认到 {{ s.connector_codes[0] }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 右列：容量看板 + 拓扑 + 资源 + 通知 -->
      <section class="stack">
        <div class="panel" v-if="board">
          <h2>④ 可售容量看板（回执后实时重算）</h2>
          <div class="chip-row" style="margin-bottom:10px">
            <span v-for="c in board.connectors" :key="c.id" class="seg">
              枪 {{ c.code }} · {{ c.rated_power_kw }}kW ·
              <b :class="deviceOk(c.operational_status) ? 'ok-txt' : 'bad-txt'">
                {{ DEVICE_STATUS_LABELS[c.operational_status] || c.operational_status }}
              </b>
            </span>
          </div>
          <table>
            <thead><tr><th>时间片</th><th>需求</th><th>可售合格枪</th><th>池名额</th></tr></thead>
            <tbody>
              <tr v-for="(row, i) in board.matrix" :key="i">
                <td class="mono">{{ row.slice }}</td>
                <td>≥{{ row.min_power_kw }}kW</td>
                <td :class="row.sellable_connector_codes.length ? 'ok-txt' : 'bad-txt'">
                  {{ row.sellable_connector_codes.length ? row.sellable_connector_codes.join('/') : '无' }}
                </td>
                <td>{{ row.pool_slots }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="panel" v-if="topology">
          <h2>⑤ 设备拓扑与隔离依赖</h2>
          <div v-for="n in topology.nodes" :key="n.id" class="topo-node"
               :style="{ paddingLeft: (8 + n.depth * 16) + 'px' }">
            <span class="mono muted">{{ kindIcon(n.kind) }}</span>
            <b>{{ n.code }}</b> {{ n.name }}
            <span v-if="n.metric_value != null"
                  :class="['badge', n.metric_value > n.alarm_threshold ? 'bad' : 'muted']">
              {{ n.metric_value }}{{ n.metric_unit }} / 阈值 {{ n.alarm_threshold }}
            </span>
            <div v-if="n.affected_connector_codes?.length" class="muted" style="font-size:12px">
              隔离影响枪：<b :class="n.affected_connector_codes.length > 1 ? 'bad-txt' : ''">
                {{ n.affected_connector_codes.join('、') }}
              </b>
            </div>
          </div>
        </div>

        <div class="panel">
          <h2>⑥ 持证技师与备件批次</h2>
          <table>
            <thead><tr><th>技师</th><th>资质</th><th>班次</th></tr></thead>
            <tbody>
              <tr v-for="t in techs" :key="t.id">
                <td>{{ t.name }}</td>
                <td class="muted" style="font-size:12px">{{ t.certifications.join('、') }}</td>
                <td class="mono">{{ fmtHM(t.shift_start) }}–{{ fmtHM(t.shift_end) }}</td>
              </tr>
            </tbody>
          </table>
          <h3>备件</h3>
          <div v-for="p in parts" :key="p.id">
            <div><b>{{ p.name }}</b> <span class="muted mono">{{ p.sku }}</span></div>
            <span v-for="b in p.batches" :key="b.id" class="seg">
              {{ b.batch_no }} ·
              <span :class="b.quality_status==='RELEASED' ? 'ok-txt' : 'bad-txt'">{{ b.quality_status }}</span>
              · 可用 {{ b.quantity_available }}
            </span>
          </div>
        </div>

        <div class="panel">
          <h2>⑦ 已发通知（{{ notes.length }}）</h2>
          <ul class="timeline">
            <li v-for="n in notes" :key="n.id">
              <b>[{{ n.kind }}] {{ n.title }}</b>
              <div class="muted">{{ n.body }}</div>
            </li>
            <li v-if="!notes.length" class="muted">暂无通知</li>
          </ul>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, computed } from 'vue'
import {
  api, newIdempotencyKey, STATUS_LABELS, IMPACT_LABELS,
  WO_STATUS_LABELS, DEVICE_STATUS_LABELS, RISK_LABELS,
  SUGGESTION_LABELS, WO_KIND_LABELS,
} from '../api.js'

const compare = ref(null)
const board = ref(null)
const topology = ref(null)
const techs = ref([])
const parts = ref([])
const wos = ref([])
const notes = ref([])
const reservations = ref([])
const notice = ref(null)
const altMap = reactive({})
const accForm = reactive({})
const signName = reactive({})
const cancelId = ref(null)
const triageValue = ref(2.0)
const delay = reactive({ wo_id: 1, minutes: 25 })
const sched = reactive({ technician_id: 1, spare_batch_id: 1, cooldown_minutes: 5 })

const allBatches = computed(() => parts.value.flatMap((p) =>
  p.batches.map((b) => ({ ...b, sku: p.sku }))))

onMounted(loadAll)

async function loadAll() {
  const [t, p, w, n, r, b, topo] = await Promise.all([
    api.get('/api/technicians'),
    api.get('/api/spare-parts'),
    api.get('/api/work-orders'),
    api.get('/api/notifications'),
    api.get('/api/reservations'),
    api.get('/api/ops/board'),
    api.get('/api/topology'),
  ])
  techs.value = t
  parts.value = p
  wos.value = w
  notes.value = n
  reservations.value = r
  board.value = b
  topology.value = topo
  for (const wo of w) {
    if (!accForm[wo.id]) {
      accForm[wo.id] = {
        insulation_mohm: 80, load_test_kw: 120, load_test_passed: true,
        load_tested_at: '2026-11-27T18:03', final_reading_at: '2026-11-27T18:09',
      }
      signName[wo.id] = '王建国'
    }
  }
  if (!cancelId.value && r.length) cancelId.value = r[0].id
  if (!sched.technician_id && t.length) sched.technician_id = t[0].id
  await runCompare(true)
}

async function runCompare(silent) {
  try {
    compare.value = await api.post('/api/work-orders/compare')
  } catch (e) {
    if (!silent) showError(e)
  }
}

function showError(e) {
  notice.value = {
    kind: 'bad',
    text: e.message || '操作失败',
    violations: e.detail?.violations,
  }
}
function showOk(text) { notice.value = { kind: 'ok', text } }

async function mutate(fn, okText) {
  notice.value = null
  try {
    const out = await fn()
    if (okText) showOk(okText)
    await loadAll()
    return out
  } catch (e) {
    showError(e)
    return null
  }
}

function schedulePayload(optKey) {
  const base = {
    technician_id: sched.technician_id,
    spare_batch_id: sched.spare_batch_id,
    cooldown_minutes: sched.cooldown_minutes,
    idempotency_key: newIdempotencyKey(),
  }
  if (optKey === 'MERGED_1740') return { ...base, strategy: 'MERGED' }
  if (optKey === 'SEPARATE_B_ONLY') return { ...base, strategy: 'SEPARATE', scope: 'B_ONLY' }
  return { ...base, strategy: 'SEPARATE', scope: 'BOTH_TONIGHT' }
}

async function applyOption(opt) {
  const payload = schedulePayload(opt.option_key)
  const out = await mutate(
    () => api.post('/api/work-orders/schedule', payload),
    opt.feasible ? `已采纳 ${opt.title}` : '排程被拒绝（符合预期）',
  )
  if (!opt.feasible && out === null) {
    // 拒绝信息已由 mutate -> showError 展示 violations
  }
}

async function startWork(wo) {
  await mutate(() => api.post(`/api/work-orders/${wo.id}/start`,
    { idempotency_key: newIdempotencyKey() }), `${wo.ref} 已挂牌开工`)
}

function iso(v) { return v && v.length === 16 ? v + ':00' : v }

async function submitAcceptance(wo) {
  const f = accForm[wo.id]
  await mutate(() => api.post(`/api/work-orders/${wo.id}/acceptance`, {
    ...f,
    load_tested_at: iso(f.load_tested_at),
    final_reading_at: iso(f.final_reading_at),
    idempotency_key: newIdempotencyKey(),
  }), '验收读数已记录')
}

async function signoff(wo) {
  await mutate(() => api.post(`/api/work-orders/${wo.id}/signoff`, {
    signoff_by: signName[wo.id] || '值班主管',
    signed_at: '2026-11-27T18:10:00',
    idempotency_key: newIdempotencyKey(),
  }), `${wo.ref} 已签字并恢复`)
}

async function sendDelayReceipt() {
  await mutate(() => api.post('/api/receipts', {
    type: 'WORK_ORDER_DELAYED', work_order_id: delay.wo_id,
    delay_minutes: delay.minutes, reason: '现场回执：作业延期',
    idempotency_key: newIdempotencyKey(),
  }), '延期回执已处理，容量/通知已重算')
}

async function sendFailReceipt() {
  await mutate(() => api.post('/api/receipts', {
    type: 'ACCEPTANCE_FAILED_RECEIPT', work_order_id: 1,
    reason: '绝缘与负载测试失败', insulation_mohm: 8,
    load_test_kw: 40, load_test_passed: false,
    load_tested_at: '2026-11-27T18:20:00',
    final_reading_at: '2026-11-27T18:26:00',
    idempotency_key: newIdempotencyKey(),
  }), '测试失败回执已处理：B 保持停运')
}

async function sendPassReceipt() {
  await mutate(() => api.post('/api/work-orders/1/acceptance', {
    insulation_mohm: 80, load_test_kw: 120, load_test_passed: true,
    load_tested_at: '2026-11-27T18:03:00',
    final_reading_at: '2026-11-27T18:09:00',
    idempotency_key: newIdempotencyKey(),
  }), '合格读数已记录，可签字恢复')
}

async function sendCancelReceipt() {
  if (!cancelId.value) return
  await mutate(() => api.post('/api/receipts', {
    type: 'RESERVATION_CANCELLED', reservation_id: cancelId.value,
    idempotency_key: newIdempotencyKey(),
  }), '取消回执已处理')
}

async function triage(v) {
  triageValue.value = v
  await mutate(() => api.post('/api/work-orders/triage-reading', {
    connector_id: 2, value_mohm: v,
    observed_at: v < 5 ? '2026-11-27T17:20:00' : undefined,
    idempotency_key: newIdempotencyKey(),
  }), '绝缘研判完成')
}

async function loadAlt(r) {
  altMap[r.id] = await api.get(`/api/reservations/${r.id}/alternatives`)
}

async function confirmAlternative(r, s) {
  const code = s.connector_codes[0]
  const gun = board.value.connectors.find((c) => c.code === code)
  if (!gun) return
  await mutate(() => api.post('/api/operations/service-actions', {
    reservation_id: r.id, kind: 'ALTERNATIVE_CONNECTOR',
    target_connector_id: gun.id, note: `候选 ${code} 经主管确认改派`,
    idempotency_key: newIdempotencyKey(),
  }), `已显式确认改派到 ${code}`)
  altMap[r.id] = await api.get(`/api/reservations/${r.id}/alternatives`).catch(() => null)
}

function gunCode(id) {
  const c = board.value?.connectors.find((x) => x.id === id)
  return c ? c.code : '#' + id
}
function gunIdsToCodes(ids) { return (ids || []).map(gunCode) }
function fmt(t) { return t ? String(t).replace('T', ' ').slice(5, 16) : '—' }
function fmtHM(t) { return t ? String(t).slice(11, 16) : '—' }
function deviceOk(s) { return s === 'OPERATIONAL' }
function kindIcon(k) {
  return { PANEL: '▣', FEEDER: '┣', BREAKER: '⏻', ISOLATOR: '⌗', CONNECTOR: '🔌' }[k] || '•'
}
function statusBadge(s) {
  return {
    CONFIRMED: 'ok', ARRIVED: 'info', CHARGING: 'warn', COMPLETED: 'muted',
    CANCELLED: 'muted', NO_SHOW: 'muted',
    NEEDS_RESELECTION: 'bad', MANUAL_CONTACT: 'bad',
  }[s] || 'muted'
}
function impactBadge(d) {
  return { UNCHANGED: 'ok', NEEDS_RESELECTION: 'bad', MANUAL_CONTACT: 'warn', PROTECTED_CHARGING: 'warn' }[d] || 'muted'
}
function woStatusBadge(s) {
  return {
    PENDING: 'info', SCHEDULED: 'info', IN_PROGRESS: 'warn',
    PENDING_ACCEPTANCE: 'warn', ACCEPTANCE_FAILED: 'bad', COMPLETED: 'ok', CANCELLED: 'muted',
  }[s] || 'muted'
}
function riskBadge(l) { return { LOW: 'muted', MEDIUM: 'info', HIGH: 'warn', CRITICAL: 'bad' }[l] || 'muted' }
</script>

<style scoped>
.opt-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.opt-card { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; background: #fcfdfe; }
.opt-card.feasible { border-color: #9ed3b3; }
.opt-card.infeasible { border-color: #e3a39f; background: #fff8f7; }
.opt-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; margin-bottom: 8px; }
.checks { list-style: none; margin: 0 0 6px; padding: 0; font-size: 12.5px; }
.checks li { margin: 4px 0; }
.checks li.ok { color: var(--ok); }
.checks li.bad { color: var(--bad); }
.mini-board { margin-top: 8px; }
.ok-txt { color: var(--ok); }
.bad-txt { color: var(--bad); }
.danger-btn { background: var(--bad); border-color: var(--bad); }
.warn-btn { background: var(--warn); border-color: var(--warn); }
.topo-node { padding: 4px 8px; border-left: 2px solid var(--line); margin: 3px 0; }
</style>
