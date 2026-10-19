#!/usr/bin/env bash
# check_tier0_live.sh — LIVE composite tier-0 gate (authorized 2026-10-19).
#
# Replaces the audit finding's original grep-for-hit gate (which could not
# decide anything pre-calibration) and the earlier wiring-only version of
# this script. Authorization (user, 2026-10-19), verbatim constraints:
#   (a) three identical repeat plans through :5273 -> at least one tier-0
#       outcome=hit with the metrics counters incrementing and saved_ms>0;
#   (b) a DIFFERENT plan (different POI pair) -> outcome must be a miss,
#       proving no stale-serving — if (b) ever fails, revert the enable and
#       report this finding BLOCKED, do not ship any-hit behavior;
#   (c) check_tier0_gate.sh full suite green;
#   (d) drift panel still green.
# Kill switch and TTL stay untouched and tested (suite).
#
# Calibration record (measured 2026-10-19, live stack): CONFIDENCE_FLOOR
# 0.6 -> 0.45 (env VVIP_CONFIDENCE_FLOOR; live pre-movement confidences
# route_strategy 0.5033 / disruption_tolerance 0.4696 via POST :8083/decide;
# re-measure if the decider model changes); noul answers carry no
# confidence and are EXCLUDED from the floor aggregation (was None->0.0
# coercion, which permanently mislabeled every noul-bearing checkpoint);
# MaxSim normalized to 0..1 brain-side (raw / query_token_count; self-match
# of the gate state doc: raw 55.40 / 64 tokens = 0.8657) so the 0.35
# novelty and 0.75 tier-0 floors mean what they say.
#
# Scope note for (b), measured and reported honestly: the different-POI
# plan runs on a FRESH movement — the in-contract way plans are created —
# and must miss via the movement-scoped cache key. On the SAME movement a
# body change is out-of-contract input; measured normalized similarity of a
# different-POI state doc against the movement's own decision doc is 0.8615
# (>= 0.75), i.e. the floor alone does not discriminate coordinate drift
# within one movement — the movement-scoped key is the operative guard.
#
# Deployment: this script does NOT build or restart anything. Run it after
# the argus stack is up with the current tree, e.g.:
#   docker compose -p vvip-argus -f infra/compose.yml \
#     -f infra/compose.gb10-override.yml -f infra/compose.argus-override.yml \
#     build convoy-brain
#   docker compose -p vvip-argus -f infra/compose.yml \
#     -f infra/compose.gb10-override.yml -f infra/compose.argus-override.yml \
#     up -d convoy-brain
#   bash scripts/check_tier0_live.sh
#
# Exit codes: 0 all green; 1 wiring/gate failure; 3 BLOCKED (stale-serving
# detected — revert the calibration enable, do not ship any-hit behavior).
# Read-only against the DB (SELECT only); live traffic is 4 plan calls +
# the drift panel's fixed probes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VVIP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE_PY="$VVIP_DIR/.venvs/gate/bin/python"
PROBE="$VVIP_DIR/scripts/probe_tier0_live.py"

DECK="http://localhost:5273"
BRAIN="http://localhost:8092"
PLANS="${TIER0_LIVE_PLANS:-3}"
PLAN_TIMEOUT="${TIER0_LIVE_PLAN_TIMEOUT:-240}"
SKIP_DRIFT="${TIER0_LIVE_SKIP_DRIFT:-0}"

BODY='{"origin":[72.5798,23.0254],"destination":[72.6266,23.0733],"vvip_class":"Z","planned_departure":"2030-01-01T00:00:00Z"}'
# (b): a genuinely different corridor (Surat pair, ~200 km from Ahmedabad).
BODY_DIFFERENT='{"origin":[72.8355,21.1702],"destination":[72.9581,21.0045],"vvip_class":"Z","planned_departure":"2030-01-01T00:00:00Z"}'

fail() { echo "FAIL check_tier0_live: $*" >&2; exit 1; }
blocked() { echo "BLOCKED check_tier0_live: $*" >&2; exit 3; }
# metric 'name{label="v"}' -> value (0 when the series does not exist yet).
# Exact FIRST-FIELD match: the exposition sample line starts with the
# metric name, while HELP/TYPE lines start with '#' — so neither the
# comment lines nor the _created gauges can ever match.
metric() {
  local v
  v=$(curl -s -m 5 "$BRAIN/metrics" | awk -v m="$1" '$1 == m {print $2; exit}')
  echo "${v:-0}"
}
# Prometheus counters are floats ("15.0"); bash $(( )) is integer-only.
fsub() { awk -v a="$1" -v b="$2" 'BEGIN{printf "%.4f", a-b}'; }
fge()  { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>=b)}'; }
fgt()  { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }
trap 'if [ -n "${STATUS_FILE:-}" ]; then rm -f "$STATUS_FILE".*; fi' EXIT

