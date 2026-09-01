#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/VERSION"
TAG="${1:-}"

[ -f "$VERSION_FILE" ] || {
  echo "Missing VERSION file." >&2
  exit 1
}

VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [[ ! "$VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "VERSION must use MAJOR.MINOR.PATCH, for example 1.4.2 (found: $VERSION)." >&2
  exit 1
fi

if [ -n "$TAG" ] && [ "$TAG" != "v$VERSION" ]; then
  echo "Release tag '$TAG' does not match VERSION '$VERSION'. Expected 'v$VERSION'." >&2
  exit 1
fi

echo "Release version is valid: v$VERSION"
