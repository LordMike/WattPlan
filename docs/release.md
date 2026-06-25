# Release

This file is the authoritative release runbook for WattPlan. When release
questions conflict with workflow files or older notes, use this file first,
then verify the current workflow implementation.

WattPlan releases are GitHub tag driven. The release tag includes a leading `v`;
the integration manifest version does not. For example, tag `v0.5.0` must match
manifest version `0.5.0` exactly.

## Stable Release Steps

Use this flow for a new stable release. If the tag already exists, stop and use
the existing-tag recovery section instead.

1. Confirm the next release version, then set the shell variables used by the
   rest of this runbook:

   ```bash
   export NEXT_VERSION=0.5.0
   export NEXT_TAG="v$NEXT_VERSION"
   ```

2. Ensure the repo is on `main`, aligned with `origin/main`, and has no working
   tree changes:

   ```bash
   git status --short --branch
   ```

3. Confirm the tag and GitHub release do not already exist:

   ```bash
   git tag --list "$NEXT_TAG"
   gh release view "$NEXT_TAG"
   ```

   `git tag --list` should print nothing, and `gh release view` should return
   `release not found`. If either command finds something, use the existing-tag
   recovery section instead.

4. Set both version files to the release version without the leading `v`:

   ```bash
   sed -i -E 's/"version": "[^"]+"/"version": "'"$NEXT_VERSION"'"/' custom_components/wattplan/manifest.json
   sed -i -E 's/^version = "[^"]+"/version = "'"$NEXT_VERSION"'"/' pyproject.toml
   grep -n '"version"\|^version =' custom_components/wattplan/manifest.json pyproject.toml
   ```

5. Run tests and validate the HACS artifact locally:

   ```bash
   ./scripts/run_tests.sh
   python scripts/build_hacs_zip.py \
     --output-name wattplan.zip \
     --validate-manifest-version "$NEXT_VERSION"
   ```

6. Commit the version bump, then create and push the tag:

   ```bash
   git add custom_components/wattplan/manifest.json pyproject.toml
   git commit -m "Release $NEXT_TAG"
   git push origin main
   git tag "$NEXT_TAG"
   git push origin "$NEXT_TAG"
   ```

7. Wait for the `Release` workflow to complete successfully:

   ```bash
   gh run list --workflow Release --limit 5
   ```

8. Review the draft GitHub release, attached `wattplan.zip`, and generated
   release notes.
9. Replace the generated release notes with text following the template below.
10. Publish the draft release manually.

## Release Text Template

Edit release notes after the `Release` workflow has created the draft release
and attached `wattplan.zip`. Do not publish the draft until the release text has
been reviewed.

```markdown
WattPlan X.Y.Z <summarize the main user-visible theme in one sentence>.

<One short paragraph explaining the practical impact for users. Focus on what
changes in behavior, setup, entities, services, planning, restore behavior, or
automations.>

<Optional second paragraph for context if the release has multiple themes. Keep
it user-facing, not implementation-heavy.>

## Breaking changes

<Include only if needed. State exactly what changed, what old behavior, name, or
state is removed, and what users must update before upgrading.>

<If automations or dashboards are affected, provide the old-to-new mapping or
concrete migration guidance.>

## What's Changed

* <Generated PR item, preserving GitHub's generated format>
* <Generated PR item, preserving GitHub's generated format>

## Additional fixes

* <Use for notable commits not represented well by PR titles, or for clarifying
  important fixes. Include commit or PR links when useful.>
* <Omit this section if there are no extra notes.>

## Upgrade notes

* <Use only when users need to take action but it is not already covered by
  breaking changes.>
* <Omit this section if there is nothing actionable.>

**Full Changelog**: https://github.com/LordMike/WattPlan/compare/vPREVIOUS...vX.Y.Z
```

Release text rules:

- Lead with a human-written summary, not the generated PR list.
- Put `## Breaking changes` first when present.
- Keep `## What's Changed` as the generated GitHub PR list.
- Add `## Additional fixes` only for important direct commits or missing details.
- Add `## Upgrade notes` only for user actions not covered by breaking changes.
- End with the full changelog comparison link.
- Prefer user-visible behavior over internal implementation details.

## Existing Tag Or Failed Release

If the release tag already exists, do not assume it is usable.

1. Set the release variables for the existing tag:

   ```bash
   export NEXT_VERSION=0.5.0
   export NEXT_TAG="v$NEXT_VERSION"
   ```

2. Check the GitHub release and recent workflow runs:

   ```bash
   gh release view "$NEXT_TAG"
   gh run list --workflow Release --limit 5
   ```

3. If the workflow failed, inspect the failed logs:

   ```bash
   gh run view <run-id> --log-failed
   ```

4. If there is no published GitHub release and the failure is caused by a bad
   release commit, such as a manifest version mismatch, fix the version files,
   commit the fix, then move the tag to the corrected commit only with explicit
   maintainer approval:

   ```bash
   git tag -f "$NEXT_TAG"
   git push origin main
   git push origin "$NEXT_TAG" --force
   ```

5. If a GitHub release already exists, do not move the tag unless the
   maintainer explicitly approves replacing that release. Prefer a new patch
   release instead.

## Prereleases

Tags containing `-` are marked as GitHub prereleases automatically.

Examples:

- `v0.5.0-beta.1`
- `v0.5.0-rc.1`

The manifest version must still match the tag without the leading `v`, for
example `0.5.0-rc.1` for tag `v0.5.0-rc.1`.

## Workflow Behavior

WattPlan uses two GitHub Actions workflows:

- `CI` runs on pushes and pull requests and runs the test suite.
- `Release` runs on tags matching `v*`, pushes to `main`, and manual dispatch.

Tagged release builds run:

```bash
python scripts/build_hacs_zip.py \
  --output-name "wattplan.zip" \
  --validate-manifest-version "$VERSION"
```

For tag `$NEXT_TAG`, the workflow validates against `$NEXT_VERSION`.

The tagged release artifact is `dist/wattplan.zip`. It is the HACS release zip
and contains the Home Assistant integration payload from
`custom_components/wattplan/`, rooted under `custom_components/` in the archive.
It excludes the `utilities/` subtree and Python cache artifacts so
developer-only files are not shipped.

For tagged commits, the workflow publishes the same zip in two places:

- as a GitHub Actions workflow artifact
- as a GitHub release asset on the tag's draft release page

GitHub releases created by the workflow are drafts by default. Review the
generated release notes and attached `wattplan.zip`, then publish the release
manually when it is ready.

## Main Branch Dev Artifacts

Pushes to `main` build a release-style dev artifact labeled from the manifest
version plus the short commit SHA:

- `wattplan-<manifest-version>-dev.<sha>.zip`

These are uploaded as workflow artifacts, not published as GitHub releases, and
do not validate the manifest version against a tag.

Manual runs of the `Release` workflow behave the same way unless run from a tag
ref.

## Version Rules

- The Git tag is the public release identifier and includes the leading `v`.
- The manifest version omits the leading `v` and is enforced by the release
  workflow for tagged releases.
- `pyproject.toml` is not enforced by the release workflow, but must be kept in
  sync for repository consistency.
- Do not publish a release if the tag, manifest, and `pyproject.toml` disagree.
