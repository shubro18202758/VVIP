#!/usr/bin/env bash
# secret_scan.sh — release hygiene: no credential reaches a commit or a push.
#
#   bash scripts/secret_scan.sh [REPO_DIR] [--range REV_RANGE]
#
# REPO_DIR defaults to the repository this script lives in. It scans, in that
# repository:
#   tree     every file that would be committed: tracked + untracked files
#            not ignored (git ls-files -co --exclude-standard; when REPO_DIR is
#            not a git checkout, every file outside .git, node_modules, .venv(s),
#            build, target, .svelte-kit, __pycache__). Binary files are skipped.
#   diff     the lines `git diff --cached` (staged) and `git diff` (unstaged)
#            add — what the next commit will carry.
#   commits  the lines added by the commits a push would send, and their
#            messages: REV_RANGE, else @{upstream}..HEAD, else origin/main..HEAD,
#            else (no remote) every commit reachable from HEAD.
#
# Rules: the LITERAL values of VEER_OPERATOR_PIN, VEER_SESSION_SECRET and
# VEER_CESIUM_ION_TOKEN (read at run time from veer.env —
# ${VEER_ENV_FILE:-~/.config/veer/veer.env} — and the file named by
# $VEER_OPERATOR_PIN_FILE): a value of 20+ characters anywhere it occurs; a
# shorter one (the 6-digit PIN) only as a standalone token — not inside a hex
# hash, a decimal, an id|timestamp signature or a path — on a line that names
# a PIN/password, and never in *.dump, .run-models/ or evidence/snapshot JSON
# (those skips are reported); plus patterns: those three keys (and
# POSTGIS_PASSWORD) assigned a literal, a JWT (the shape of a Cesium ion
# token), a signed operator session cookie, a bearer token, a PEM private key,
# provider tokens (AWS, GitHub, Google, Slack, Hugging Face, OpenAI,
# Anthropic), a database URL with a password, and a quoted long literal
# assigned to a key/secret/token/password/pin name. Placeholders (${…}, <…>)
# and the repo's public dev defaults (dev_changeme, the 1234 test PIN) are not
# findings. gitleaks / trufflehog run as well when installed.
#
# Output: one `where:line: [rule]` per finding — NEVER the matched text, never
# a value. Exit 0 clean, 1 on any finding, 2 on a usage error.
#
#   bash scripts/secret_scan.sh --selftest   plants FAKE credentials of every
#   rule (and a leak that only an unpushed commit still carries) in a
#   throwaway git repo with a throwaway veer.env, next to placeholders, prose
#   and test fixtures that must stay silent; exit 0 only when every rule fires
#   and nothing else does.

set -uo pipefail

REPO=""
RANGE=""
SELFTEST=0
while [ $# -gt 0 ]; do
    case "$1" in
        --range) RANGE="${2:?--range needs a revision range}"; shift 2 ;;
        --selftest) SELFTEST=1; shift ;;
        -h|--help) sed -n '2,38p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "usage: bash scripts/secret_scan.sh [REPO_DIR] [--range REV_RANGE]" >&2; exit 2 ;;
        *) REPO="$1"; shift ;;
    esac
