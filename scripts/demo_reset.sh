#!/usr/bin/env bash
# demo_reset.sh — pre-judging data hygiene for the live Veer demo (WP-15 T12).
#
# Puts the stack into the judged starting state WITHOUT deciding anything a
# human must decide. It stops at PENDING: a signed-in duty officer approves
# live on /ops (the user's decision), so this script never approves a
# sighting, a dispatch or an e-challan.
#
# Usage:
#   bash scripts/demo_reset.sh                              dry-run (default): GET-only
#                                                           (+ read-only SELECTs) — prints
#                                                           the inventory and the plan
#   bash scripts/demo_reset.sh --apply --officer '<name>'   the reset, as that named officer
#        [--restage]                                        re-stage the sweep even when the
#                                                           running one is still fresh
#        [--cancel-debris]                                  also cancel the LIVE acceptance
#                                                           debris on the board through C-F
#                                                           (see "Debris" below)
#   bash scripts/demo_reset.sh --purge-acceptance           list the acceptance-test rows a
#                                                           purge would delete (SELECT only);
#                                                           with VEER_PURGE_ACCEPTANCE_OK=yes
#                                                           it deletes them (ONE transaction)
#
# --apply (needs the user's go-ahead; signs in through scripts/deck_session.sh
# as --officer, so every write below is recorded under that name by the deck's
# verified x-veer-officer header):
#   0. verifies the stack: services answer, READY 11/11 on the live stack
#      (scripts/veer_up.sh --status), migrations 015-020 ledgered (020 = the
#      real-road geom_source), display geometry covers the network
#      (geometry_basis never 'schematic'), the operator gate is on, and the
#      Cesium ion mode when a token is configured (withheld from anonymous
#      clients, ion-photoreal for the signed-in officer — never printed);
#   1. stops every running emulator run (a living-city session is reported,
#      never stopped: VEER_DEMO is the user's switch) — except a staging sweep
#      that is still fresh (clock DEMO_SWEEP_CLOCK, a pending sighting since
#      it started, ≥ DEMO_MIN_KEEP_SEC left to run), which a second --apply
#      keeps (a no-op);
#   2. the user-chosen stolen-vehicle remediation (C-N): cancels the old
#      fine-based pursuit (DEMO_LEGACY_PURSUIT, 4508d86d…) through the audited
#      C-F lifecycle, and has the officer REJECT the pending penal
#      recommendations (e-challans) on the stolen plate — they are
#      unapprovable (409) because the registered owner is the theft victim.
#      The approved e-challan behind that pursuit cannot be withdrawn
#      (decisions are final): it stays as superseded audit history, and it is
#      never a pursuit basis again — /ops offers a stolen vehicle's pursuit
#      only from its approved R_STOLEN_SIGHTING and the oracle refuses an
#      e-challan basis (422);
#   3. with --cancel-debris: cancels each LIVE debris event (below) through
#      C-F as the officer (its open dispatches close with it), clears a
#      leftover journey exercise plate (dismisses its pending sightings,
#      clears its earmark), and dismisses the stolen plate's stale EMULATED
#      sightings from earlier runs (a showcase's) so its card counts only the
#      staging sweep's fresh ones;
#   4. stages fresh EMULATED sightings: starts watchlist_anpr_sweep (it ensures
#      the FIR earmark precondition itself) and waits, bounded, until a
#      PENDING R_STOLEN_SIGHTING of the stolen plate and its JR-EM patrol units
#      are in. The sweep keeps running (45 min at clock 1, then completes on
#      its own): the patrol units exist only while a run publishes them (C-A,
#      Valkey EX 30). A pending sighting awaits the HUMAN approval that opens
#      the new pursuit;
#   5. verifies the judged state (GET-only) — including what /ops offers the
#      duty officer for the stolen plate, derived with the deck's own rules:
#      ONE stolen-vehicle card with AUTHORISE PURSUIT (no sighting approved
#      yet) and a pursuit card with no basis until that human approval, never
#      an e-challan — and prints the judge checklist.
#
# Debris (acceptance / rehearsal rows; --apply never deletes a row):
#   * TEST-labelled events — metadata.label set or such a title: the live
#     smokes' ARGUS SMOKE TEST / FLEET SMOKE TEST / TIER-0 GATE TEST
#     movements, JOURNEY CHECK (automated) rows, ACCEPTANCE TEST rehearsals;
#   * the showcase's per-run residue — rows attributed to 'Scripted demo
#     officer (showcase script)' / "God's Eye Morning showcase", and the
#     events of earlier rath_yatra_monsoon_ambulance / watchlist_anpr_sweep
#     runs (never the staging sweep's own);
#   * the acceptance rows ('Acceptance Tester', ACCEPTANCE-/ACCTEST units).
# By default debris is left as it is (non-destructive): /ops hides terminal
# rows with its own filters (terminal events leave the board, terminal
# dispatches older than 30 min fold behind 'history (n)', the audit stream
# follows the selected live event). A LIVE debris event (a smoke's planning
# movement) stays on the board under its TEST pill until --cancel-debris;
# the verification says which rows are still visible and until when. Audit
# rows are never deleted — only --purge-acceptance with the user's explicit
# OK removes the acceptance-test rows.
#
# An approved R_STOLEN_SIGHTING on the stolen plate from BEFORE this staging
# (the showcase's scripted approval, an earlier rehearsal) is final: /ops then
# reads 'pursuit authorised by <who>' with DISMISS only, and its pursuit card
# opens from that approval — the judged human AUTHORISE is pre-empted. No
# reset can withdraw it: the verification FAILS and names it.
#
# Env (defaults are the LIVE argus stack, as in scripts/veer_env.sh — which is
# not sourced: it would export veer.env, the operator PIN included, into every
# child of this script):
#   DECK_URL / DECK_PORT (5273), ORACLE_URL / ORACLE_PORT (8091),
#   BRAIN_URL / BRAIN_PORT (8092), EMULATOR_URL / EMULATOR_PORT (8090),
#   POSTGIS_CONTAINER (vvip-argus-postgis), POSTGIS_USER, POSTGIS_DB,
#   VEER_LOG_DIR (veer.env's, else VVIP/.run-models/logs) — the log + JSON summary,
#   the PIN: VEER_OPERATOR_PIN, else VEER_OPERATOR_PIN_FILE, else veer.env,
#   DEMO_STOLEN_PLATE (GJ27EM9001), DEMO_LEGACY_PURSUIT (4508d86d-566b-438f-8414-b3d67fd7e6e4),
#   DEMO_SWEEP_USE_CASE (watchlist_anpr_sweep), DEMO_SWEEP_CLOCK (1),
#   DEMO_WAIT_SEC (420: the bound on the wait for the first stolen sighting),
#   DEMO_MAX_SIGHTING_AGE_SEC (600: "the newest sighting is under 10 min old"),
#   DEMO_MIN_KEEP_SEC (600: a running sweep is kept only with this much left to run).
#
# Exit: 0 done (dry-run: preflight answered) · 1 a step or a check failed ·
#       2 usage / preflight refusal · 3 purge listed but not confirmed.
#
# Security: every URL is an internal endpoint (env-overridable config constant,
# loopback default) — none comes from external input. The PIN, the session
# cookie and the ion token are never printed: the cookie is read from the jar
# into the request header only, and the signed-in /gods-eye/mode answer is
# reduced to its mode before anything is kept.

set -uo pipefail

VVIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    awk '/^# Usage:/ { p = 1 } /^# --apply \(needs/ { exit } p { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

APPLY=0 OFFICER="" RESTAGE=0 PURGE=0 CANCEL_DEBRIS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        --officer) OFFICER="${2:-}"; [ $# -gt 1 ] && shift ;;
        --officer=*) OFFICER="${1#--officer=}" ;;
        --restage) RESTAGE=1 ;;
        --cancel-debris) CANCEL_DEBRIS=1 ;;
        --purge-acceptance) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "demo_reset: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$PURGE" = 1 ] && { [ "$APPLY" = 1 ] || [ "$RESTAGE" = 1 ] || [ "$CANCEL_DEBRIS" = 1 ]; }; then
    echo "demo_reset: --purge-acceptance runs on its own (it is the only path that deletes rows)" >&2
    exit 2
fi
if [ "$APPLY" = 0 ] && { [ "$RESTAGE" = 1 ] || [ "$CANCEL_DEBRIS" = 1 ]; }; then
    echo "demo_reset: --restage and --cancel-debris act only with --apply (a dry-run writes nothing)" >&2
    exit 2
fi
if [ "$APPLY" = 1 ]; then
    # a named human: 2..64 characters, no control / bidi characters, not a
    # system actor — whitespace collapsed as the deck's sign-in does
    OFFICER="$(printf '%s' "$OFFICER" | python3 -c '
