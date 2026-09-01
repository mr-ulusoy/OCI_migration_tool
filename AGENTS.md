# Repository Instructions

## Application Releases

- Read `docs/RELEASING.md` before preparing or publishing an application release.
- Do not change the root `VERSION` file for ordinary code changes, documentation changes, fixes, or pushes to `main`.
- Change `VERSION` only when the user explicitly asks to prepare or publish a release.
- For an explicit release request, use `scripts/prepare-release.sh patch|minor|major` instead of calculating or editing the version manually.
- The release tag must be exactly `v$(cat VERSION)`. Never reuse, move, overwrite, or force-push a release tag.
- Never create a tag, push a tag, or publish a GitHub Release unless the user explicitly asks for those remote actions.
- A pushed matching version tag is validated by CI. GitHub Release publication is handled by the CI release job only after backend and frontend checks pass.