done
# ── --selftest: every rule fires on a planted fake, nothing fires on the rest ──
if [ "$SELFTEST" = 1 ]; then
    T="$(mktemp -d "${TMPDIR:-/tmp}/veer-secret-selftest.XXXXXX")" || exit 2
    trap 'rm -rf "$T"' EXIT
    g() { git -C "$T/repo" -c user.name=selftest -c user.email=selftest@invalid -c commit.gpgsign=false \
            -c core.hooksPath=/dev/null "$@" >/dev/null 2>&1; }
    mkdir -p "$T/repo/tests"
    git init -q "$T/repo"
    # The throwaway "real" values the value rules must find, and every planted
    # credential, are assembled from fragments: this file itself is scanned
    # with the repository and must not match its own rules.
    PIN=482913 SECRET=Zx9Qw8Er7Ty6Ui5Op4As3 PW=Sup3rS3cretPw DBPW=Hunter2Hunter2
    JWT="$(printf 'ey%s.ey%s.%s' JhbGciOiJIUzI1NiJ9 JqdGkiOiJzZWxmdGVzdCJ9 c2VsZnRlc3Qtc2lnbmF0dXJl)"
    printf '%s=%s\n' VEER_OPERATOR_PIN "$PIN" VEER_SESSION_SECRET "$SECRET" VEER_CESIUM_ION_TOKEN "$JWT" > "$T/veer.env"
    printf 'boot\n' > "$T/repo/README"
    g add -A; g commit -q -m base
    # a leak committed, then deleted: only the unpushed commits still carry it
    printf '%s=%s\n' VEER_OPERATOR_PIN "$PIN" > "$T/repo/old.env"
    g add -A; g commit -q -m leak
    rm "$T/repo/old.env"
    g add -A; g commit -q -m "drop it"
    # a commit MESSAGE that carries the PIN (messages are pushed too)
    g commit -q --allow-empty -m "rotate the operator pin to $PIN"
    printf '%s=%s\n' VEER_OPERATOR_PIN "$PIN" VEER_SESSION_SECRET "$SECRET" POSTGIS_PASSWORD "$PW" > "$T/repo/leaks.env"
    {
        printf '  %s: %s\n' VEER_CESIUM_ION_TOKEN "$JWT"
        printf '  url: %s://admin:%s@db:5432/corridor_db\n' postgresql "$DBPW"
    } > "$T/repo/leaks.yml"
    {
        printf "headers: { cookie: '%s=%s' }\n" veer_operator abcdefghijklmnopqrstuvwxyz0123456789.sig
        printf "const h = { Authorization: '%s %s' };\n" Bearer abcdefghijklmnopqrstuvwxyz012345
        printf 'const %s = "%s";\n' api_key abcdefghijklmnop1234
        printf -- '-----BEGIN RSA %s-----\n' 'PRIVATE KEY'
        printf 'AKIA%s\n' QWERTYUIOPASDFGH
        printf 'ghp_%s\n' abcdefghijklmnopqrstuvwxyzABCDEFGHIJ
        printf 'AIza%s\n' SyAbcdefghijklmnopqrstuvwxyz0123456
        printf 'xoxb-%s\n' 1234567890-abcdefghij
        printf 'hf_%s\n' abcdefghijklmnopqrstuvwxyzABCDEFGH
        printf 'sk-%s\n' abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN
        printf 'sk-ant-%s\n' abcdefghijklmnopqrstuvwxyz0123
    } > "$T/repo/leaks.js"
    # silent: placeholders, prose, public dev defaults, a PIN's digits inside a
    # decimal, and test fixtures' fake credentials
    cat > "$T/repo/quiet.sh" <<'QUIET'