import re, sys
s = " ".join(sys.stdin.read().split())
bad = re.search(r"[\x00-\x1f\x7f‎‏‪-‮⁦-⁩]", s)
machine = re.fullmatch(r"(?i)operator|system|anonymous|unknown|none|null|.*\(system\)|event-emulator.*|convoy-brain.*|traffic-oracle.*|system:.*", s)
sys.stdout.write(s if 2 <= len(s) <= 64 and not bad and not machine else "")')"
    if [ -z "$OFFICER" ]; then
        echo "demo_reset: --apply needs --officer '<the named officer running the reset>' (2..64 characters, a person, not a system actor)" >&2
        exit 2
    fi
fi
[ "$APPLY" = 0 ] && [ -n "$OFFICER" ] && echo "demo_reset: --officer is used by --apply only (this is a dry-run)" >&2

# ── internal endpoints (env-overridable constants, loopback-only defaults) ──
DECK_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}"
ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"
BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8092}}"
EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"
PG_CONTAINER="${POSTGIS_CONTAINER:-vvip-argus-postgis}"
PG_USER="${POSTGIS_USER:-corridor_admin}"
PG_DB="${POSTGIS_DB:-corridor_db}"
DECK_URL="${DECK_URL%/}"

if [ -z "${VEER_LOG_DIR:-}" ]; then
    VEER_LOG_DIR="$(sed -n 's/^VEER_LOG_DIR=//p' "${VEER_ENV_FILE:-$HOME/.config/veer/veer.env}" 2>/dev/null | tail -n 1 | sed 's/^"\(.*\)"$/\1/')"
    VEER_LOG_DIR="${VEER_LOG_DIR:-$VVIP_DIR/.run-models/logs}"
fi
mkdir -p "$VEER_LOG_DIR" || { echo "demo_reset: cannot create VEER_LOG_DIR $VEER_LOG_DIR" >&2; exit 2; }
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$VEER_LOG_DIR/demo_reset-$STAMP.log"
SUMMARY_JSON="$VEER_LOG_DIR/demo_reset-$STAMP.json"
exec > >(tee -a "$LOG_FILE") 2>&1

MODE=dry-run
[ "$APPLY" = 1 ] && MODE=apply
[ "$PURGE" = 1 ] && MODE=purge-acceptance
echo "[demo-reset] $(date -Is) mode=$MODE deck=$DECK_URL oracle=$ORACLE_URL brain=$BRAIN_URL emulator=$EMULATOR_URL pg=$PG_CONTAINER"
echo "[demo-reset] log $LOG_FILE"

# The live stack (all defaults): READY 11/11 comes from veer_up.sh --status
# (read-only). A private stack has no model units or compose project of its
# own, so its services are judged by the checks below only.
VEER_STATUS=skipped
if [ "$DECK_URL" = "http://127.0.0.1:5273" ] && [ "$ORACLE_URL" = "http://127.0.0.1:8091" ] \
        && [ "$PG_CONTAINER" = vvip-argus-postgis ] && [ "$PURGE" = 0 ]; then
    echo "[demo-reset] live stack: scripts/veer_up.sh --status (read-only)"
    if bash "$VVIP_DIR/scripts/veer_up.sh" --status; then VEER_STATUS=ready; else VEER_STATUS=not-ready; fi
fi

DECK_GATE="" DECK_COOKIE_JAR=""
if [ "$APPLY" = 1 ]; then
    # shellcheck source=scripts/deck_session.sh
    . "$VVIP_DIR/scripts/deck_session.sh"
    gate="$(curl -s -m 8 "$DECK_URL/api/session" 2>/dev/null \
        | python3 -c 'import json,sys; print("on" if json.load(sys.stdin).get("gate") else "off")' 2>/dev/null)"
    if [ "$gate" != on ]; then
        echo "[demo-reset] REFUSED: the deck's operator gate is ${gate:-unreachable} — the judged deck runs with VEER_OPERATOR_PIN set" >&2
        exit 2
    fi
    if [ "$VEER_STATUS" = not-ready ]; then
        echo "[demo-reset] REFUSED: the live stack is not READY (scripts/veer_up.sh --status above)" >&2
        exit 2
    fi
    trap deck_signout EXIT          # revokes the session and removes the cookie jar, on every exit
    deck_signin "$DECK_URL" "$OFFICER" || exit 2
    echo "[demo-reset] signed in at $DECK_URL as the named officer '$OFFICER'"
fi

DR_APPLY="$APPLY" DR_PURGE="$PURGE" DR_RESTAGE="$RESTAGE" DR_CANCEL_DEBRIS="$CANCEL_DEBRIS" DR_OFFICER="$OFFICER" \
DR_DECK_URL="$DECK_URL" DR_ORACLE_URL="$ORACLE_URL" DR_BRAIN_URL="$BRAIN_URL" DR_EMULATOR_URL="$EMULATOR_URL" \
DR_PG_CONTAINER="$PG_CONTAINER" DR_PG_USER="$PG_USER" DR_PG_DB="$PG_DB" \
DR_VEER_STATUS="$VEER_STATUS" DR_SUMMARY_JSON="$SUMMARY_JSON" DR_LOG_FILE="$LOG_FILE" \
DR_COOKIE_JAR="${DECK_COOKIE_JAR:-}" DR_GATE="${DECK_GATE:-}" \
DR_STOLEN_PLATE="${DEMO_STOLEN_PLATE:-GJ27EM9001}" \
DR_LEGACY_PURSUIT="${DEMO_LEGACY_PURSUIT:-4508d86d-566b-438f-8414-b3d67fd7e6e4}" \
DR_SWEEP_USE_CASE="${DEMO_SWEEP_USE_CASE:-watchlist_anpr_sweep}" DR_SWEEP_CLOCK="${DEMO_SWEEP_CLOCK:-1}" \
DR_WAIT_SEC="${DEMO_WAIT_SEC:-420}" DR_MAX_SIGHTING_AGE_SEC="${DEMO_MAX_SIGHTING_AGE_SEC:-600}" \
DR_MIN_KEEP_SEC="${DEMO_MIN_KEEP_SEC:-600}" \
DR_PURGE_OK="${VEER_PURGE_ACCEPTANCE_OK:-}" \
python3 - <<'PYEOF'
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

E = os.environ
APPLY = E["DR_APPLY"] == "1"
PURGE = E["DR_PURGE"] == "1"
RESTAGE = E["DR_RESTAGE"] == "1"
CANCEL_DEBRIS = E["DR_CANCEL_DEBRIS"] == "1"
OFFICER = E["DR_OFFICER"]
DECK, ORACLE, BRAIN, EMU = (E[k].rstrip("/") for k in ("DR_DECK_URL", "DR_ORACLE_URL", "DR_BRAIN_URL", "DR_EMULATOR_URL"))
PLATE = E["DR_STOLEN_PLATE"].strip().upper()
LEGACY = E["DR_LEGACY_PURSUIT"].strip().lower()
SWEEP = E["DR_SWEEP_USE_CASE"]


def _num(key):
    try:
        value = float(E[key])
    except ValueError:
        print(f"[demo-reset] {key[3:]} must be a number (got {E[key]!r})", file=sys.stderr)
        sys.exit(2)
    if value <= 0:
        print(f"[demo-reset] {key[3:]} must be positive (got {E[key]!r})", file=sys.stderr)
        sys.exit(2)
    return value


CLOCK = _num("DR_SWEEP_CLOCK")
WAIT_SEC = _num("DR_WAIT_SEC")
MAX_AGE = _num("DR_MAX_SIGHTING_AGE_SEC")
MIN_KEEP = _num("DR_MIN_KEEP_SEC")
PSQL = ["docker", "exec", E["DR_PG_CONTAINER"], "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-U", E["DR_PG_USER"], "-d", E["DR_PG_DB"]]

PENAL = ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY")
STOLEN_RULE = "R_STOLEN_SIGHTING"
LIVE_EVENT = ("planning", "approved", "active")
CLOSED_EVENT = ("completed", "cancelled")
TERMINAL_DISPATCH = ("completed", "cancelled", "deferred")
HISTORY_AFTER_SEC = 30 * 60      # DispatchTiles folds older terminal dispatches
DECK_APPROVED_LIMIT = 50         # the deck reads GET /enforcement/recommendations?status=approved&limit=50
#: One officer approval authorises ONE pursuit (C6): the deck skips an approval
#: whose pursuit has CLOSED — a consumed authorisation, e.g. the scripted
#: showcase's (client.ts approvalSpent, used by DecisionQueue.svelte
#: stolenGroups and ops/store.svelte.ts pursuitCandidates; the oracle refuses to
#: reuse it with a 422 and lists each approval's `pursuit`).
DECK_SKIPS_CLOSED_PURSUIT_APPROVALS = True
BBOX = "min_lon=72.40&min_lat=22.90&max_lon=72.75&max_lat=23.20"   # Ahmedabad, all 49 segments
#: acceptance / rehearsal markers in free text (titles, unit ids, earmark reasons, texts)
DEBRIS_TEXT = re.compile(r"acceptance|acctest|smoke test|gate test|journey check|-JOURNEY-", re.I)
ACCEPTANCE_ACTORS = ("Acceptance Tester",)
SHOWCASE_ACTORS = ("Scripted demo officer (showcase script)", "God's Eye Morning showcase")
JOURNEY_ACTORS = ("Journey check (automated)",)
DEBRIS_ACTORS = ACCEPTANCE_ACTORS + SHOWCASE_ACTORS + JOURNEY_ACTORS
SHOWCASE_USE_CASES = ("rath_yatra_monsoon_ambulance", "watchlist_anpr_sweep")
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

