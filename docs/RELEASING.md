# Release Process

Cloud Migration Console uses versioned GitHub Releases as its production update channel. The dashboard never treats an ordinary commit on `main` as an application update.

## Version Source

The repository root [`VERSION`](../VERSION) file is the single application version source. It must contain exactly one stable semantic version:

```text
MAJOR.MINOR.PATCH
```

Examples: `0.1.0`, `0.1.1`, `1.0.0`.

- Increase `PATCH` for compatible fixes and small improvements.
- Increase `MINOR` for compatible new features.
- Increase `MAJOR` for incompatible behavior or configuration changes.

The Git tag must always be `v` followed by the exact file value. For example, `VERSION=0.2.0` requires tag `v0.2.0`. CI validates both the format and this match.

## Release Checklist

1. Confirm `main` is clean, reviewed, and passing CI.
2. Prepare the next version with the release helper. It updates `VERSION` but never commits, tags, pushes, or publishes:

   ```bash
   scripts/prepare-release.sh patch
   ```

   Use `minor`, `major`, or an explicit version such as `1.2.0` when appropriate. Add `--dry-run` to preview the result without changing the file.
3. Update relevant user documentation and review the generated GitHub release notes after publication.
4. Run the local verification commands:

   ```bash
   scripts/check-release-version.sh
   bash -n install.sh scripts/*.sh
   (cd backend && python -m unittest discover -p 'test_*.py')
   (cd frontend && npm run lint && npm run build)
   ```

5. Commit the release preparation, for example `release: v0.2.0`, and push `main`.
6. Create and push the matching annotated tag:

   ```bash
   git tag -a v0.2.0 -m "Cloud Migration Console v0.2.0"
   scripts/check-release-version.sh v0.2.0
   git push origin v0.2.0
   ```

7. Wait for the tag CI run to pass.
8. After backend tests, frontend checks, and the tag/version check pass, the `Publish validated release` CI job creates the published GitHub Release automatically. Do not create it before CI succeeds.
9. On a test installation, select `Settings` -> `System Upgrade` -> `Check`. Confirm that the release version and title appear, then complete one controlled upgrade before production rollout.

## Important Rules

- Do not edit `VERSION` for documentation-only changes.
- Use `scripts/prepare-release.sh` instead of calculating or editing the next release version manually.
- Do not reuse, move, or overwrite a published release tag.
- Do not publish a GitHub Release from `main` without its matching version tag.
- Fix a failed release with a new patch version instead of changing an existing published release.
- Keep release notes operational: mention configuration changes, migrations, security fixes, downtime expectations, and rollback considerations.
