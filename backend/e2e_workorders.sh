#!/usr/bin/env bash
# Feature-2 题面场景 E2E：工单/拓扑/备件/持证技师、合并 vs 分开、三类回执与容量重算。
# 前置：数据库已重置为种子状态：cd backend && .venv/bin/python -m app.seed
# 用法：bash e2e_workorders.sh
set -u
BASE="${BASE:-http://127.0.0.1:8000}"
pass=0; fail=0

check() { # desc expected actual
  if [ "$2" = "$3" ]; then echo "  ✅ $1 ($3)"; pass=$((pass+1));
  else echo "  ❌ $1：期望 [$2] 实际 [$3]"; fail=$((fail+1)); fi
}
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
code_post() { curl -s -o /tmp/f2_body.json -w '%{http_code}' -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
assert_py() { # desc python-expr-on-stdin-json
  local desc="$1"; local expr="$2"
  if curl -s "${@:3}" 2>/dev/null | python3 -c 'import json,sys
expr=sys.argv[1]
d=json.load(sys.stdin)
assert eval(expr, {"d":d}), expr' "$expr" 2>/tmp/f2_pyerr; then
    echo "  ✅ $desc"; pass=$((pass+1))
  else
    echo "  ❌ $desc :: $(tail -1 /tmp/f2_pyerr)"; fail=$((fail+1))
  fi
}
assert_file_py() { # desc file python-expr-with-d
  local desc="$1"; local file="$2"; local expr="$3"
  if python3 -c 'import json,sys
expr=sys.argv[1]
d=json.load(open(sys.argv[2]))
assert eval(expr, {"d":d}), expr' "$expr" "$file" 2>/tmp/f2_pyerr; then
    echo "  ✅ $desc"; pass=$((pass+1))
  else
    echo "  ❌ $desc :: $(tail -1 /tmp/f2_pyerr)"; fail=$((fail+1))
  fi
}

echo "== 0. 健康检查 =="
check "health" "ok" "$(curl -s $BASE/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 1. 拓扑：BR-BC 隔离 B+C，ISO-B 只隔离 B =="
assert_py "BR-BC 影响 B、C" "set(next(n for n in d['nodes'] if n['code']=='BR-BC')['affected_connector_codes'])=={'B','C'}" "$BASE/api/topology"
assert_py "ISO-B 只影响 B" "next(n for n in d['nodes'] if n['code']=='ISO-B')['affected_connector_codes']==['B']" "$BASE/api/topology"

echo "== 2. 种子工单档案完整 =="
assert_py "两张主工单且证据/截止/步骤齐备" "len(d)>=2 and all(x['evidence'] and x['deadline_at'] and x['steps'] and x['isolation_scope'] for x in d if x['ref'] in ('WO-B-INS','WO-BC-BRKR'))" "$BASE/api/work-orders"

echo "== 3. 建立 18:00-18:30 的 B 90kW 单与 C 60kW 单 =="
B_BOOK=$(post /api/reservations '{"vehicle_id":4,"site_id":1,"start_at":"2026-11-27T18:00:00","duration_minutes":30,"min_power_kw":90,"mode":"CONNECTOR","preferred_connector_id":2,"idempotency_key":"e2ef2-bookb-0001"}')
BID=$(echo "$B_BOOK" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
CID=$(post /api/reservations '{"vehicle_id":2,"site_id":1,"start_at":"2026-11-27T18:00:00","duration_minutes":30,"min_power_kw":60,"mode":"CONNECTOR","preferred_connector_id":3,"idempotency_key":"e2ef2-bookc-0001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
check "B 单锁定 2 号枪" "2" "$(echo "$B_BOOK" | python3 -c 'import json,sys;print(json.load(sys.stdin)["connector_id"])')"

echo "== 4. 方案对比：合并冲击 B+C；分开保留 C；今晚连做断路器被班次拒绝 =="
CMP=$(post /api/work-orders/compare '{}')
echo "$CMP" > /tmp/f2_cmp.json
assert_file_py "合并方案判定 B 单 NEEDS_RESELECTION" /tmp/f2_cmp.json \
  "next(i['decision'] for o in d['options'] if o['option_key']=='MERGED_1740' for i in o['impacts'] if i['reservation_id']==$BID)=='NEEDS_RESELECTION'"
assert_file_py "合并方案同时冲击 B 与 C 两单" /tmp/f2_cmp.json \
  "sum(1 for i in next(o for o in d['options'] if o['option_key']=='MERGED_1740')['impacts'] if i['decision']=='NEEDS_RESELECTION')>=2"
assert_file_py "分开 B_ONLY 仅 B 单改选、C 单继续履约" /tmp/f2_cmp.json \
  "sum(1 for i in next(o for o in d['options'] if o['option_key']=='SEPARATE_B_ONLY')['impacts'] if i['decision']=='NEEDS_RESELECTION')==1 and next(i['decision'] for o in d['options'] if o['option_key']=='SEPARATE_B_ONLY' for i in o['impacts'] if i['reservation_id']==$CID)=='UNCHANGED'"
assert_file_py "分开今晚连做被班次+截止时点拒绝" /tmp/f2_cmp.json \
  "next(o for o in d['options'] if o['option_key']=='SEPARATE_BOTH_TONIGHT')['feasible'] is False and any(v['code']=='TECH_SHIFT' for v in next(o for o in d['options'] if o['option_key']=='SEPARATE_BOTH_TONIGHT')['violations'])"

echo "== 5. 不安全排程一律 422 =="
check "今晚连做断路器 422" "422" "$(code_post /api/work-orders/schedule '{"strategy":"SEPARATE","scope":"BOTH_TONIGHT","technician_id":1,"idempotency_key":"e2ef2-rej1-0001"}'; echo)"
check "无资质学徒 422" "422" "$(code_post /api/work-orders/schedule '{"strategy":"SEPARATE","scope":"B_ONLY","technician_id":2,"idempotency_key":"e2ef2-rej2-0001"}'; echo)"
check "冻结备件批次 422" "422" "$(code_post /api/work-orders/schedule '{"strategy":"SEPARATE","scope":"B_ONLY","technician_id":1,"spare_batch_id":2,"idempotency_key":"e2ef2-rej3-0001"}'; echo)"
check "冷却仅2分钟 422" "422" "$(code_post /api/work-orders/schedule '{"strategy":"SEPARATE","scope":"B_ONLY","technician_id":1,"cooldown_minutes":2,"idempotency_key":"e2ef2-rej4-0001"}'; echo)"
check "被拒后工单仍 PENDING" "PENDING" "$(curl -s $BASE/api/work-orders/1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"

echo "== 6. 采纳安全的分开·B_ONLY：200，合格备件扣到 0，候选建议全部未确认 =="
check "B_ONLY 排程 200" "200" "$(code_post /api/work-orders/schedule '{"strategy":"SEPARATE","scope":"B_ONLY","technician_id":1,"idempotency_key":"e2ef2-sched-0001"}'; echo)"
check "合格批次库存=0" "0" "$(curl -s $BASE/api/spare-parts | python3 -c 'import json,sys;d=json.load(sys.stdin);print([b["quantity_available"] for p in d for b in p["batches"] if b["id"]==1][0])')"
python3 -c "import json;d=json.load(open('/tmp/f2_body.json'));assert all(s['confirmed'] is False for a in d['alternatives'] for s in a['suggestions'])" \
  && { echo "  ✅ 所有替代建议 confirmed=false（候选不是已确认）"; pass=$((pass+1)); } || fail=$((fail+1))

echo "== 7. 延期 25 分钟：击穿班次与最迟时点 =="
check "延期回执 200" "200" "$(code_post /api/work-orders/1/delay '{"delay_minutes":25,"reason":"现场延期","idempotency_key":"e2ef2-delay-0001"}'; echo)"
check "产生 2 条安全冲突" "2" "$(python3 -c 'import json;print(len(json.load(open("/tmp/f2_body.json"))["breaches"]))')"

echo "== 8. 测试失败回执：B 验收失败、容量清空 =="
check "失败回执 200" "200" "$(code_post /api/receipts '{"type":"ACCEPTANCE_FAILED_RECEIPT","work_order_id":1,"reason":"绝缘仍低","insulation_mohm":8,"load_test_kw":40,"load_test_passed":false,"load_tested_at":"2026-11-27T18:20:00","final_reading_at":"2026-11-27T18:26:00","idempotency_key":"e2ef2-fail-0001"}'; echo)"
check "passed=false" "False" "$(python3 -c 'import json;print(json.load(open("/tmp/f2_body.json"))["passed"])')"
check "B 状态 ACCEPTANCE_FAILED" "ACCEPTANCE_FAILED" "$(curl -s $BASE/api/work-orders/1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')"
check "18:20-18:30 的 90kW 无可售枪" "0" "$(curl -s $BASE/api/ops/board | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len([c for r in d["matrix"] if r["slice"]=="18:20–18:30" and r["min_power_kw"]==90 for c in r["sellable_connector_codes"]]))')"

echo "== 9. 未通过验收禁止签字恢复 =="
check "失败状态签字 409" "409" "$(code_post /api/work-orders/1/signoff '{"signoff_by":"王建国","idempotency_key":"e2ef2-badsign-01"}'; echo)"

echo "== 10. 预约取消回执 =="
check "取消回执 200" "200" "$(code_post /api/receipts "{\"type\":\"RESERVATION_CANCELLED\",\"reservation_id\":$BID,\"idempotency_key\":\"e2ef2-cancel-001\"}"; echo)"
check "订单 CANCELLED" "CANCELLED" "$(python3 -c 'import json;print(json.load(open("/tmp/f2_body.json"))["status"])')"

echo "== 11. 通知留痕齐全 =="
for k in SCHEDULE_CONFIRMED SCHEDULE_BREACH WORK_DELAYED ACCEPTANCE_FAILED REWORK_CREATED CANCEL_RECEIPT; do
  assert_py "通知含 $k" "any(n['kind']=='$k' for n in d)" "$BASE/api/notifications"
done

echo "== 12. 复测合格 + 冷却合规 + 签字 → B 恢复 OPERATIONAL =="
check "合格验收 200" "200" "$(code_post /api/work-orders/1/acceptance '{"insulation_mohm":80,"load_test_kw":120,"load_test_passed":true,"load_tested_at":"2026-11-27T18:24:00","final_reading_at":"2026-11-27T18:29:00","idempotency_key":"e2ef2-accok-0001"}'; echo)"
check "签字恢复 200" "200" "$(code_post /api/work-orders/1/signoff '{"signoff_by":"王建国","signed_at":"2026-11-27T18:30:00","idempotency_key":"e2ef2-sign-0001"}'; echo)"
check "B 恢复 OPERATIONAL" "OPERATIONAL" "$(curl -s $BASE/api/ops/board | python3 -c 'import json,sys;d=json.load(sys.stdin);print(next(c["operational_status"] for c in d["connectors"] if c["code"]=="B"))')"

echo "== 13. 绝缘恶化到 2MΩ：强制停机并拦截 90kW 售卖 =="
check "研判 immediate=true" "True" "$(post /api/work-orders/triage-reading '{"connector_id":2,"value_mohm":2.0,"observed_at":"2026-11-27T17:20:00","idempotency_key":"e2ef2-triage-001"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["immediate_shutdown_required"])')"
check "强制停机后 B 单 409" "409" "$(code_post /api/reservations '{"vehicle_id":4,"site_id":1,"start_at":"2026-11-27T18:00:00","duration_minutes":10,"min_power_kw":90,"preferred_connector_id":2,"idempotency_key":"e2ef2-aftertrip-1"}'; echo)"

echo
echo "通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ]