POSTGRES_PASSWORD: ${POSTGIS_PASSWORD:-dev_changeme}
VEER_OPERATOR_PIN: ${VEER_OPERATOR_PIN:-}
# the VEER_OPERATOR_PIN= line of veer.env
echo "persist VEER_SESSION_SECRET: scripts/veer_env.sh --set KEY -"
DSN=postgresql://corridor_admin:${POSTGIS_PASSWORD:-dev_changeme}@127.0.0.1:56434/corridor_db
VEER_OPERATOR_PIN="$(cat "$STATE/pin")"
lon=72.4829131
QUIET
    # the PIN's digits inside a hash, an id|timestamp signature, a decimal and a
    # row id, on lines that name no PIN — and a snapshot JSON that names one
    printf 'sha a3f9%sbc\nrow %s|1729500000\nid=%s total 7\n' "$PIN" "$PIN" "$PIN" > "$T/repo/data.txt"
    mkdir -p "$T/repo/showcase"
    printf '{"pin_sig": "%s|1729500000", "id": %s, "blob": "%s"}\n' "$PIN" "$PIN" "$SECRET" > "$T/repo/showcase/snapshot.json"
    printf 'operator:\n  pin: %s\n' "$PIN" > "$T/repo/settings.yml"
    printf "process.env.%s = '%s';\n" VEER_OPERATOR_PIN 13579 VEER_SESSION_SECRET suite-secret-value > "$T/repo/tests/fake.entry.ts"
    g add -A
    out="$(env -u VEER_OPERATOR_PIN_FILE VEER_ENV_FILE="$T/veer.env" bash "${BASH_SOURCE[0]}" "$T/repo" --range HEAD 2>&1)"
    status=$?
    want=(operator-pin-literal session-secret-literal ion-token-literal postgis-password-literal jwt session-cookie
          bearer-token private-key aws-access-key github-token google-api-key slack-token huggingface-token
          openai-key anthropic-key database-url-password assigned-secret
          operator-pin-value session-secret-value ion-token-value)
    bad=0
    for r in "${want[@]}"; do
        grep -q "\[$r\]" <<<"$out" || { echo "secret_scan selftest: rule $r did NOT fire on its planted fake" >&2; bad=1; }
    done
    grep -qE '^  FINDING tree settings\.yml:2: \[operator-pin-value\]' <<<"$out" \
        || { echo "secret_scan selftest: the PIN as a standalone token next to 'pin:' was NOT found" >&2; bad=1; }
    grep -qE '^  FINDING tree showcase/snapshot\.json:1: \[session-secret-value\]' <<<"$out" \
        || { echo "secret_scan selftest: a 20+ char value inside a snapshot JSON was NOT found (long values match everywhere)" >&2; bad=1; }
    if grep -qE 'FINDING .*(data\.txt|showcase/snapshot\.json):[0-9]+: \[operator-pin-value\]' <<<"$out"; then
        echo "secret_scan selftest: FALSE POSITIVE — the PIN's digits inside a hash / signature / row id / snapshot JSON" >&2
        bad=1
    fi
    grep -qE 'skipped the short .* in [0-9]+ evidence/snapshot/dump file occurrence' <<<"$out" \
        || { echo "secret_scan selftest: the short-value skip was NOT reported" >&2; bad=1; }
    grep -qE '^  FINDING commit [0-9a-f]+:old\.env:1: \[operator-pin-(literal|value)\]' <<<"$out" \
        || { echo "secret_scan selftest: the leak only an unpushed commit carries was NOT found" >&2; bad=1; }
    grep -qE '^  FINDING commit [0-9a-f]+:\(message\):1: \[operator-pin-value\]' <<<"$out" \
        || { echo "secret_scan selftest: the PIN in an unpushed commit's MESSAGE was NOT found" >&2; bad=1; }
    if grep -E 'FINDING .*(quiet\.sh|tests/fake\.entry\.ts):' <<<"$out" >&2; then
        echo "secret_scan selftest: FALSE POSITIVE on a placeholder / prose / dev default / test fixture (above)" >&2
        bad=1
    fi
    [ "$status" = 1 ] || { echo "secret_scan selftest: a scan with findings exited $status, expected 1" >&2; bad=1; }
    if grep -qF -e "$PIN" -e "$SECRET" -e "$PW" -e "$DBPW" -e "$JWT" <<<"$out"; then
        echo "secret_scan selftest: the report PRINTED a planted value" >&2
        bad=1
    fi
    if [ "$bad" = 0 ]; then
        echo "secret_scan selftest: PASS — all ${#want[@]} rules fire on their planted fakes (incl. a leak only an unpushed commit carries, a PIN in an unpushed commit message, a short PIN next to 'pin:' and a long secret inside a snapshot JSON); placeholders, prose, dev defaults, test fixtures and the PIN's digits inside hashes/signatures/ids/snapshots stay silent, the skip is reported, no value printed"
    fi
    exit "$bad"
