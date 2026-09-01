#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/VERSION"
BUMP="${1:-}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Prepare the next Cloud Migration Console release version.

Usage:
  scripts/prepare-release.sh patch [--dry-run]
  scripts/prepare-release.sh minor [--dry-run]
  scripts/prepare-release.sh major [--dry-run]
  scripts/prepare-release.sh X.Y.Z [--dry-run]

The command updates VERSION only. It never commits, tags, pushes, or publishes.
EOF
}

if [ "${2:-}" = "--dry-run" ]; then
  DRY_RUN=1
elif [ -n "${2:-}" ]; then
  usage >&2
  exit 2
fi

if [ -z "$BUMP" ] || [ "$BUMP" = "-h" ] || [ "$BUMP" = "--help" ]; then
  usage
  [ -n "$BUMP" ] && exit 0
  exit 2
fi

[ -f "$VERSION_FILE" ] || {
  echo "Missing VERSION file: $VERSION_FILE" >&2
  exit 1
}

CURRENT="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [[ ! "$CURRENT" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "Current VERSION is invalid: $CURRENT" >&2
  exit 1
fi

if git -C "$ROOT_DIR" ls-files --error-unmatch VERSION >/dev/null 2>&1 \
  && ! git -C "$ROOT_DIR" diff --quiet -- VERSION; then
  echo "VERSION already has uncommitted changes. Review or restore it before preparing another release." >&2
  exit 1
fi

IFS=. read -r CURRENT_MAJOR CURRENT_MINOR CURRENT_PATCH <<< "$CURRENT"
case "$BUMP" in
  patch)
    NEXT="$CURRENT_MAJOR.$CURRENT_MINOR.$((CURRENT_PATCH + 1))"
    ;;
  minor)
    NEXT="$CURRENT_MAJOR.$((CURRENT_MINOR + 1)).0"
    ;;
  major)
    NEXT="$((CURRENT_MAJOR + 1)).0.0"
    ;;
  *)
    if [[ ! "$BUMP" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
      echo "Release must be patch, minor, major, or an explicit X.Y.Z version." >&2
      exit 2
    fi
    NEXT="$BUMP"
    ;;
esac

IFS=. read -r NEXT_MAJOR NEXT_MINOR NEXT_PATCH <<< "$NEXT"
if (( NEXT_MAJOR < CURRENT_MAJOR \
  || (NEXT_MAJOR == CURRENT_MAJOR && NEXT_MINOR < CURRENT_MINOR) \
  || (NEXT_MAJOR == CURRENT_MAJOR && NEXT_MINOR == CURRENT_MINOR && NEXT_PATCH <= CURRENT_PATCH) )); then
  echo "Next version $NEXT must be greater than current version $CURRENT." >&2
  exit 1
fi

if git -C "$ROOT_DIR" rev-parse --verify --quiet "refs/tags/v$NEXT" >/dev/null; then
  echo "Tag v$NEXT already exists locally. Release tags must never be reused." >&2
  exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "Current version: v$CURRENT"
  echo "Next version:    v$NEXT"
  echo "Dry run only; VERSION was not changed."
  exit 0
fi

printf '%s\n' "$NEXT" > "$VERSION_FILE"
"$ROOT_DIR/scripts/check-release-version.sh" "v$NEXT"

cat <<EOF

Prepared Cloud Migration Console v$NEXT (previously v$CURRENT).
Only VERSION was changed. No commit, tag, push, or GitHub Release was created.

Next steps:
  1. Review the release changes and notes.
  2. Run the verification commands in docs/RELEASING.md.
  3. Commit and push main.
  4. Create and push the annotated tag v$NEXT.

After tag CI passes, the CI release job publishes the GitHub Release automatically.
EOF
