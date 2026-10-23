# shellcheck shell=bash
# deck_session.sh — operator sign-in for the deck's PIN gate (VEER_OPERATOR_PIN), shared by every script that
# calls a gated deck route. SOURCE it:
#     . "$VEER_ROOT/scripts/deck_session.sh"
#     deck_signin http://127.0.0.1:5273 "Showcase run (automated check)" || exit 1
#     deck_curl -s http://127.0.0.1:5273/api/v1/units      # curl with the session's cookie jar
#     deck_signout                                         # revoke the session, delete the jar
#
# deck_signin returns 0 when signed in OR when the deck's gate is off (DECK_GATE=off: nothing to sign in to),
# 1 when the deck is unreachable or refuses. Sourcing this file (re)sets DECK_GATE, DECK_OFFICER and
# DECK_SIGNED_URL (and reads DECK_COOKIE_JAR): a caller must not use those names for its own variables.
# They are the caller's shell variables, never exported: a child script that sources this helper signs in
# on its own and can never reuse, overwrite or delete its parent's cookie jar. The jar (mode 600) exists only
# while a session does — a gate-off deck or a refused sign-in leaves none behind. One attempt per call: the
# deck throttles 8 failed sign-ins per 5 min per client (operator-session.server.ts), so a retry loop would
# lock the operator out.
#
# The PIN is read at call time from $VEER_OPERATOR_PIN, else the file named by $VEER_OPERATOR_PIN_FILE (a
# sandbox deck's throwaway PIN), else the VEER_OPERATOR_PIN= line of ${VEER_ENV_FILE:-~/.config/veer/veer.env}.
# A PIN file that is NAMED but missing or unreadable is an error — never a fall-back to veer.env's (live) PIN.
# Surrounding whitespace is trimmed (the deck trims its own PIN); a quoted veer.env value is unquoted as
# scripts/veer_env.sh's loader does. It is never echoed, logged or put on a command line: the JSON body
# reaches curl on stdin. Safe under a caller's `set -euo pipefail`: every failure returns 1 with a message.

DECK_COOKIE_JAR="${DECK_COOKIE_JAR:-}"
DECK_GATE=""          # on | off after deck_signin
DECK_OFFICER=""
DECK_SIGNED_URL=""

_deck_pin() {  # the PIN on stdout; returns 1 when none is configured
    local pin="" f
    if [ -n "${VEER_OPERATOR_PIN:-}" ]; then
        pin="$VEER_OPERATOR_PIN"
    elif [ -n "${VEER_OPERATOR_PIN_FILE:-}" ]; then
        [ -r "$VEER_OPERATOR_PIN_FILE" ] || return 1
        pin="$(<"$VEER_OPERATOR_PIN_FILE")"
    else
        f="${VEER_ENV_FILE:-$HOME/.config/veer/veer.env}"
        [ -r "$f" ] || return 1
        pin="$(sed -n 's/^[[:space:]]*VEER_OPERATOR_PIN=//p' "$f" | tail -n 1)"
        pin="${pin%"${pin##*[![:space:]]}"}"
        [[ "$pin" =~ ^\"(.*)\"$ || "$pin" =~ ^\'(.*)\'$ ]] && pin="${BASH_REMATCH[1]}"
    fi
    pin="${pin#"${pin%%[![:space:]]*}"}"
    pin="${pin%"${pin##*[![:space:]]}"}"
    [ -n "$pin" ] || return 1
    printf '%s' "$pin"
}

deck_signin() {  # <deck_url> <officer label, e.g. "Repetition gate (automated check)">
    local url="${1%/}" officer="${2:?deck_signin: officer label}" gate code made_jar=""
    DECK_SIGNED_URL="$url"
    gate="$(curl -s -m 8 "$url/api/session" 2>/dev/null \
        | python3 -c 'import json,sys; print("on" if json.load(sys.stdin).get("gate") else "off")' 2>/dev/null || true)"
    case "$gate" in
        off) DECK_GATE=off; return 0 ;;   # no session, no jar
        on) ;;
        *) echo "deck_signin: $url/api/session did not answer" >&2; return 1 ;;
    esac
    if ! _deck_pin >/dev/null; then
        if [ -z "${VEER_OPERATOR_PIN:-}" ] && [ -n "${VEER_OPERATOR_PIN_FILE:-}" ]; then
            echo "deck_signin: VEER_OPERATOR_PIN_FILE ($VEER_OPERATOR_PIN_FILE) is missing, unreadable or empty — not falling back to veer.env" >&2
        else
            echo "deck_signin: the gate is on but no PIN is configured (VEER_OPERATOR_PIN, VEER_OPERATOR_PIN_FILE or veer.env)" >&2
        fi
        return 1
    fi
    # the jar exists only for a session (mode 600); a refused sign-in leaves none behind
    if [ -z "$DECK_COOKIE_JAR" ]; then
        DECK_COOKIE_JAR="$(mktemp "${TMPDIR:-/tmp}/veer-deck-jar.XXXXXX")" || { DECK_COOKIE_JAR=""; return 1; }
        made_jar=1
    fi
    chmod 600 "$DECK_COOKIE_JAR"
    code="$(_deck_pin | python3 -c 'import json,sys; sys.stdout.write(json.dumps({"officer": sys.argv[1], "pin": sys.stdin.read()}))' "$officer" \
        | curl -s -m 10 -o /dev/null -w '%{http_code}' -c "$DECK_COOKIE_JAR" \
            -H 'content-type: application/json' --data-binary @- "$url/api/session" || true)"
    if [ "$code" = 200 ]; then
        DECK_GATE=on; DECK_OFFICER="$officer"
        return 0
    fi
    if [ -n "$made_jar" ]; then rm -f "$DECK_COOKIE_JAR"; DECK_COOKIE_JAR=""; fi
    echo "deck_signin: sign-in refused (HTTP $code) — check the PIN; not retrying (the deck throttles failed sign-ins)" >&2
    return 1
}

deck_curl() {  # curl, carrying the session cookie when signed in
    if [ "$DECK_GATE" = on ] && [ -n "$DECK_COOKIE_JAR" ]; then
        curl -b "$DECK_COOKIE_JAR" "$@"
    else
        curl "$@"
    fi
}

deck_signout() {
    if [ "$DECK_GATE" = on ] && [ -n "$DECK_SIGNED_URL" ]; then
        curl -s -m 5 -o /dev/null -X DELETE -b "$DECK_COOKIE_JAR" "$DECK_SIGNED_URL/api/session" || true
    fi
    [ -n "$DECK_COOKIE_JAR" ] && rm -f "$DECK_COOKIE_JAR"
    DECK_COOKIE_JAR=""; DECK_GATE=""; DECK_OFFICER=""
}
