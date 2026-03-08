# Release

WattPlan uses two GitHub Actions workflows:

- `CI`
  - runs on pushes and pull requests
  - runs the test suite
  - builds a zip artifact on `main`
- `Release`
  - runs on tags matching `v*`
  - builds a HACS-ready zip
  - publishes it as a GitHub release asset

## Versioning

The release workflow expects tags in this form:

- stable release: `v0.1.0`
- prerelease: `v0.2.0-beta.1`

The leading `v` is stripped before validating the integration manifest version.

## Artifact behavior

Release builds run:

```bash
python scripts/build_hacs_zip.py \
  --version-label "$VERSION" \
  --validate-manifest-version "$VERSION"
```

The generated artifact is:

- `dist/wattplan-<version>.zip`

The zip contains the integration tree under:

- `src/custom_components/wattplan/...`

## Prereleases

Tags containing `-` are marked as GitHub prereleases automatically.

Examples:

- `v0.3.0-beta.1`
- `v0.3.0-rc.1`

## Main branch artifacts

Pushes to `main` build a CI artifact labeled with the short commit SHA:

- `wattplan-main-<sha>.zip`

These are uploaded as workflow artifacts, not published as GitHub releases.