fi

REPO="$(cd "${REPO:-$(dirname "${BASH_SOURCE[0]}")/..}" 2>/dev/null && pwd)" \
    || { echo "secret_scan: no such directory" >&2; exit 2; }
command -v grep >/dev/null && echo x | grep -qP 'x' 2>/dev/null \
    || { echo "secret_scan: needs GNU grep with -P (PCRE)" >&2; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/veer-secret-scan.XXXXXX")" || exit 2
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT
FINDINGS="$WORK/findings"
: > "$FINDINGS"

is_git=0
git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 && is_git=1

# ── rules ────────────────────────────────────────────────────────────────────
# One PCRE per line: "<rule>\t<pattern>", matched against "path\tline\ttext"
# records. A key counts as assigned a literal in three shapes: shell/env
# KEY=value (no space after '='), a line-leading YAML/JSON "KEY": value, or
# code assigning a quoted literal (process.env.KEY = '…'); possessive
# quantifiers keep a ${…}/<…> placeholder or a public dev default (@FAKE@)
# from being matched by backtracking around it.
cat > "$WORK/rules" <<'EOF'
operator-pin-literal	(?:(?<![\w.])VEER_OPERATOR_PIN=["']?+|(?:\t\s*+|(?<!\$)[{,]\s*+)["']?VEER_OPERATOR_PIN["']?\s*+:(?![-+=?])\s*+["']?+|VEER_OPERATOR_PIN["']?\]?\s*+=\s*+["'])(?!@FAKE@(?:["'\s,;)}]|$)|\$|<|\{)[A-Za-z0-9]{4,}
session-secret-literal	(?:(?<![\w.])VEER_SESSION_SECRET=["']?+|(?:\t\s*+|(?<!\$)[{,]\s*+)["']?VEER_SESSION_SECRET["']?\s*+:(?![-+=?])\s*+["']?+|VEER_SESSION_SECRET["']?\]?\s*+=\s*+["'])(?!@FAKE@(?:["'\s,;)}]|$)|\$|<|\{)[A-Za-z0-9+/_=-]{8,}
ion-token-literal	(?:(?<![\w.])VEER_CESIUM_ION_TOKEN=["']?+|(?:\t\s*+|(?<!\$)[{,]\s*+)["']?VEER_CESIUM_ION_TOKEN["']?\s*+:(?![-+=?])\s*+["']?+|VEER_CESIUM_ION_TOKEN["']?\]?\s*+=\s*+["'])(?!\$|<|\{)[A-Za-z0-9._-]{20,}
postgis-password-literal	(?:(?<![\w.])POSTGIS_PASSWORD=["']?+|(?:\t\s*+|(?<!\$)[{,]\s*+)["']?POSTGIS_PASSWORD["']?\s*+:(?![-+=?])\s*+["']?+|POSTGIS_PASSWORD["']?\]?\s*+=\s*+["'])(?!@FAKE@(?:["'\s,;)}]|$)|\$|<|\{)[^\s"'$<{}]{6,}
jwt	\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
session-cookie	veer_operator=(?!\$|<|\{)[A-Za-z0-9._~%-]{24,}
bearer-token	[Aa]uthorization["']?\s*+[:=]?\s*+["']?Bearer\s++(?!\$|<|\{)[A-Za-z0-9._~+/-]{20,}
private-key	-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----
aws-access-key	\b(?:AKIA|ASIA)[0-9A-Z]{16}\b
github-token	\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})
google-api-key	\bAIza[0-9A-Za-z_-]{35}\b
slack-token	\bxox[abposr]-[A-Za-z0-9-]{10,}
huggingface-token	\bhf_[A-Za-z0-9]{30,}\b
openai-key	\bsk-(?!ant-)[A-Za-z0-9_-]{32,}\b
anthropic-key	\bsk-ant-[A-Za-z0-9_-]{20,}
database-url-password	\b(?:postgres(?:ql)?|redis|rediss|mysql|mongodb(?:\+srv)?|amqp|nats)://[^:/@\s"']++:(?!@FAKE@@|\$|<|\{)[^@\s"'/]{4,}@
assigned-secret	(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pin)["']?\s*+[:=]\s*+["'](?!@FAKE@["'])[A-Za-z0-9+/_=.-]{16,}["']
EOF
FAKE='(?:1234|0000|123456|test|testpin|changeme|dev_changeme|dummy|example|placeholder|redacted|x{3,}|\*{3,})'
sed -i "s/@FAKE@/${FAKE//\\/\\\\}/g" "$WORK/rules"
# Test fixtures carry fake credentials by design: the literal-shaped pattern
# rules skip them (the real values, private keys and provider/JWT tokens are
# still looked for everywhere).
TEST_EXEMPT=" operator-pin-literal session-secret-literal ion-token-literal postgis-password-literal database-url-password assigned-secret session-cookie bearer-token "
TEST_PATH='(^|[/:])(tests?|__tests__|fixtures?)/|(^|[/:])test_[^/]*[.]py$|[.](test|spec|unit)[.][a-z]+$|[.]entry[.]ts$'

# The literal values (never printed, never in argv): one PCRE per value with
# boundaries — a 4–8 digit PIN must not hide inside a longer number or a
# decimal (a coordinate).
ENV_FILE="${VEER_ENV_FILE:-$HOME/.config/veer/veer.env}"
nvalues=0
SHORT_VALUE_RULES=" "
# where a SHORT value is not looked for (digits there are hashes, ids, counts)
SHORT_SKIP='(^|[/:])[.]run-models/|[.]dump$|(^|[/:])(showcase|scripts/data|artifacts|fixtures|snapshots?|evidence|backups?)/.*[.](json|jsonl|ndjson|geojson|csv)$|snapshot[^/]*[.]json$'
add_value() {  # <rule> <value>
    local v="$2"
    [ -n "$v" ] || return 0
    [[ "$v" == *'\E'* ]] && { echo "secret_scan: a $1 value carries \\E — not scannable as a literal" >&2; return 0; }
    if [ "${#v}" -ge 20 ]; then
        printf '%s\t\\Q%s\\E\n' "$1" "$v" >> "$WORK/rules"
    else
        # a standalone token on a line that names a PIN / password
        printf '%s\t^[^\\t]*\\t[^\\t]*\\t(?=.*?(?i:\\bpin\\b|_pin|pin_|operatorpin|passw(?:or)?d|passcode|\\botp\\b)).*?(?<![A-Za-z0-9_.|:/-])\\Q%s\\E(?![A-Za-z0-9_|:/-])(?!\\.\\d)\n' "$1" "$v" >> "$WORK/rules"
        SHORT_VALUE_RULES="$SHORT_VALUE_RULES$1 "
    fi
    nvalues=$((nvalues + 1))
}
env_value() {  # <KEY> — the last KEY= line of veer.env, unquoted
    [ -r "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r' | sed 's/^"\(.*\)"$/\1/; s/^'"'"'\(.*\)'"'"'$/\1/'
}
add_value operator-pin-value "$(env_value VEER_OPERATOR_PIN)"
add_value session-secret-value "$(env_value VEER_SESSION_SECRET)"
add_value ion-token-value "$(env_value VEER_CESIUM_ION_TOKEN)"
if [ -n "${VEER_OPERATOR_PIN_FILE:-}" ] && [ -r "$VEER_OPERATOR_PIN_FILE" ]; then
    add_value operator-pin-value "$(tr -d '\r\n' < "$VEER_OPERATOR_PIN_FILE")"
fi

mapfile -t RULES < <(cut -f1 "$WORK/rules")
i=0
while IFS= read -r line; do
    printf '%s\n' "${line#*$'\t'}" > "$WORK/pat.$i"
    i=$((i + 1))
done < "$WORK/rules"

# scan_lines <label> <file of "where<TAB>line<TAB>text" records> — records a
# finding per rule hit; only the first two fields ever leave this function
scan_lines() {
    local label="$1" records="$2" k from
    awk -F '\t' -v re="$TEST_PATH" '$1 !~ re' "$records" > "$records.nontest"
    awk -F '\t' -v re="$SHORT_SKIP" '$1 !~ re' "$records" > "$records.short"
    awk -F '\t' -v re="$SHORT_SKIP" -v l="$label" '$1 ~ re { print l " " $1 }' "$records" | sort -u >> "$WORK/short_skipped"
    for k in "${!RULES[@]}"; do
        from="$records"
        [[ "$TEST_EXEMPT" == *" ${RULES[$k]} "* ]] && from="$records.nontest"
        [[ "$SHORT_VALUE_RULES" == *" ${RULES[$k]} "* ]] && from="$records.short"
        LC_ALL=C grep -aP -f "$WORK/pat.$k" "$from" 2>/dev/null | cut -f1,2 | while IFS=$'\t' read -r where ln; do
            printf '%s %s:%s: [%s]\n' "$label" "$where" "$ln" "${RULES[$k]}" >> "$FINDINGS"
        done
    done
}

# ── tree: every file a commit would carry ────────────────────────────────────
TREE="$WORK/tree.records"
: > "$TREE"
if [ "$is_git" = 1 ]; then
    git -C "$REPO" ls-files -z -co --exclude-standard
else
    # not a checkout: no .gitignore to honour, so skip dependency/build trees
    (cd "$REPO" && find . \( -name .git -o -name node_modules -o -name .venvs -o -name .venv -o -name build \
        -o -name target -o -name .svelte-kit -o -name __pycache__ \) -prune -o -type f -printf '%P\0')
fi > "$WORK/files"
nfiles=0
while IFS= read -r -d '' f; do
    [ -f "$REPO/$f" ] && [ ! -L "$REPO/$f" ] || continue    # gitlinks, symlinks, deleted files
    LC_ALL=C grep -Iq . "$REPO/$f" 2>/dev/null || continue   # binary (or empty)
    nfiles=$((nfiles + 1))
    awk -v f="$f" '{ print f "\t" NR "\t" $0 }' "$REPO/$f" >> "$TREE"
done < "$WORK/files"
scan_lines tree "$TREE"

# ── diff + commits: the lines a commit / a push would add ────────────────────
diff_records() {  # stdin: a unified diff (or git log -p); stdout: path<TAB>line<TAB>text
    awk '
        /^commit [0-9a-f]{40}/ { c = substr($2, 1, 10); next }
        /^\+\+\+ / { p = substr($0, 5); sub(/^b\//, "", p); next }
        /^@@ / { split($3, a, ","); n = substr(a[1], 2) + 0; next }
        /^\+/ { print (c ? c ":" : "") p "\t" n "\t" substr($0, 2); n++; next }
        /^ / { n++ }
    '
}
range_desc="(not a git checkout)"
if [ "$is_git" = 1 ]; then
    git -C "$REPO" diff --cached --no-color --no-ext-diff -U0 | diff_records > "$WORK/staged.records"
    git -C "$REPO" diff --no-color --no-ext-diff -U0 | diff_records > "$WORK/unstaged.records"
    scan_lines staged "$WORK/staged.records"
    scan_lines unstaged "$WORK/unstaged.records"
    if [ -z "$RANGE" ]; then
        if git -C "$REPO" rev-parse -q --verify '@{upstream}' >/dev/null 2>&1; then
            RANGE='@{upstream}..HEAD'
        elif git -C "$REPO" rev-parse -q --verify origin/main >/dev/null 2>&1; then
            RANGE='origin/main..HEAD'
        elif git -C "$REPO" rev-parse -q --verify HEAD >/dev/null 2>&1; then
            RANGE='HEAD'
        fi
    fi
    if [ -n "$RANGE" ]; then
        ncommits="$(git -C "$REPO" rev-list --count "$RANGE" 2>/dev/null || echo '?')"
        git -C "$REPO" log -p --no-color --no-ext-diff -U0 --format='commit %H' "$RANGE" 2>/dev/null \
            | diff_records > "$WORK/commits.records"
        scan_lines commit "$WORK/commits.records"
        # the commit messages travel with the push too (where = <commit>:(message))
        git -C "$REPO" log --no-color --format='%x01%H%n%B' "$RANGE" 2>/dev/null \
            | awk '/^\001/ { c = substr($0, 2, 10); n = 0; next } { n++; print c ":(message)\t" n "\t" $0 }' \
            > "$WORK/messages.records"
        scan_lines commit "$WORK/messages.records"
        range_desc="$RANGE ($ncommits commit(s), their messages included)"
    else
        range_desc="(no commits)"
    fi
fi

# ── gitleaks / trufflehog, when installed (their findings, redacted) ─────────
tools="grep rules"
if command -v gitleaks >/dev/null 2>&1; then
    tools="$tools + gitleaks"
    gitleaks detect --no-git --source "$REPO" --redact --no-banner --exit-code 1 >"$WORK/gitleaks.tree" 2>&1 \
        || printf 'gitleaks tree: findings (re-run: gitleaks detect --no-git --source %s --redact)\n' "$REPO" >> "$FINDINGS"
    if [ "$is_git" = 1 ] && [ -n "$RANGE" ]; then
        gitleaks detect --source "$REPO" --log-opts="$RANGE" --redact --no-banner --exit-code 1 >"$WORK/gitleaks.git" 2>&1 \
            || printf 'gitleaks commits: findings in %s (re-run: gitleaks detect --source %s --log-opts=%s --redact)\n' "$RANGE" "$REPO" "$RANGE" >> "$FINDINGS"
    fi
fi
if command -v trufflehog >/dev/null 2>&1; then
    tools="$tools + trufflehog"
    trufflehog filesystem "$REPO" --no-update --fail --only-verified >"$WORK/trufflehog" 2>&1 \
        || printf 'trufflehog: verified findings (re-run: trufflehog filesystem %s --no-update --only-verified)\n' "$REPO" >> "$FINDINGS"
fi

# ── report ───────────────────────────────────────────────────────────────────
echo "secret_scan: $REPO"
echo "  tree    $nfiles text file(s) a commit would carry"
[ "$is_git" = 1 ] && echo "  diff    staged + unstaged added lines"
echo "  commits $range_desc"
echo "  rules   $((${#RULES[@]} - nvalues)) pattern(s) + $nvalues literal value(s) from $( [ -r "$ENV_FILE" ] && echo "$ENV_FILE" || echo 'no veer.env')$( [ -n "${VEER_OPERATOR_PIN_FILE:-}" ] && echo ' and $VEER_OPERATOR_PIN_FILE'); $tools"
if [ "$SHORT_VALUE_RULES" != " " ] && [ -s "$WORK/short_skipped" ]; then
    nskip="$(sort -u "$WORK/short_skipped" | wc -l)"
    echo "  skipped the short (<20 char) value(s) — the PIN — in $nskip evidence/snapshot/dump file occurrence(s) across tree, diff and commits; longer values were matched there too:"
    sort -u "$WORK/short_skipped" | head -12 | sed 's/^/          /'
    [ "$nskip" -gt 12 ] && echo "          … and $((nskip - 12)) more"
fi
if [ -s "$FINDINGS" ]; then
    sort -u "$FINDINGS" | sed 's/^/  FINDING /'
    echo "secret_scan: $(sort -u "$FINDINGS" | wc -l) finding(s) — the matched text is never printed; open each file:line to judge it"
    exit 1
fi
echo "secret_scan: clean"
exit 0
