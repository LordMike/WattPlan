---
name: wattplan-release
description: Prepare and publish a WattPlan release or prerelease using its target tag and release notes. Use for WattPlan release requests and release workflow questions.
---

# WattPlan release

1. Determine the requested `v`-prefixed tag and draft release notes from changes
   since the previous release. Save notes outside the checkout or in ignored
   `.tmp/` so the checkout stays clean.
2. For a requested publication, run the colocated script with PowerShell 7,
   Git, and authenticated GitHub CLI:

   ```powershell
   pwsh -NoProfile -File .agents/skills/wattplan-release/Release-WattPlan.ps1 `
       -Tag v0.6.8-alpha -NotesFile C:/path/to/release-notes.md
   ```

   Substitute the intended tag and notes path; `-Notes 'Markdown text'` also
   accepts notes directly. The script owns checkout, versions, pushes, CI waits,
   tagging, release verification, and notes. `-TimeoutMinutes` defaults to 30
   per CI wait.
3. Report the release URL, or the failure stage and CI URL. On failure, inspect
   the preserved state before retrying; never move a published tag.
