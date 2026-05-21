#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-$(git -C "$PROJECT_DIR" describe --tags --always --dirty 2>/dev/null || date +%Y%m%d%H%M%S)}"
DIST_DIR="${DIST_DIR:-$PROJECT_DIR/dist-packages}"
PACKAGE_NAME="oci-migrator-$VERSION.tar.gz"

mkdir -p "$DIST_DIR"

if git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$PROJECT_DIR" archive --format=tar.gz --prefix="oci-migrator-$VERSION/" -o "$DIST_DIR/$PACKAGE_NAME" HEAD
else
  tar \
    --exclude='venv' \
    --exclude='frontend/node_modules' \
    --exclude='frontend/dist' \
    --exclude='backend/__pycache__' \
    --exclude='*.log' \
    -czf "$DIST_DIR/$PACKAGE_NAME" \
    -C "$PROJECT_DIR/.." "$(basename "$PROJECT_DIR")"
fi

echo "$DIST_DIR/$PACKAGE_NAME"
