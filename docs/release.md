# Release

WattPlan releases use the centrally managed CI workflow. Set the same version,
without a leading `v`, in:

- `custom_components/wattplan/manifest.json`
- `pyproject.toml`

Run the test suite and validate the HACS archive before committing:

```bash
./scripts/run_tests.sh
python scripts/build_hacs_zip.py \
  --output-name wattplan.zip \
  --validate-manifest-version 0.5.0
```

Commit and push the version change to `main`, then create and push a matching
SemVer tag such as `v0.5.0`. The optional `v` prefix is not part of the version
stored in the files.

Managed CI reruns validation, requires the manifest version to match the tag,
builds `wattplan.zip`, and creates or updates the GitHub Release with generated
notes. A tag with a suffix such as `v0.5.0-beta.1` is published as a
prerelease. Do not move an existing published tag; use a new patch version to
correct a failed or incorrect release.
