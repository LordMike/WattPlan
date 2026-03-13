# Release

WattPlan uses two GitHub Actions workflows:

- `CI`
  - runs on pushes and pull requests
  - runs the test suite
- `Release`
  - runs on tags matching `v*`
  - runs on pushes to `main`
  - can also be started manually from GitHub Actions
  - builds a HACS-ready zip
  - publishes a GitHub release asset only for tags

## Versioning

The release workflow expects tags in this form:

- stable release: `v0.1.0`
- prerelease: `v0.2.0-beta.1`

The leading `v` is stripped before validating the integration manifest version.

## Artifact behavior

Tagged release builds run:

```bash
python scripts/build_hacs_zip.py \
  --output-name "wattplan.zip" \
  --validate-manifest-version "$VERSION"
```

The generated artifact is:

- tagged release: `dist/wattplan.zip`
- `main` branch / manual non-tag run: `dist/wattplan-<version>.zip`

The tagged release artifact is the HACS release zip.

For tagged commits, the workflow publishes the same zip in two places:

- as a GitHub Actions workflow artifact
- as a GitHub release asset on the tag's release page

## Prereleases

Tags containing `-` are marked as GitHub prereleases automatically.

Examples:

- `v0.3.0-beta.1`
- `v0.3.0-rc.1`

## Main branch dev artifacts

Pushes to `main` build a release-style dev artifact labeled from the manifest version plus the short commit SHA:

- `wattplan-<manifest-version>-dev.<sha>.zip`

These are uploaded as workflow artifacts, not published as GitHub releases, and do not validate the manifest version against a tag.

Manual runs of the `Release` workflow behave the same way unless you run them from a tag ref.