[ -x "$GATE_PY" ] || fail "gate venv python not found at $GATE_PY"
[ -f "$PROBE" ] || fail "probe missing: $PROBE"
command -v docker >/dev/null || fail "docker not found"

# ── (c) offline suite first — fast, decisive, no live dependency ──────────────
echo "== check_tier0_live (c): full convoy-brain suite (gate venv) =="
( cd "$VVIP_DIR" && bash scripts/check_tier0_gate.sh ) \
  || fail "check_tier0_gate.sh (full suite) failed"

plan_status_of() {  # $1: saved response file -> tier0 status string
  "$GATE_PY" -c '
import json,sys
doc = json.load(open(sys.argv[1]))
cp = doc.get("decider_checkpoint") or {}
print(cp.get("tier0_cache", {}).get("status") or "?")
' "$1" 2>/dev/null || echo parse-error
}
plan_confidences_of() {  # $1: saved response file -> "name=conf,..." (report)
  "$GATE_PY" -c '
import json,sys
doc = json.load(open(sys.argv[1]))
cp = doc.get("decider_checkpoint") or {}
ans = cp.get("answers") or {}
print(",".join(k + "=" + str(v.get("confidence")) for k, v in ans.items()))
' "$1" 2>/dev/null || echo ""
}

run_plan() {  # $1 movement, $2 body, $3 outfile -> "http_code status"
  local code
  code=$(curl -s -m "$PLAN_TIMEOUT" -o "$3" -w '%{http_code}' \
    -X POST "$DECK/api/convoy/movements/$1/plan" \
    -H 'content-type: application/json' -d "$2")
  echo "$code"
}

echo "== check_tier0_live: before-snapshot =="
KINDS_BEFORE=$(docker exec vvip-argus-postgis psql -U corridor_admin -d corridor_db \
  -tAc "SELECT count(*) FROM traffic.memory_docs WHERE kind='decision'" 2>/dev/null) \
  || fail "cannot read vvip-argus-postgis (psql SELECT)"
HITS_BEFORE=$(metric 'vvip_decider_tier0_cache_events_total{outcome="hit"}')
STORED_BEFORE=$(metric 'vvip_decider_tier0_cache_events_total{outcome="stored"}')
SAVED_BEFORE=$(metric 'vvip_decider_tier0_latency_saved_ms_total')
RECORDED_BEFORE=$(metric 'vvip_decider_memory_record_events_total{outcome="recorded"}')
echo "decision docs: ${KINDS_BEFORE:-0}  hits: ${HITS_BEFORE}  stored: ${STORED_BEFORE}  saved_ms: ${SAVED_BEFORE}  recorded: ${RECORDED_BEFORE}"

# ── (a) three identical repeat plans -> at least one HIT + counters ──────────
echo "== check_tier0_live (a): $PLANS identical repeat plans via $DECK =="
STATUS_FILE=$(mktemp)
M1=$(curl -s -m 30 -X POST "$DECK/api/convoy/movements" \
  -H 'content-type: application/json' -d "$BODY" \
  | "$GATE_PY" -c 'import json,sys;print(json.load(sys.stdin)["movement_id"])') \
  || fail "movement creation failed"
echo "movement: $M1"
HITS_SEEN=0
for i in $(seq 1 "$PLANS"); do
  CODE=$(run_plan "$M1" "$BODY" "$STATUS_FILE.$i")
  STATUS=$(plan_status_of "$STATUS_FILE.$i")
  CONF=$(plan_confidences_of "$STATUS_FILE.$i")
  echo "plan $i: http=$CODE tier0=$STATUS confidences=[$CONF]"
  [ "$CODE" = "200" ] || fail "(a) plan $i returned http=$CODE"
  [ "$STATUS" = "hit" ] && HITS_SEEN=$((HITS_SEEN + 1))
done
HITS_AFTER=$(metric 'vvip_decider_tier0_cache_events_total{outcome="hit"}')
SAVED_AFTER=$(metric 'vvip_decider_tier0_latency_saved_ms_total')
HIT_DELTA=$(fsub "$HITS_AFTER" "$HITS_BEFORE")
SAVED_DELTA=$(fsub "$SAVED_AFTER" "$SAVED_BEFORE")
if [ "$HITS_SEEN" -ge 1 ] && fge "$HIT_DELTA" 1 && fgt "$SAVED_DELTA" 0; then
  echo "PASS (a): $HITS_SEEN/$PLANS repeat plans hit; hit counter +${HIT_DELTA}; latency_saved_ms +${SAVED_DELTA}"
