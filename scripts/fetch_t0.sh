#!/usr/bin/env bash
# fetch_t0.sh — provision the t0-alpha foundation forecaster artifact.
#
# infra/docker/Dockerfile.traffic-oracle.gb10 bakes models-local/t0/ into the
# oracle image (COPY models-local/t0/ ...). models-local/ is git-ignored (the
# 107 MB ONNX never enters the repo), so a fresh clone cannot build the
# oracle until this script has fetched it. Idempotent: an artifact whose
# sha256 already matches is left alone.
#
# Usage:  scripts/fetch_t0.sh [--dest DIR] [--check]
#   --dest DIR   target directory (default VVIP/models-local/t0)
#   --check      verify only; exit 1 if missing or wrong (no download)
#
# Provenance: huggingface.co/theforecastingcompany/t0-alpha-onnx-int8
# (Apache-2.0), pinned to the upstream git revision the oracle was built and
# calibrated with (2026-09-13, "Use INT8 weight storage with FP32 compute").
# The sha256 is the one that revision's config.json/manifest.json declare.
# Upstream `main` has since been re-exported (2026-10-18: a float32
# normalisation fix, a different 103 MB artifact); moving to it is a model
# change that needs the tier-0 forecaster re-calibrated, so it is opt-in:
# override BOTH T0_REVISION and T0_SHA256.

set -euo pipefail

VEER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="https://huggingface.co/theforecastingcompany/t0-alpha-onnx-int8"
REVISION="${T0_REVISION:-1c18a3e2296fa8afdf6daf471de16628a9b5eea8}"
ARTIFACT="t0-alpha-grouped-int8.onnx"
ARTIFACT_SHA256="${T0_SHA256:-82da43921c6b171a9f6ca8bd701f9a7923806438c3e95fd4eadbeda0cb03ed98}"
# small upstream files kept verbatim beside the artifact
SIDECARS=(LICENSE README.md config.json manifest.json CHANGELOG.md)

DEST="$VEER_ROOT/models-local/t0"
CHECK_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dest)  DEST="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        *) echo "usage: $0 [--dest DIR] [--check]" >&2; exit 2 ;;
    esac
done

log()  { echo "[fetch_t0] $*"; }
fail() { echo "[fetch_t0] ERROR: $*" >&2; exit 1; }

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

artifact_ok() {
    [ -f "$DEST/$ARTIFACT" ] && [ "$(sha_of "$DEST/$ARTIFACT")" = "$ARTIFACT_SHA256" ]
}

if artifact_ok; then
    log "OK: $DEST/$ARTIFACT (sha256 $ARTIFACT_SHA256)"
    exit 0
fi
[ "$CHECK_ONLY" = 1 ] && fail "missing or wrong artifact at $DEST/$ARTIFACT — run: $0"

command -v curl >/dev/null || fail "curl is required"
mkdir -p "$DEST"
tmp="$(mktemp -d "$DEST/.fetch.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

log "downloading $ARTIFACT from $REPO_URL @ $REVISION"
curl -fsSL --retry 3 --connect-timeout 20 -o "$tmp/$ARTIFACT" "$REPO_URL/resolve/$REVISION/$ARTIFACT" \
    || fail "download failed"
got="$(sha_of "$tmp/$ARTIFACT")"
[ "$got" = "$ARTIFACT_SHA256" ] || fail "sha256 mismatch: got $got, pinned $ARTIFACT_SHA256"

for f in "${SIDECARS[@]}"; do
    curl -fsSL --retry 3 --connect-timeout 20 -o "$tmp/$f" "$REPO_URL/resolve/$REVISION/$f" \
        || fail "download of $f failed"
done
# the upstream contract files must declare the same artifact hash
for f in config.json manifest.json; do
    grep -q "$ARTIFACT_SHA256" "$tmp/$f" || fail "$f does not declare sha256 $ARTIFACT_SHA256"
done

cat > "$tmp/NOTICE" <<EOF
# NOTICE — t0-alpha ONNX INT8 artifact

Obtained: $(date -u +%Y-%m-%dT%H:%M:%SZ) by scripts/fetch_t0.sh

- File: $ARTIFACT
- Size: $(stat -c %s "$tmp/$ARTIFACT") bytes
- sha256: $ARTIFACT_SHA256 (pinned in scripts/fetch_t0.sh; matches the
  sha256 declared by the upstream config.json and manifest.json)
- Source: $REPO_URL/resolve/$REVISION/$ARTIFACT
- Upstream revision: $REVISION
- License: Apache License 2.0 — full text in LICENSE (verbatim upstream copy)
- Consumer: traffic-oracle foundation forecaster rung
  (infra/docker/Dockerfile.traffic-oracle.gb10 bakes this directory;
  VVIP_FOUNDATION_MODEL_PATH points at the .onnx inside the image)
EOF

for f in "$ARTIFACT" "${SIDECARS[@]}" NOTICE; do
    mv -f "$tmp/$f" "$DEST/$f"
done
artifact_ok || fail "post-install verification failed"
log "installed $DEST/$ARTIFACT (sha256 verified) + ${SIDECARS[*]} NOTICE"
