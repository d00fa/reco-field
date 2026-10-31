#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
DIST_DIR="$SOURCE_ROOT/appliance/dist"
VERSION=$(cat "$SOURCE_ROOT/appliance/VERSION")
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'Invalid appliance version\n' >&2; exit 1; }
NAME=reco-field-$VERSION-source

mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR/$NAME.tar.gz"
tar \
  --exclude=.git \
  --exclude='appliance/.venv*' \
  --exclude='appliance/build' \
  --exclude='appliance/.install-smoke*' \
  --exclude='appliance/.tools' \
  --exclude='appliance/.qa-*' \
  --exclude='appliance/qa' \
  --exclude='appliance/dist' \
  --exclude='appliance/.pytest_cache' \
  --exclude='docs/agent-work' \
  --exclude='*.egg-info' \
  --exclude='target' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.log' \
  --exclude='*.mkv' \
  --exclude='*.mp4' \
  --exclude='*.key' \
  --exclude='*.crt' \
  --exclude='*.pem' \
  --exclude='*.p12' \
  --exclude='.env' \
  --exclude='.env.*' \
  --owner=0 --group=0 --mode='u+rwX,go+rX,go-w' \
  --transform "s,^\.,$NAME," \
  -czf "$DIST_DIR/$NAME.tar.gz" \
  -C "$SOURCE_ROOT" .
(cd "$DIST_DIR" && sha256sum "$NAME.tar.gz") > "$DIST_DIR/$NAME.tar.gz.sha256"
printf '%s\n' "$DIST_DIR/$NAME.tar.gz"