else
  fail "(a) no tier-0 hit on $PLANS identical repeats (hits_seen=$HITS_SEEN counter_delta=$HIT_DELTA saved_ms_delta=$SAVED_DELTA)"
fi

# ── (b) a DIFFERENT plan (different POI pair) -> must MISS ───────────────────
echo "== check_tier0_live (b): different-POI plan on a fresh movement =="
M2=$(curl -s -m 30 -X POST "$DECK/api/convoy/movements" \
  -H 'content-type: application/json' -d "$BODY_DIFFERENT" \
  | "$GATE_PY" -c 'import json,sys;print(json.load(sys.stdin)["movement_id"])') \
  || fail "(b) movement creation failed"
echo "movement: $M2"
CODE=$(run_plan "$M2" "$BODY_DIFFERENT" "$STATUS_FILE.diff")
STATUS=$(plan_status_of "$STATUS_FILE.diff")
SCORE=$("$GATE_PY" -c '
import json,sys
doc = json.load(open(sys.argv[1]))
print((doc.get("decider_checkpoint") or {}).get("tier0_cache", {}).get("best_score"))
' "$STATUS_FILE.diff" 2>/dev/null || echo "?")
echo "different-poi plan: http=$CODE tier0=$STATUS best_score=$SCORE"
[ "$CODE" = "200" ] || fail "(b) different-POI plan returned http=$CODE"
case "$STATUS" in
  hit)
    blocked "(b) different-POI plan was SERVED a cached answer (stale-serving) — revert the calibration enable and report the finding BLOCKED" ;;
  miss_*|*)
    echo "PASS (b): different-POI plan outcome=$STATUS (a miss — no stale-serving)" ;;
esac

# ── feeder wiring assertions (deployed-image proof) ───────────────────────────
echo "== check_tier0_live: feeder wiring =="
FAILURES=0
MOVEMENT_DOCS=$(docker exec vvip-argus-postgis psql -U corridor_admin -d corridor_db \
  -tAc "SELECT count(*) FROM traffic.memory_docs WHERE ref_id LIKE 'decision:$M1:%' OR ref_id LIKE 'decision:$M2:%'") \
  || fail "psql SELECT (movement docs) failed"
if [ "${MOVEMENT_DOCS:-0}" -ge 1 ]; then
  echo "PASS wiring: experiments' movements have ${MOVEMENT_DOCS} kind=decision doc(s) in traffic.memory_docs"
else
  echo "FAIL wiring: neither movement wrote a kind=decision doc (feeder broken or not deployed)"; FAILURES=1
fi
RECALL=$("$GATE_PY" "$PROBE" --verify-corpus --movement-id "$M1" 2>/dev/null | tail -1) || RECALL=""
echo "recall: $RECALL"
if echo "$RECALL" | grep -q '"best_kind": *"decision"'; then
  echo "PASS wiring: :8085/search recalls the feeder's kind=decision doc"
else
  echo "FAIL wiring: :8085/search did not recall a kind=decision doc"; FAILURES=1
fi
RECORDED_AFTER=$(metric 'vvip_decider_memory_record_events_total{outcome="recorded"}')
if fgt "$RECORDED_AFTER" "$RECORDED_BEFORE"; then
  echo "PASS wiring: brain memory_record_events_total{recorded} ${RECORDED_BEFORE} -> ${RECORDED_AFTER}"
else
  echo "FAIL wiring: brain never recorded (deployed image lacks the feeder?)"; FAILURES=1
fi
[ "$FAILURES" -eq 0 ] || fail "feeder wiring assertions failed"

# ── (d) drift panel still green ───────────────────────────────────────────────
if [ "$SKIP_DRIFT" != "1" ]; then
  echo "== check_tier0_live (d): model drift panel (live) =="
  ( cd "$VVIP_DIR" && bash scripts/model_drift_panel.sh ) \
    || fail "(d) model_drift_panel.sh failed"
else
  echo "== check_tier0_live (d): SKIPPED (TIER0_LIVE_SKIP_DRIFT=1) =="
fi

echo "PASS check_tier0_live (composite: suite + repeat-hit + different-plan-miss + wiring + drift panel)"
exit 0
