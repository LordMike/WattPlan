# Release

WattPlan releases use the centrally managed CI workflow. Work from `main` in
the canonical WattPlan repository. Before pushing, check what will be included:

```bash
git fetch origin main
git status --short --branch
git log --oneline origin/main..HEAD
```

## Prepare the version

Choose the next SemVer version **without** a leading `v` and set the same value
in `custom_components/wattplan/manifest.json` and `pyproject.toml`. Set
`NEXT_VERSION` to that value for the commands below (replace the placeholder):

```bash
NEXT_VERSION=REPLACE_WITH_NEXT_SEMVER
python - "$NEXT_VERSION" <<'PY'
import json
from pathlib import Path
import sys
import tomllib

expected = sys.argv[1]
manifest = json.loads(
    Path("custom_components/wattplan/manifest.json").read_text(encoding="utf-8")
)["version"]
package = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]
if manifest != expected or package != expected:
    raise SystemExit(
        f"Version mismatch: expected {expected}, manifest={manifest}, package={package}"
    )
PY
./scripts/run_tests.sh
python scripts/build_hacs_zip.py \
  --output-name wattplan.zip \
  --validate-manifest-version "$NEXT_VERSION"
```

Run the tests from Linux/WSL with the environment described in
[Development](development.md). The ZIP check validates the manifest version;
the separate check above also catches a mismatched `pyproject.toml`.

## Push, tag, and publish

Commit and push the version change to `main`. Wait for that push's CI to pass
**before** tagging; fix failures on `main`, push again, and wait for green CI.
The CI and release commands below use an authenticated GitHub CLI (`gh`).

```bash
git add custom_components/wattplan/manifest.json pyproject.toml
git commit -m "Prepare $NEXT_VERSION release"
git push origin main
gh run list --branch main --limit 3
# Replace RUN_ID with the matching main run ID from the list.
gh run watch RUN_ID --exit-status
```

Use the established `v`-prefixed tag convention (the `v` is not stored in the
version files). Push only the intended tag and wait for its separate CI run:

```bash
git tag -a "v$NEXT_VERSION" -m "WattPlan $NEXT_VERSION"
git push origin "v$NEXT_VERSION"
gh run list --limit 5
# Replace RUN_ID with the matching tag run ID from the list.
gh run watch RUN_ID --exit-status
gh release view "v$NEXT_VERSION" --json tagName,isPrerelease,isDraft,assets,url
```

Tag CI validates the matching manifest version, builds `wattplan.zip`, and
creates a GitHub Release with generated notes. A version suffix such as
`-alpha` or `-beta.1` makes it a prerelease. Verify that the release has the
`wattplan.zip` attachment before announcing it.

If a specific release description is needed, **wait until tag CI has finished**
and then replace the generated body, for example with
`gh release edit "v$NEXT_VERSION" --notes-file /path/to/release-notes.md`.
For an existing release, CI refreshes attachments but does **not** update its
description; editing notes before tag CI finishes risks racing the publisher.

Do not move an existing published tag. Use a new patch version to correct a
failed or incorrect release.
