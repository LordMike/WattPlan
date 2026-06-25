# Release

This is the authoritative WattPlan release runbook. Releases are GitHub tag
driven: tag `v0.5.0` must match manifest and project version `0.5.0`.

## Stable Release

Stop and use the recovery section if the tag or GitHub release already exists.

```bash
export NEXT_VERSION=0.5.0
export NEXT_TAG="v$NEXT_VERSION"

git fetch origin
git status --short --branch
git tag --list "$NEXT_TAG"
gh release view "$NEXT_TAG" || true
```

Required state before continuing:

- `git status` shows clean `main...origin/main`.
- `git tag --list "$NEXT_TAG"` prints nothing.
- `gh release view "$NEXT_TAG"` returns `release not found`.

Set both version files to `$NEXT_VERSION`, without the leading `v`:

- `custom_components/wattplan/manifest.json`
- `pyproject.toml`

Validate locally:

```bash
grep -n '"version"\|^version =' custom_components/wattplan/manifest.json pyproject.toml
./scripts/run_tests.sh
python scripts/build_hacs_zip.py --output-name wattplan.zip --validate-manifest-version "$NEXT_VERSION"
```

Commit, push, and tag:

```bash
git diff -- custom_components/wattplan/manifest.json pyproject.toml
git status --short --branch
git add custom_components/wattplan/manifest.json pyproject.toml
git commit -m "Release $NEXT_TAG"
git push origin main
git tag "$NEXT_TAG"
git push origin "$NEXT_TAG"
```

Wait for the tagged release workflow. A separate `main` workflow run may also
appear; publish only after the tag run succeeds.

```bash
gh run list --workflow Release --limit 5
gh run watch <tag-run-id> --exit-status
gh release view "$NEXT_TAG" --json tagName,name,isDraft,isPrerelease,assets,url
```

Before publishing the draft release:

- Verify the release is a draft, not a prerelease for stable tags.
- Verify `wattplan.zip` is attached.
- Replace the generated notes with user-facing release text.
- Check `git status --short --branch` is clean.

## Release Notes

Use this shape, keeping generated PR bullets under `What's Changed`:

```markdown
WattPlan X.Y.Z <one-sentence user-visible theme>.

<One short paragraph explaining the practical impact for users. Focus on setup,
entities, services, planning, restore behavior, or automations.>

## Breaking changes

<Only include if users must change something. State exactly what changed and how
to migrate.>

## What's Changed

* <Generated PR item>
* <Generated PR item>

## Additional fixes

* <Only include important fixes not clear from PR titles. Omit otherwise.>

## Upgrade notes

* <Only include actionable upgrade notes. Omit otherwise.>

**Full Changelog**: https://github.com/LordMike/WattPlan/compare/vPREVIOUS...vX.Y.Z
```

Rules:

- Lead with human-written release impact, not the generated PR list.
- Keep `What's Changed` as GitHub generated it.
- Put `Breaking changes` before `What's Changed` when present.
- Omit empty optional sections.

## Test Venv Missing

If `./scripts/run_tests.sh` fails with `bin/pytest: No such file or directory`,
recreate the worktree-specific `/tmp` venv:

```bash
WORKTREE_KEY=$(printf '%s' "$PWD" | cksum | cut -d ' ' -f 1)
VENV_DIR="/tmp/wattplan-venv-$WORKTREE_KEY"

uv venv "$VENV_DIR" --python python3.14 --clear
uv pip install --python "$VENV_DIR/bin/python" -r requirements-test.txt
```

Use Python 3.14 or newer.

## Existing Tag Or Failed Release

Do not move a tag unless a maintainer explicitly approves it.

```bash
export NEXT_VERSION=0.5.0
export NEXT_TAG="v$NEXT_VERSION"

gh release view "$NEXT_TAG"
gh run list --workflow Release --limit 5
gh run view <run-id> --log-failed
```

If there is no published GitHub release and the failed tag points at a bad
release commit, fix the issue, commit it, then move the tag only after explicit
approval:

```bash
git tag -f "$NEXT_TAG"
git push origin main
git push origin "$NEXT_TAG" --force
```

If a GitHub release already exists, prefer a new patch release instead of moving
the tag.

## Prereleases

Tags containing `-` are GitHub prereleases automatically, for example:

- `v0.5.0-beta.1`
- `v0.5.0-rc.1`

The manifest and project version must still match the tag without the leading
`v`, for example `0.5.0-rc.1`.

## Reference

- `Release` runs on pushes to `main`, tags matching `v*`, and manual dispatch.
- `main` runs upload workflow artifacts only, named `wattplan-<version>-dev.<sha>.zip`.
- Tag runs validate the manifest version and attach `wattplan.zip` to a draft GitHub release.
- `wattplan.zip` contains `custom_components/wattplan/` rooted under `custom_components/`.
- The release workflow enforces the manifest version, but `pyproject.toml` must stay in sync.