summary = {"mode": "purge-acceptance" if PURGE else ("apply" if APPLY else "dry-run"),
           "officer": OFFICER if APPLY else None, "cancel_debris": CANCEL_DEBRIS, "started_at": None,
           "stack": {}, "inventory": {}, "plan": [], "actions": [], "checks": [], "writes": 0}
failures = 0


def now():
    return datetime.now(timezone.utc)


def ts(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_sec(value):
    t = ts(value)
    return None if t is None else (now() - t).total_seconds()


def ist(dt):
    return dt.astimezone(IST).strftime("%H:%M") if dt else "?"


def say(text=""):
    print(text, flush=True)


def step(title):
    say(f"\n[demo-reset] ── {title}")


def ok(text):
    say(f"  ok   {text}")
    summary["checks"].append({"ok": True, "text": text})


def bad(text):
    global failures
    failures += 1
    say(f"  FAIL {text}")
    summary["checks"].append({"ok": False, "text": text})


def warn(text):
    say(f"  WARN {text}")
    summary["checks"].append({"ok": None, "text": text})


def info(text):
    say(f"       {text}")


def check(cond, good, failed):
    ok(good) if cond else bad(failed)
    return cond


def save_summary():
    with open(E["DR_SUMMARY_JSON"], "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)


def _cookie():
    """The session cookie from deck_session.sh's curl jar (#HttpOnly_ lines
    included) — read into the request header, never printed."""
    jar = E.get("DR_COOKIE_JAR") or ""
    if E.get("DR_GATE") != "on" or not jar or not os.path.exists(jar):
        return None
    with open(jar, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 7 and parts[5] == "veer_operator":
                return f"veer_operator={parts[6]}"
    return None


COOKIE = _cookie() if APPLY else None


def http(method, url, body=None, timeout=30, cookie=None):
    """(status, json) — status 0 when unreachable. A dry-run is GET-only by
    construction: any other method raises before a byte is sent."""
    if method != "GET" and not APPLY:
        raise RuntimeError(f"dry-run refused a {method} to {url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    if cookie:
        req.add_header("cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = {"detail": raw.decode("utf-8", "replace")[:300]}
        return exc.code, doc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, {"detail": f"{type(exc).__name__}: {exc}"[:300]}


def get(url, timeout=30):
    return http("GET", url, timeout=timeout)


def write(path, body, timeout=60):
    """One write THROUGH THE DECK, as the signed-in officer (the deck sets the
    verified x-veer-officer header; the body names the same officer)."""
    status, doc = http("POST", DECK + path, body, timeout=timeout, cookie=COOKIE)
    summary["writes"] += 1
    summary["actions"].append({"POST": path, "status": status,
                               "detail": (doc or {}).get("detail") if isinstance(doc, dict) else None})
    return status, doc


def sql(query):
    """Read-only SELECT through the store container (None when docker is not reachable)."""
    try:
        out = subprocess.run(PSQL + ["-c", query], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [line.split("|") for line in out.stdout.splitlines() if line]


def detail(doc):
    if isinstance(doc, dict):
        d = doc.get("detail")
        return d if isinstance(d, str) else json.dumps(d)[:240]
    return str(doc)[:240]


# ════════════════════════════════════════════════════════════════════════════
# --purge-acceptance: the ONLY path that deletes rows (user's explicit OK)
# ════════════════════════════════════════════════════════════════════════════
PURGE_EVENTS = (
    "SELECT e.event_id FROM events.corridor_events e WHERE e.status IN ('completed', 'cancelled') AND ("
    "e.title ILIKE 'ACCEPTANCE TEST%' OR e.metadata->>'label' ILIKE '%acceptance%' OR EXISTS ("
    "SELECT 1 FROM events.event_status_log l WHERE l.event_id = e.event_id AND l.actor = 'Acceptance Tester'))"
)
PURGE_DISPATCHES = (
    "SELECT d.dispatch_id FROM corridor.dispatches d WHERE d.status IN ('completed', 'cancelled', 'deferred') AND ("
    "d.requested_by = 'Acceptance Tester' OR d.unit_id ILIKE 'ACCEPTANCE-%' OR d.unit_id ILIKE '%ACCTEST%')"
)

if PURGE:
    summary["started_at"] = now().isoformat()
    step("acceptance-test rows (terminal only — a live event or open dispatch is never touched)")
    rows = sql(
        "SELECT 'event', e.event_id, e.status, e.title FROM events.corridor_events e WHERE e.event_id IN ("
        + PURGE_EVENTS + ") UNION ALL SELECT 'dispatch', d.dispatch_id, d.status, d.unit_id || ' by ' || d.requested_by "
        "FROM corridor.dispatches d WHERE d.dispatch_id IN (" + PURGE_DISPATCHES + ") ORDER BY 1, 4")
    if rows is None:
        bad(f"the store is not reachable through docker ({E['DR_PG_CONTAINER']}) — nothing listed")
        save_summary()
        sys.exit(1)
    for kind, rid, status, label in rows:
        info(f"{kind:8} {rid} {status:9} {label[:90]}")
    summary["inventory"]["purge_rows"] = [{"kind": r[0], "id": r[1], "status": r[2], "label": r[3]} for r in rows]
    if not rows:
        ok("nothing to purge")
        save_summary()
        sys.exit(0)
    counts = sql(
        "SELECT (SELECT count(*) FROM events.event_status_log WHERE event_id IN (" + PURGE_EVENTS + ")), "
        "(SELECT count(*) FROM corridor.dispatch_events WHERE dispatch_id IN (" + PURGE_DISPATCHES + ") "
        "OR dispatch_id IN (SELECT dispatch_id FROM corridor.dispatches WHERE event_id IN (" + PURGE_EVENTS + ")))")
    n_log, n_devents = counts[0] if counts else ("?", "?")
    info(f"{len(rows)} rows; with them {n_log} event_status_log rows and {n_devents} dispatch_events rows "
         "(ON DELETE CASCADE: routes, phases, status log, the events' dispatches and their audit rows)")
    if E.get("DR_PURGE_OK") != "yes":
        warn("not deleted: purging audit history needs the user's explicit OK — re-run with VEER_PURGE_ACCEPTANCE_OK=yes")
        save_summary()
        sys.exit(3)
    out = subprocess.run(PSQL + ["--single-transaction",
                                 "-c", "DELETE FROM corridor.dispatches WHERE dispatch_id IN (" + PURGE_DISPATCHES + ")",
                                 "-c", "DELETE FROM events.corridor_events WHERE event_id IN (" + PURGE_EVENTS + ")"],
                         capture_output=True, text=True, timeout=60)
    summary["writes"] += 1
    if out.returncode != 0:
        bad(f"purge rolled back: {out.stderr.strip()[:300]}")
        save_summary()
        sys.exit(1)
    ok(f"purged in one transaction: {'; '.join(out.stdout.splitlines())} (user-approved: VEER_PURGE_ACCEPTANCE_OK=yes)")
    left = sql("SELECT (SELECT count(*) FROM (" + PURGE_EVENTS + ") a) + (SELECT count(*) FROM (" + PURGE_DISPATCHES + ") b)")
    check(bool(left) and left[0][0] == "0", "no acceptance-test row left", f"{left} acceptance-test rows left")
    save_summary()
    sys.exit(0 if failures == 0 else 1)


# ════════════════════════════════════════════════════════════════════════════
# 0. the stack (GET-only + read-only SELECTs)
# ════════════════════════════════════════════════════════════════════════════
summary["started_at"] = now().isoformat()
step("stack")
st, sess = get(DECK + "/api/session", timeout=8)
gate_on = bool(st == 200 and isinstance(sess, dict) and sess.get("gate"))
summary["stack"]["gate"] = "on" if gate_on else ("off" if st == 200 else "unreachable")
if st != 200:
    bad(f"deck {DECK}/api/session answered {st or 'nothing'}")
elif gate_on:
    ok("operator gate ON (VEER_OPERATOR_PIN): every proxied write needs a signed-in officer")
else:
    (bad if APPLY else warn)("operator gate OFF — the judged deck must run with VEER_OPERATOR_PIN")
# read anonymously first: with the gate on, a keyed deck says the upgrade is
# withheld — the token itself is not fetched by this read
st, mode = get(DECK + "/gods-eye/mode", timeout=8)
mode_name = (mode or {}).get("mode") if isinstance(mode, dict) else None
keyed = mode_name == "ion-photoreal" or "withheld" in str((mode or {}).get("note", "") if isinstance(mode, dict) else "")
mode = None
summary["stack"]["ion"] = mode_name
if st != 200:
    warn(f"/gods-eye/mode answered {st}")
elif keyed and mode_name == "ion-photoreal":
    ok("Cesium ion keyed: Google Photorealistic 3D tiles + world terrain + Bing imagery")
elif keyed:
    ok("Cesium ion keyed: withheld from anonymous clients (keyless OSM until an officer signs in)")
else:
    info("ion: not configured — keyless OpenStreetMap basemap + ellipsoid terrain (the documented fallback)")
if keyed and COOKIE:
    # the signed-in answer carries the ion token: only its mode is kept, the
    # document is dropped at once (the token is never printed or stored)
    st, doc = http("GET", DECK + "/gods-eye/mode", timeout=8, cookie=COOKIE)
    signed_mode = doc.get("mode") if isinstance(doc, dict) else None
    asset = doc.get("tiles3d_asset_id") if isinstance(doc, dict) else None
    doc = None
    summary["stack"]["ion_signed_in"] = signed_mode
    check(st == 200 and signed_mode == "ion-photoreal",
          f"the signed-in officer gets the ion upgrade (mode ion-photoreal, photoreal 3D asset {asset})",
          f"signed-in /gods-eye/mode answered {st} mode {signed_mode} — the judged canvas would stay keyless")

vs = E["DR_VEER_STATUS"]
if vs == "ready":
    ok("READY 11/11 (scripts/veer_up.sh --status)")
elif vs == "not-ready":
    bad("scripts/veer_up.sh --status: not READY (table above)")
else:
    info("private stack: READY judged by the service checks below (veer_up.sh --status covers the live stack)")

st, oh = get(ORACLE + "/health", timeout=10)
ing = (oh or {}).get("ingest", {}) if isinstance(oh, dict) else {}
check(st == 200 and (oh or {}).get("status") == "ok",
      f"oracle ok · ingest {'connected' if ing.get('connected') else 'DOWN'} · {ing.get('msgs_per_min')} msg/min",
      f"oracle /health {st}: {detail(oh)}")
st, bh = get(BRAIN + "/health", timeout=10)
check(st == 200, f"brain answers ({(bh or {}).get('status', '?') if isinstance(bh, dict) else '?'})",
      f"brain /health {st}: {detail(bh)}")
st, eh = get(EMU + "/health", timeout=10)
stack_ok = isinstance(eh, dict) and all((eh.get("stack") or {}).values()) and bool(eh.get("stack"))
check(st == 200 and stack_ok, f"emulator answers, every seam probed true (status {(eh or {}).get('status')}"
      + (f": {'; '.join(eh.get('degraded_reasons') or [])}" if isinstance(eh, dict) and eh.get("degraded_reasons") else "") + ")",
      f"emulator /health {st}: {detail(eh)}")
living = (eh or {}).get("living_city", {}) if isinstance(eh, dict) else {}

migs = sql("SELECT name FROM corridor.schema_migrations ORDER BY name")
if migs is None:
    warn(f"store not reachable through docker ({E['DR_PG_CONTAINER']}): migrations not checked")
else:
    names = [m[0] for m in migs]
    need = [f"{n:03d}" for n in range(15, 21)]
    missing = [n for n in need if not any(x.startswith(n + "_") for x in names)]
    summary["stack"]["migrations"] = len(names)
    check(not missing, f"migrations {len(names)} ledgered, 015-020 applied (020_road_network_osm: real-road geom_source)",
          f"migrations missing: {missing}")
st, segs = get(f"{ORACLE}/api/v1/spatial/segments?{BBOX}", timeout=20)
feats = (segs or {}).get("segments", []) if isinstance(segs, dict) else []
basis = {}
for f in feats:
    b = f.get("geometry_basis") or "?"
    basis[b] = basis.get(b, 0) + 1
total = sql("SELECT count(*) FROM corridor.road_segments")
n_total = int(total[0][0]) if total else len(feats)
summary["stack"]["geometry_basis"] = basis
check(st == 200 and feats and basis.get("schematic", 0) == 0 and len(feats) == n_total,
      f"display geometry covers the network: {len(feats)}/{n_total} segments, basis {basis}",
      f"geometry: {len(feats)}/{n_total} segments served, basis {basis} (status {st})")
preflight_failures = failures

# ════════════════════════════════════════════════════════════════════════════
# 1. inventory (GET-only)
# ════════════════════════════════════════════════════════════════════════════
step("inventory")


def _list(url, key):
    _, doc = get(url)
    return (doc or {}).get(key, []) if isinstance(doc, dict) else []


def load():
    return {
        "runs": _list(EMU + "/api/v1/emulator/runs?limit=50", "runs"),
        "events": _list(ORACLE + "/api/v1/events?limit=500", "events"),
        "vehicles": _list(ORACLE + "/api/v1/vehicles?earmark=flagged", "vehicles"),
        "pending": _list(ORACLE + "/api/v1/enforcement/recommendations?status=pending&limit=500", "recommendations"),
        "approved": _list(ORACLE + "/api/v1/enforcement/recommendations?status=approved&limit=500", "recommendations"),
        "units": _list(ORACLE + "/api/v1/units", "units"),
        "dispatches": _list(ORACLE + "/api/v1/dispatches?limit=200", "dispatches"),
        "feed": _list(ORACLE + "/api/v1/city/feed?limit=100", "items"),
    }


def meta_of(row):
    m = row.get("metadata")
    return m if isinstance(m, dict) else {}


def running_runs(inv):
    return [r for r in inv["runs"] if r.get("status") == "running"]


def run_spec(r):
    return r.get("spec") if isinstance(r.get("spec"), dict) else {}


def run_use_case(r):
    return run_spec(r).get("use_case_id") or r.get("use_case_id")


def run_mode(r):
    return run_spec(r).get("mode", "use_case")


def run_started(r):
    return ts(r.get("started_at") or r.get("t0_anchor") or run_spec(r).get("t0_anchor") or r.get("created_at"))


def run_scale(r):
    try:
        return float(r.get("clock_scale") or run_spec(r).get("clock_scale") or 1.0)
    except (TypeError, ValueError):
        return 1.0


_, _ucs = get(EMU + "/api/v1/emulator/use-cases")
HORIZON_MIN = {u.get("id"): u.get("horizon_min") for u in (_ucs if isinstance(_ucs, list) else []) if isinstance(u, dict)}


def run_end(r):
    """When a use-case run completes on its own (t0 + horizon / clock)."""
    st0, horizon = run_started(r), HORIZON_MIN.get(run_use_case(r))
    if st0 is None or not horizon:
        return None
    return st0 + timedelta(seconds=float(horizon) * 60 / max(run_scale(r), 1e-6))


def stolen_vehicle(inv):
    for v in inv["vehicles"]:
        if (v.get("plate") or "").upper() == PLATE:
            return v
    return None


def recs_of(recs, vid, rules):
    return [r for r in recs if r.get("vehicle_id") == vid and r.get("rule_id") in rules]


def unit_kind(u):
    if str(u.get("unit_id", "")).startswith("JR-EM"):
        return "jr-em"
    return "dispatched" if u.get("dispatch_id") else "patrol"


# ── acceptance debris ───────────────────────────────────────────────────────
def event_debris(e, keep_runs=()):
    """Why a corridor event is acceptance debris (None when it is not). The
    legacy pursuit is the C-N remediation's, and the staging sweep's own
    event is the judged state — neither is debris."""
    if str(e.get("event_id")) == LEGACY:
        return None
    meta = meta_of(e)
    label = str(meta.get("label") or "").strip()
    if label:
        return f"TEST label '{label[:40]}'"
    if DEBRIS_TEXT.search(str(e.get("title") or "")):
        return "TEST title"
    if meta.get("opened_by") in DEBRIS_ACTORS:
        return f"opened by '{meta['opened_by']}'"
    if (meta.get("source") == "event-emulator" and meta.get("use_case_id") in SHOWCASE_USE_CASES
            and meta.get("mode") != "living_city" and str(meta.get("run_id")) not in {str(k) for k in keep_runs}):
        return f"an earlier {meta.get('use_case_id')} run ({meta.get('run_id') or 'unnumbered'})"
    return None


def dispatch_debris(d, debris_event_ids):
    if d.get("event_id") in debris_event_ids:
        return f"on debris event {str(d.get('event_id'))[:8]}"
    actor = next((a for a in (d.get("requested_by"), d.get("decided_by")) if a in DEBRIS_ACTORS), None)
    if actor:
        return f"'{actor}'"
    if DEBRIS_TEXT.search(str(d.get("unit_id") or "")):
        return "TEST unit"
    return None


def debris_vehicles(inv):
    """FLAGGED exercise plates other than the demo plate (a journey check's
    leftover): /ops would show their stolen-vehicle and pursuit cards."""
    return [v for v in inv["vehicles"] if (v.get("plate") or "").upper() != PLATE
            and DEBRIS_TEXT.search(str(v.get("earmark_reason") or ""))]


def ops_visible_debris(inv, keep_runs=()):
    """What /ops would show of the debris through its own filters: the board
    (live events), the decision queue (pending), the dispatch tiles (open, or
    terminal within 30 min) and the audit stream's default view (the selected
    live event). The city-scope ledger is history (dated)."""
    out = []
    debris_ids = {e.get("event_id") for e in inv["events"] if event_debris(e, keep_runs)}
    for e in inv["events"]:
        why = event_debris(e, keep_runs)
        if why and e.get("status") in LIVE_EVENT:
            out.append({"where": "board", "id": e["event_id"], "fold_at": None, "event": e,
                        "text": f"board: {e['event_id'][:8]} {e.get('status')} '{(e.get('title') or '')[:56]}' — {why}"})
    for r in inv["pending"]:
        if DEBRIS_TEXT.search(json.dumps([r.get("recommendation"), r.get("evidence"), r.get("audit_tail")])):
            out.append({"where": "queue", "id": r["recommendation_id"], "fold_at": None,
                        "text": f"queue: {r['recommendation_id'][:8]} {r.get('rule_id')}"})
    for v in debris_vehicles(inv):
        out.append({"where": "queue", "id": v.get("vehicle_id"), "fold_at": None, "vehicle": v,
                    "text": f"queue: exercise plate {v.get('plate')} still FLAGGED ({str(v.get('earmark_reason'))[:60]})"})
    for d in inv["dispatches"]:
        why = dispatch_debris(d, debris_ids)
        if not why:
            continue
        fold_at = None
        if d.get("status") in TERMINAL_DISPATCH:
            t = ts(d.get("updated_at") or d.get("created_at"))
            if t is None or (now() - t).total_seconds() > HISTORY_AFTER_SEC:
                continue
            fold_at = t + timedelta(seconds=HISTORY_AFTER_SEC)
        out.append({"where": "tiles", "id": d.get("dispatch_id"), "fold_at": fold_at,
                    "text": f"tiles: {d.get('unit_id')} {d.get('status')} — {why}"
                            + (f" (folds into 'history (n)' at {ist(fold_at)} IST)" if fold_at else "")})
    return out


# ── what /ops offers for the stolen plate (C-N) ─────────────────────────────
def ops_offer(inv, veh):
    """What /ops offers the duty officer for the stolen plate, derived with the
    deck's own rules (services/command-deck/src/lib/ops):
      * DecisionQueue.svelte stolenGroups — ONE card over the plate's PENDING
        R_STOLEN_SIGHTINGs; once ANY R_STOLEN_SIGHTING of the vehicle is
        APPROVED (the deck reads ?status=approved&limit=50, newest first) the
        card reads 'pursuit authorised by <decided_by>' and offers DISMISS
        only — otherwise AUTHORISE PURSUIT (approve the newest) + DISMISS;
      * store.svelte.ts pursuitCandidates — a FLAGGED vehicle with a latest
        sighting and no open pursuit; it opens from the newest (decided_at)
        APPROVED recommendation of the vehicle, and for a vehicle reported
        stolen (stolen_vehicle_fir) from an R_STOLEN_SIGHTING only, never an
        e-challan (C-N; the oracle refuses that basis with a 422 too)."""
    vid = (veh or {}).get("vehicle_id")
    closed_basis = {str(meta_of(e).get("recommendation_id")) for e in inv["events"]
                    if e.get("event_kind") == "vehicle_pursuit" and e.get("status") in CLOSED_EVENT}

    def counts(a):
        return not (DECK_SKIPS_CLOSED_PURSUIT_APPROVALS and str(a.get("recommendation_id")) in closed_basis)

    approved = [a for a in inv["approved"][:DECK_APPROVED_LIMIT] if a.get("vehicle_id") == vid and counts(a)]
    stolen = (veh or {}).get("lawful_basis") == "stolen_vehicle_fir"
    pool = [a for a in approved if not stolen or a.get("rule_id") == STOLEN_RULE]
    basis = max(pool, key=lambda a: ts(a.get("decided_at")) or ts(a.get("created_at")) or EPOCH, default=None)
    open_pursuits = [e for e in inv["events"] if e.get("event_kind") == "vehicle_pursuit"
                     and e.get("status") in LIVE_EVENT and meta_of(e).get("vehicle_id") == vid]
    return {
        "pending": recs_of(inv["pending"], vid, (STOLEN_RULE,)) if vid else [],
        "card_approved": next((a for a in approved if a.get("rule_id") == STOLEN_RULE), None),
        "pursuit_card": bool(veh) and veh.get("earmark_state", "flagged") == "flagged"
        and bool(veh.get("last_sighting")) and not open_pursuits,
        "basis": basis,
        "never_basis": [a for a in approved if stolen and a.get("rule_id") != STOLEN_RULE],
        "open_pursuits": open_pursuits,
        "closed_basis": closed_basis,
    }


def preempting_text(a, offer):
    consumed = str(a.get("recommendation_id")) in offer["closed_basis"]
    return (f"{a.get('rule_id')} {str(a.get('recommendation_id'))[:8]} approved by '{a.get('decided_by')}' at "
            f"{ist(ts(a.get('decided_at')))} IST" + (" (its pursuit already closed)" if consumed else ""))


PREEMPT_REMEDY = ("decisions are final, so no reset can withdraw it: the deck must skip an approval whose pursuit "
                  "has closed (then flip DECK_SKIPS_CLOSED_PURSUIT_APPROVALS here), or the scripted showcase runs on "
                  "this plate only after judging")

inv = load()
veh = stolen_vehicle(inv)
vid = veh.get("vehicle_id") if veh else None
running = running_runs(inv)
legacy = next((e for e in inv["events"] if e.get("event_id") == LEGACY), None)
pending_penal = recs_of(inv["pending"], vid, PENAL) if vid else []
pending_stolen = recs_of(inv["pending"], vid, (STOLEN_RULE,)) if vid else []
approved_penal = recs_of(inv["approved"], vid, PENAL) if vid else []
jr = [u for u in inv["units"] if unit_kind(u) in ("jr-em", "dispatched")]
last_t = ((veh or {}).get("last_sighting") or {}).get("t")
debris_all = sum(1 for d in inv["dispatches"] if dispatch_debris(d, set()))
debris_feed = sum(1 for i in inv["feed"] if DEBRIS_TEXT.search(json.dumps([i.get("text"), i.get("actor")]))
                  or i.get("actor") in DEBRIS_ACTORS)
running_ids = {str(r["run_id"]) for r in running}

# the staging sweep a second --apply keeps: still fresh, at the configured
# clock, with enough of its run left (a showcase's fast sweep never qualifies)
keep, keep_why_not = None, []
sweeps = [r for r in running if run_use_case(r) == SWEEP and run_mode(r) != "living_city"]
if sweeps and not RESTAGE and vid and len(sweeps) == 1:
    s = sweeps[0]
    started = run_started(s)
    fresh_pending = [r for r in pending_stolen if started and ts(r.get("created_at")) and ts(r["created_at"]) >= started]
    sight_age = age_sec(last_t)
    end = run_end(s)
    left = (end - now()).total_seconds() if end else None
    keep_why_not = [w for w, cond in (
        (f"clock {run_scale(s):g} ≠ {CLOCK:g}", abs(run_scale(s) - CLOCK) > 1e-6),
        (f"{left:.0f} s left to run (< {MIN_KEEP:.0f} s)" if left is not None else "its end is unknown",
         left is None or left < MIN_KEEP),
        ("no pending sighting since it started", not fresh_pending),
        ("its newest sighting is stale", sight_age is None or sight_age >= 0.8 * MAX_AGE),
    ) if cond]
    if not keep_why_not:
        keep = s
    keep_why_not = [f"running sweep run {s['run_id']} is not kept: {'; '.join(keep_why_not)}"] if keep_why_not else []
keep_runs = {keep["run_id"]} if keep else set()
# EMULATED sightings of the plate from EARLIER runs (a showcase's, a stale sweep's): the deck groups them
# into the plate's ONE card ('N sightings on file') next to the staging sweep's fresh ones
keep_from = run_started(keep) if keep else None
stale_sightings = [r for r in pending_stolen if "emulat" in str((r.get("evidence") or {}).get("source", "")).lower()
                   and (keep_from is None or (ts(r.get("created_at")) or EPOCH) < keep_from)]

info(f"emulator runs running: {[(r['run_id'], run_use_case(r), f'clock {run_scale(r):g}') for r in running] or 'none'}"
     f" · living city {'ON' if living.get('enabled') else 'off'} ({living.get('status', '?')})")
if legacy:
    info(f"legacy pursuit {LEGACY[:8]}: {legacy.get('status')} — '{legacy.get('title', '')[:70]}'"
         f" (basis recommendation {str(meta_of(legacy).get('recommendation_id', '?'))[:8]})")
else:
    info(f"legacy pursuit {LEGACY[:8]}: not in this store — nothing to cancel")
if veh:
    info(f"stolen plate {PLATE}: vehicle {vid[:8]} · earmark {veh.get('earmark_state')}/{veh.get('lawful_basis')}"
         f" · last sighting {last_t or 'none'}"
         + (f" ({age_sec(last_t) / 60:.0f} min ago, {(veh.get('last_sighting') or {}).get('source')})" if last_t else ""))
else:
    info(f"stolen plate {PLATE}: no active earmark yet (the sweep ensures the FIR precondition itself)")
info(f"recommendations on the plate: pending penal {len(pending_penal)} · pending {STOLEN_RULE} {len(pending_stolen)}"
     f" · approved penal {len(approved_penal)} (decisions are final: history, never a pursuit basis — C-N)")
offer0 = ops_offer(inv, veh)
for a in [a for a in inv["approved"] if vid and a.get("vehicle_id") == vid and a.get("rule_id") == STOLEN_RULE]:
    info(f"approved {STOLEN_RULE} on the plate: {preempting_text(a, offer0)}")
info(f"units on the canvas: {len(inv['units'])} ({len(jr)} JR-EM/dispatched)")
live_debris = [(e, event_debris(e, keep_runs)) for e in inv["events"]
               if e.get("status") in LIVE_EVENT and event_debris(e, keep_runs)]
info(f"acceptance debris: {len(live_debris)} live event(s) on the board, {debris_all} dispatch rows, "
     f"{debris_feed} of the newest {len(inv['feed'])} city-feed rows")
visible = ops_visible_debris(inv, keep_runs)
info(f"visible on /ops through its filters: {len(visible)}")
for v in visible[:20]:
    info(f"  visible → {v['text']}")
if len(visible) > 20:
    info(f"  … and {len(visible) - 20} more")
summary["inventory"] = {
    "running_runs": [{"run_id": r["run_id"], "use_case_id": run_use_case(r), "mode": run_mode(r),
                      "clock_scale": run_scale(r)} for r in running],
    "living_city": living, "legacy_pursuit": {"event_id": LEGACY, "status": (legacy or {}).get("status")},
    "stolen_vehicle": {"plate": PLATE, "vehicle_id": vid, "last_sighting_t": last_t},
    "pending_penal": [r["recommendation_id"] for r in pending_penal],
    "pending_stolen_sightings": len(pending_stolen),
    "approved_penal": [r["recommendation_id"] for r in approved_penal],
    "approved_stolen_sightings": [{"id": a["recommendation_id"], "decided_by": a.get("decided_by"),
                                   "decided_at": a.get("decided_at")}
                                  for a in inv["approved"] if vid and a.get("vehicle_id") == vid
                                  and a.get("rule_id") == STOLEN_RULE],
    "units": len(inv["units"]), "debris_live_events": [{"event_id": e["event_id"], "why": w} for e, w in live_debris],
    "debris_dispatches": debris_all, "debris_feed_rows": debris_feed,
    "debris_visible_on_ops": [v["text"] for v in visible],
}

# ════════════════════════════════════════════════════════════════════════════
# 2. the plan
# ════════════════════════════════════════════════════════════════════════════
step("plan")
for w in keep_why_not:
    info(w)
plan = []
for r in running:
    if keep is not None and r["run_id"] == keep["run_id"]:
        plan.append(f"keep staging sweep run {r['run_id']} (fresh: pending sighting, last read {age_sec(last_t):.0f} s ago,"
                    f" completes {ist(run_end(r))} IST)")
    elif run_mode(r) == "living_city":
        plan.append(f"leave living-city session run {r['run_id']} (VEER_DEMO is the user's switch)")
    else:
        plan.append(f"stop run {r['run_id']} ({run_use_case(r)})")
if legacy and legacy.get("status") in LIVE_EVENT:
    plan.append(f"cancel pursuit {LEGACY[:8]} via C-F (planning → cancelled), officer '{OFFICER or '<--officer>'}'")
for r in pending_penal:
    plan.append(f"reject penal {r['rule_id']} {r['recommendation_id'][:8]} on {PLATE} (e-challan withheld — vehicle reported stolen)")
for e, why in live_debris:
    run_id = meta_of(e).get("run_id")
    if meta_of(e).get("source") == "event-emulator" and str(run_id) in running_ids:
        plan.append(f"debris {e['event_id'][:8]} closes when its run {run_id} stops ({why})")
    elif CANCEL_DEBRIS:
        plan.append(f"cancel debris {e['event_id'][:8]} {e.get('status')} via C-F ({why})")
    else:
        plan.append(f"leave debris {e['event_id'][:8]} {e.get('status')} on the board under its TEST pill ({why})"
                    " — --cancel-debris cancels it via C-F")
for v in debris_vehicles(inv):
    plan.append(("dismiss the pending sightings of, and clear the earmark on" if CANCEL_DEBRIS else "leave FLAGGED")
                + f" exercise plate {v.get('plate')}" + ("" if CANCEL_DEBRIS else " — --cancel-debris clears it"))
if stale_sightings:
    plan.append((f"dismiss {len(stale_sightings)} stale EMULATED {STOLEN_RULE} of {PLATE} from earlier runs (the staging "
                 "sweep files fresh ones)") if CANCEL_DEBRIS else
                (f"leave {len(stale_sightings)} stale EMULATED {STOLEN_RULE} of {PLATE} from earlier runs in its card's "
                 "count — --cancel-debris dismisses them"))
if keep is None:
    plan.append(f"start {SWEEP} (clock {CLOCK:g}) and wait ≤{WAIT_SEC:.0f} s for a PENDING {STOLEN_RULE} + JR-EM units")
plan.append("verify the judged state (GET-only) and print the judge checklist — nothing is approved")
for p in plan:
    info(f"• {p}")
summary["plan"] = plan
pre = offer0["card_approved"] or offer0["basis"]
if pre:
    warn(f"the judged AUTHORISE PURSUIT is pre-empted: {preempting_text(pre, offer0)} — /ops reads 'pursuit "
         f"authorised by {pre.get('decided_by')}' with DISMISS only and its pursuit card opens from that approval; "
         + PREEMPT_REMEDY)

if not APPLY:
    save_summary()
    step("dry-run: nothing was written (every request above was a GET; SQL was SELECT-only)")
    sys.exit(0 if failures == 0 else 1)

if preflight_failures:
    step(f"REFUSED: {preflight_failures} preflight check(s) failed — nothing written")
    save_summary()
    sys.exit(2)
if COOKIE is None:
    step("REFUSED: no operator session cookie after the sign-in — nothing written")
    save_summary()
    sys.exit(2)

# ════════════════════════════════════════════════════════════════════════════
# 3. apply (every write through the deck, as the signed-in officer)
# ════════════════════════════════════════════════════════════════════════════
step(f"apply as '{OFFICER}'")
stopped = set()
for r in running:
    if keep is not None and r["run_id"] == keep["run_id"]:
        ok(f"kept staging sweep run {r['run_id']} — still fresh (a second --apply is a no-op; --restage forces)")
        continue
    if run_mode(r) == "living_city":
        warn(f"living city run {r['run_id']} left running (VEER_DEMO is the user's switch)")
        continue
    st, doc = write(f"/api/emulator/api/v1/emulator/runs/{r['run_id']}/stop", {})
    if check(st == 200, f"stopped run {r['run_id']} ({run_use_case(r)}) → {(doc or {}).get('status')}",
             f"stop run {r['run_id']}: {st} {detail(doc)}"):
        stopped.add(str(r["run_id"]))

fir = None
if legacy and legacy.get("status") in LIVE_EVENT:
    meta = meta_of(legacy)
    basis_id = str(meta.get("recommendation_id") or "")
    basis_rule = next((r.get("rule_id") for r in inv["approved"] if r.get("recommendation_id") == basis_id), None)
    fir = meta.get("fir")
    note = (f"superseded (C-N): opened from e-challan {basis_id[:8]} ({basis_rule or 'penal rule'}) against a "
            f"vehicle reported stolen ({fir or 'FIR on record'}) — a stolen vehicle's pursuit is authorised by a "
            "sighting, never by a fine. Re-staged by demo_reset: a fresh R_STOLEN_SIGHTING awaits a duty "
            "officer's approval; the old e-challan stays as audit history")[:500]
    st, doc = write(f"/api/v1/events/{LEGACY}/status", {"to_status": "cancelled", "officer": OFFICER, "note": note})
    check(st == 200 and (doc or {}).get("to_status") == "cancelled",
          f"pursuit {LEGACY[:8]} cancelled via C-F ({(doc or {}).get('from_status')} → cancelled, actor "
          f"'{(doc or {}).get('actor')}', {(doc or {}).get('actor_source')})",
          f"cancel pursuit {LEGACY[:8]}: {st} {detail(doc)}")
elif legacy:
    ok(f"pursuit {LEGACY[:8]} already {legacy.get('status')} — nothing to cancel")

m = re.search(r"\bFIR\b[^;()]*", fir or (veh or {}).get("earmark_reason") or "")
fir_text = re.split(r"\s+names\s+", m.group(0).strip())[0].strip(" ,—-") if m else "FIR on record"
for r in pending_penal:
    note = (f"e-challan withheld — vehicle reported stolen ({fir_text}): the registered owner is the theft "
            "victim (C-N); dismissed before judging")[:500]
    st, doc = write(f"/api/v1/enforcement/recommendations/{r['recommendation_id']}/decision",
                    {"decision": "reject", "officer": OFFICER, "note": note})
    check(st == 200 and (doc or {}).get("status") == "rejected",
          f"rejected {r['rule_id']} {r['recommendation_id'][:8]} (decided_by '{(doc or {}).get('decided_by')}')",
          f"reject {r['recommendation_id'][:8]}: {st} {detail(doc)}")

if CANCEL_DEBRIS:
    # a stopped run's own event is completed by the emulator: give it a moment
    deadline = time.monotonic() + 20
    events = inv["events"]
    while True:
        events = _list(ORACLE + "/api/v1/events?limit=500", "events") or events
        lagging = [e for e in events if e.get("status") in LIVE_EVENT and meta_of(e).get("source") == "event-emulator"
                   and str(meta_of(e).get("run_id")) in stopped]
        if not lagging or time.monotonic() > deadline:
            break
        time.sleep(2)
    for e in events:
        why = event_debris(e, keep_runs)
        if not why or e.get("status") not in LIVE_EVENT:
            continue
        run_id = str(meta_of(e).get("run_id"))
        if meta_of(e).get("source") == "event-emulator" and run_id in running_ids and run_id not in stopped:
            continue    # its run is still running (a failed stop is FAILed above): the run owns its event
        note = (f"acceptance debris ({why}) — cancelled before judging by demo_reset as the named officer "
                "(C-F); the row and its audit history stay")[:500]
        st, doc = write(f"/api/v1/events/{e['event_id']}/status", {"to_status": "cancelled", "officer": OFFICER, "note": note})
        if st == 409 and isinstance(doc, dict) and doc.get("from_status") in CLOSED_EVENT:
            ok(f"debris {e['event_id'][:8]} already {doc.get('from_status')} — nothing to cancel")
            continue
        closed_n = len((doc or {}).get("closed_dispatches") or []) if isinstance(doc, dict) else 0
        check(st == 200 and (doc or {}).get("to_status") == "cancelled",
              f"debris {e['event_id'][:8]} cancelled via C-F ({(doc or {}).get('from_status')} → cancelled; {why}"
              + (f"; {closed_n} open dispatch(es) closed with it" if closed_n else "") + ")",
              f"cancel debris {e['event_id'][:8]}: {st} {detail(doc)}")
    for v in debris_vehicles(inv):
        for r in [r for r in inv["pending"] if r.get("vehicle_id") == v.get("vehicle_id")]:
            st, doc = write(f"/api/v1/enforcement/recommendations/{r['recommendation_id']}/decision",
                            {"decision": "reject", "officer": OFFICER,
                             "note": "exercise plate (journey check leftover) — dismissed before judging"})
            check(st == 200 or (st == 409 and "already" in detail(doc)),
                  f"dismissed {r.get('rule_id')} {r['recommendation_id'][:8]} on exercise plate {v.get('plate')}",
                  f"dismiss {r['recommendation_id'][:8]}: {st} {detail(doc)}")
        st, doc = write(f"/api/v1/vehicles/{v.get('vehicle_id')}/earmark/clear", {"officer": OFFICER})
        check(st == 200 or st == 404, f"cleared the earmark on exercise plate {v.get('plate')}",
              f"clear the earmark on {v.get('plate')}: {st} {detail(doc)}")
    n_ok = 0
    for r in stale_sightings:
        st, doc = write(f"/api/v1/enforcement/recommendations/{r['recommendation_id']}/decision",
                        {"decision": "reject", "officer": OFFICER,
                         "note": "stale EMULATED sighting from an earlier exercise run — dismissed before judging; "
                                 "the staging sweep's fresh sightings await the duty officer"})
        if st == 200 or (st == 409 and "already" in detail(doc)):
            n_ok += 1
        else:
            bad(f"dismiss stale sighting {r['recommendation_id'][:8]}: {st} {detail(doc)}")
    if stale_sightings:
        info(f"dismissed {n_ok}/{len(stale_sightings)} stale EMULATED {STOLEN_RULE} of {PLATE} from earlier runs")

staged = keep
if keep is None:
    st, doc = write("/api/emulator/api/v1/emulator/runs", {"use_case_id": SWEEP, "clock_scale": CLOCK})
    if st == 202 and isinstance(doc, dict) and doc.get("run_id") is not None:
        staged = {"run_id": doc["run_id"], "started_at": doc.get("started_at") or doc.get("t0_anchor")}
        ok(f"staged {SWEEP} run {doc['run_id']} (clock {CLOCK:g}, EMULATED sightings + JR-EM patrols)")
    else:
        bad(f"start {SWEEP}: {st} {detail(doc)}"
            + (f" (already running as run {doc.get('run_id')})" if st == 409 and isinstance(doc, dict) else ""))

if staged is not None and keep is None:
    started = ts(staged.get("started_at")) or now()
    deadline = time.monotonic() + WAIT_SEC
    last_note = 0.0
    seen_pending = seen_units = False
    jr_now = []
    while time.monotonic() < deadline:
        _, run_doc = get(f"{EMU}/api/v1/emulator/runs/{staged['run_id']}?include_tape=false")
        run_doc = run_doc if isinstance(run_doc, dict) else {}
        if run_doc.get("status") not in (None, "running"):
            bad(f"run {staged['run_id']} ended {run_doc.get('status')}: {run_spec(run_doc).get('reason')}")
            break
        v = next((x for x in _list(ORACLE + "/api/v1/vehicles?earmark=flagged", "vehicles")
                  if (x.get("plate") or "").upper() == PLATE), None)
        vid = v["vehicle_id"] if v else vid
        fresh = [r for r in _list(ORACLE + "/api/v1/enforcement/recommendations?status=pending&limit=500", "recommendations")
                 if r.get("vehicle_id") == vid and r.get("rule_id") == STOLEN_RULE
                 and (ts(r.get("created_at")) or started) >= started]
        # this run's patrols (a stopped run's frames linger up to 30 s in Valkey)
        jr_now = [u for u in _list(ORACLE + "/api/v1/units", "units") if str(u.get("unit_id", "")).startswith("JR-EM")
                  and str(u.get("run_id")) == str(staged["run_id"])]
        seen_pending, seen_units = bool(fresh), bool(jr_now)
        if seen_pending and seen_units:
            ok(f"run {staged['run_id']}: PENDING {STOLEN_RULE} on {PLATE} ({len(fresh)}) + {len(jr_now)} JR-EM units "
               f"after {(now() - started).total_seconds():.0f} s")
            break
        if time.monotonic() - last_note > 30:
            p = run_doc.get("progress") or {}
            info(f"waiting: event {run_doc.get('event_status')} · frames {p.get('delivered')}/{p.get('total')} · "
                 f"pending sighting {'yes' if seen_pending else 'not yet'} · JR-EM units {len(jr_now)}")
            last_note = time.monotonic()
        time.sleep(5)
    else:
        bad(f"no PENDING {STOLEN_RULE} + JR-EM units within {WAIT_SEC:.0f} s (pending {seen_pending}, units {seen_units})")

# ════════════════════════════════════════════════════════════════════════════
# 4. the judged state (GET-only)
# ════════════════════════════════════════════════════════════════════════════
step("verify the judged state")
inv = load()
veh = stolen_vehicle(inv)
vid = veh.get("vehicle_id") if veh else vid
running = running_runs(inv)
staged_id = staged["run_id"] if staged else None
stale = [r for r in running if r["run_id"] != staged_id and run_mode(r) != "living_city"]
check(not stale, f"emulator: 0 stale runs running (running now: "
      f"{[(r['run_id'], run_use_case(r)) for r in running] or 'none'} — the staging sweep is EMULATED and ends on its own)",
      f"stale runs still running: {[(r['run_id'], run_use_case(r)) for r in stale]}")
last_t = ((veh or {}).get("last_sighting") or {}).get("t")
a = age_sec(last_t)
check(a is not None and a < MAX_AGE, f"newest sighting of {PLATE}: {a:.0f} s old (< {MAX_AGE:.0f} s, "
      f"{((veh or {}).get('last_sighting') or {}).get('source')})" if a is not None else "",
      f"newest sighting of {PLATE}: {last_t or 'none'} (needs < {MAX_AGE:.0f} s)")
units_ok = [u for u in inv["units"] if unit_kind(u) in ("jr-em", "dispatched")]
check(units_ok, f"/api/v1/units: {len(units_ok)} JR-EM/dispatched unit(s) "
      f"({', '.join(sorted({u['unit_id'] for u in units_ok}))}; emulated: "
      f"{all(u.get('emulated') for u in units_ok)})", "/api/v1/units: no JR-EM or dispatched unit")
pend_stolen = recs_of(inv["pending"], vid, (STOLEN_RULE,)) if vid else []
check(pend_stolen, f"{len(pend_stolen)} PENDING {STOLEN_RULE} on {PLATE} await the duty officer's decision "
      "(evidence, never a fine)", f"no PENDING {STOLEN_RULE} on {PLATE}")
pend_penal = recs_of(inv["pending"], vid, PENAL) if vid else []
check(not pend_penal, f"0 pending penal recommendations on {PLATE} (withheld: rejected by the named officer)",
      f"{len(pend_penal)} penal recommendations still pending on {PLATE}")
legacy = next((e for e in inv["events"] if e.get("event_id") == LEGACY), None)
if legacy:
    _, audit = get(f"{ORACLE}/api/v1/events/{LEGACY}/audit")
    rows = (audit or {}).get("items", []) if isinstance(audit, dict) else []
    cancel = next((i for i in rows if "cancelled" in str(i.get("text", ""))), None)
    check(legacy.get("status") == "cancelled" and cancel is not None,
          f"pursuit {LEGACY[:8]} cancelled via C-F — audit: '{str(cancel.get('text', ''))[:110]}' "
          f"(actor {cancel.get('actor')})" if cancel else "",
          f"pursuit {LEGACY[:8]} is {legacy.get('status')} (no C-F cancellation in its audit)")
    basis_id = str(meta_of(legacy).get("recommendation_id") or "")
    basis = next((r for r in inv["approved"] if r.get("recommendation_id") == basis_id), None)
    if basis:
        ok(f"its basis {basis['rule_id']} {basis_id[:8]} stays APPROVED as superseded audit history "
           f"(decided {basis.get('decided_at', '')[:16]} by {basis.get('decided_by')}) — decisions are final")

offer = ops_offer(inv, veh)
if offer["pending"]:
    pre = offer["card_approved"]
    check(pre is None,
          f"/ops stolen-vehicle card for {PLATE}: AUTHORISE PURSUIT + DISMISS over {len(offer['pending'])} sighting(s) "
          "— no sighting is approved yet: the duty officer decides",
          f"/ops stolen-vehicle card reads 'pursuit authorised by {(pre or {}).get('decided_by')}' and offers DISMISS "
          f"only — {preempting_text(pre, offer) if pre else ''}; " + PREEMPT_REMEDY)
if offer["open_pursuits"]:
    bad(f"a pursuit is open on {PLATE}: {[(e['event_id'][:8], e.get('status'), meta_of(e).get('opened_by')) for e in offer['open_pursuits']]}"
        " — the board shows it and the pursuit card stays hidden; close it on /ops (or --cancel-debris when it is debris)")
elif offer["pursuit_card"]:
    b = offer["basis"]
    never = ", ".join(f"{x['rule_id']} {x['recommendation_id'][:8]}" for x in offer["never_basis"])
    check(b is None,
          f"/ops pursuit card for {PLATE} has no basis until that approval — never an e-challan"
          + (f" ({never}: history, skipped by the card and refused by the oracle — C-N)" if never else " (C-N)"),
          f"/ops pursuit card would OPEN PURSUIT from {preempting_text(b, offer) if b else ''} — before the duty "
          "officer decides; " + PREEMPT_REMEDY)
else:
    warn(f"no /ops pursuit card for {PLATE} yet (needs a FLAGGED earmark and a latest sighting)")

visible = ops_visible_debris(inv, {staged_id} if staged_id is not None else set())
if not visible:
    ok("no acceptance debris visible on /ops through its filters (board: live events only · queue: pending only · "
       "tiles: terminal > 30 min folded into 'history (n)' · audit: the selected live event)")
else:
    hints = []
    if any(v["where"] == "board" for v in visible) and not CANCEL_DEBRIS:
        hints.append("--apply --cancel-debris cancels the live ones via C-F (everything else is a no-op)")
    folds = [v["fold_at"] for v in visible if v["fold_at"]]
    if folds:
        hints.append(f"the terminal tiles fold into 'history (n)' by {ist(max(folds))} IST — re-run --apply after "
                     "that to re-verify")
    bad(f"acceptance debris visible on /ops ({len(visible)}): {'; '.join(v['text'] for v in visible[:8])}"
        + (f"; … {len(visible) - 8} more" if len(visible) > 8 else "") + (f" — {'; '.join(hints)}" if hints else ""))
debris_feed = sum(1 for i in inv["feed"] if DEBRIS_TEXT.search(json.dumps([i.get("text"), i.get("actor")]))
                  or i.get("actor") in DEBRIS_ACTORS)
if debris_feed:
    info(f"the CITY scope of the audit stream still lists {debris_feed} dated acceptance/showcase rows among the newest "
         f"{len(inv['feed'])} (the ledger is never edited; --purge-acceptance needs the user's explicit OK)")
live = [e for e in inv["events"] if e.get("status") in LIVE_EVENT]
info(f"live on the board: {[(e['event_id'][:8], e.get('event_kind'), e.get('status')) for e in live] or 'none'}")

# ════════════════════════════════════════════════════════════════════════════
# 5. the judge checklist
# ════════════════════════════════════════════════════════════════════════════
run_end_at = None
if staged_id is not None:
    _, rd = get(f"{EMU}/api/v1/emulator/runs/{staged_id}?include_tape=false")
    run_end_at = run_end(rd) if isinstance(rd, dict) else None
old = ", ".join(f"{r['recommendation_id'][:8]}" for r in recs_of(inv["approved"], vid, PENAL)) if vid else ""
step("judge checklist (a signed-in HUMAN decides every step)")
for line in [
    f"1. On the GB10 browser open {DECK}/ops and sign in as the duty officer (name + PIN).",
    "2. Decision queue: ONE card 'Stolen vehicle sighted — e-challan withheld (vehicle reported stolen, FIR …)' for",
    f"   {PLATE} — NO FINE · EVIDENCE ONLY, EMULATED sightings. AUTHORISE PURSUIT approves the newest sighting:",
    "   the human approval, never a fine.",
    "3. The pursuit card then names that approval as its basis; OPEN PURSUIT opens the pursuit from it — never from",
    f"   an e-challan{f' (the old {old} is history)' if old else ''}: the card skips it and the oracle refuses it (422, C-N).",
    "   The interception plan ranks choke junctions.",
    "4. Dispatch tiles: Jan Rakshak → 'dispatch to choke #1' → REQUEST → APPROVE → EN ROUTE: the unit moves on the",
    "   canvas (EMULATED projection). The JR-EM sweep patrols are already on the canvas.",
    "5. No fine for a reported-stolen vehicle: the card reads 'e-challan withheld' · NO FINE · EVIDENCE ONLY, and",
    f"   the oracle refuses any e-challan approval on {PLATE} (409 'e-challan withheld — vehicle reported stolen').",
    "6. COMPLETE the pursuit: its open dispatches close with it and the unit is released.",
    "7. Escort: the AI only PROPOSES route switches / Plan B / holds — the officer approves every switch;",
    "   a proposal nobody answers expires and the convoy holds its approved plan.",
    "8. /citizen in EN / HI / GU: emulated events read SIMULATED FEED · EMULATED; plates stay masked unless an",
    "   officer is signed in; vehicles only, via ANPR — never faces or persons.",
    "   The sweep's cameras read the stolen plate over its first ~15 min: its newest sighting stays under",
    "   10 min old for ~25 min after staging — stage shortly before judging (a later --apply re-stages).",
    f"9. After judging: stop the staging sweep (run {staged_id}) from the emulator panel"
    + (f", or it completes on its own at {ist(run_end_at)} IST." if run_end_at else "."),
]:
    say(f"  {line}")
summary["staged_run_id"] = staged_id
summary["finished_at"] = now().isoformat()
summary["failures"] = failures
save_summary()
step(f"{'DONE' if failures == 0 else f'{failures} check(s) FAILED'} — {summary['writes']} write(s) as '{OFFICER}'; "
     f"summary {E['DR_SUMMARY_JSON']}")
sys.exit(0 if failures == 0 else 1)
PYEOF
rc=$?
exit "$rc"
