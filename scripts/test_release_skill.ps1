#requires -Version 7.0
# Offline release workflow checks: real temporary Git repositories, simulated GitHub.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PSNativeCommandUseErrorActionPreference = $false
$releaseScript = Join-Path $PSScriptRoot '../.agents/skills/wattplan-release/Release-WattPlan.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('wattplan-release-tests-' + [guid]::NewGuid())
$originalGlobalConfig = $env:GIT_CONFIG_GLOBAL
$env:GIT_CONFIG_GLOBAL = if ($IsWindows) { 'NUL' } else { '/dev/null' }
$originalLocation = Get-Location

function Git-Test {
    $result = & git @args 2>&1
    if ($LASTEXITCODE -ne 0) { throw "git $args failed: $result" }
    return ($result -join "`n")
}

function Assert-Test([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}

# Functions shadow native gh only within this test process. No GitHub calls occur.
function gh {
    $global:LASTEXITCODE = 0
    $command = $args[0..1] -join ' '
    $global:releaseTestevents.Add($command)
    switch ($command) {
        'repo view' { 'LordMike/WattPlan' }
        'run list' {
            Assert-Test ($args -contains '--repo') 'GitHub repository must be explicit.'
            Assert-Test ($args -contains 'ci.yml') 'Must select CI workflow.'
            Assert-Test ($args -contains 'push') 'Must select push event.'
            $branch = $args[[array]::IndexOf($args, '--branch') + 1]
            $sha = $args[[array]::IndexOf($args, '--commit') + 1]
            $global:releaseTestcurrentBranch = $branch
            Assert-Test ($sha -eq (Git-Test rev-parse HEAD)) 'Wrong release commit.'
            $remoteRef = if ($branch -eq 'main') { 'refs/heads/main' } else { "refs/tags/$branch^{}" }
            Assert-Test ((Git-Test ls-remote origin $remoteRef).StartsWith($sha)) 'Must push before waiting.'
            if ($branch -eq 'main') {
                Assert-Test (-not (Git-Test tag --list v0.6.8-alpha)) 'Tag created before main CI passed.'
            }
            # A same-SHA run for a different ref must never satisfy the gate.
            @(
                @{ databaseId = 999; headSha = $sha; headBranch = 'unrelated'; event = 'push'; status = 'completed'; conclusion = 'success'; url = 'https://example.invalid/wrong' },
                @{ databaseId = $(if ($branch -eq 'main') { 1 } else { 2 }); headSha = $sha; headBranch = $branch; event = 'push'; status = 'queued'; conclusion = ''; url = 'https://example.invalid/ci' }
            ) | ConvertTo-Json -Compress
        }
        'run view' {
            Assert-Test ($args[2] -in @(1, 2)) 'Selected an unrelated CI run.'
            $conclusion = if (($global:releaseTestscenario -eq 'main-ci-fails' -and $global:releaseTestcurrentBranch -eq 'main') -or
                ($global:releaseTestscenario -eq 'tag-ci-fails' -and $global:releaseTestcurrentBranch -ne 'main')) { 'failure' } else { 'success' }
            @{ status = 'completed'; conclusion = $conclusion; url = 'https://example.invalid/ci' } | ConvertTo-Json -Compress
        }
        'release view' {
            Assert-Test ($global:releaseTestcurrentBranch -eq 'v0.6.8-alpha') 'Release inspected before tag CI.'
            if ($args -contains 'body,url') {
                @{ body = $global:releaseTestpublishedNotes; url = 'https://example.invalid/release' } | ConvertTo-Json -Compress
            } else {
                $assets = @(if ($global:releaseTestscenario -ne 'missing-asset') { @{ name = 'wattplan.zip'; size = 100 } })
                @{ tagName = 'v0.6.8-alpha'; isDraft = $false; isPrerelease = $true; assets = $assets; url = 'https://example.invalid/release' } | ConvertTo-Json -Depth 4 -Compress
            }
        }
        'release edit' {
            $global:releaseTestpublishedNotes = [IO.File]::ReadAllText($args[[array]::IndexOf($args, '--notes-file') + 1])
            if ($global:releaseTestscenario -eq 'notes-fail') { $global:LASTEXITCODE = 1 }
        }
        default { throw "Unexpected gh command: $args" }
    }
}

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    foreach ($global:releaseTestscenario in @('success', 'behind-main', 'dirty', 'staged', 'untracked', 'local-ahead',
        'local-tag', 'remote-tag', 'main-ci-fails', 'tag-ci-fails', 'missing-asset', 'notes-fail', 'empty-notes', 'invalid-tag')) {
        $caseRoot = Join-Path $testRoot $global:releaseTestscenario
        $checkout = Join-Path $caseRoot 'checkout'
        $remote = Join-Path $caseRoot 'remote.git'
        New-Item -ItemType Directory -Path $checkout -Force | Out-Null
        Set-Location $checkout
        Git-Test init --bare $remote | Out-Null
        Git-Test init --initial-branch=main | Out-Null
        Git-Test config user.email release-test@example.invalid | Out-Null
        Git-Test config user.name 'Release Test' | Out-Null
        Git-Test config core.autocrlf false | Out-Null
        Git-Test config commit.gpgsign false | Out-Null
        Git-Test config tag.gpgsign false | Out-Null
        $skillDirectory = Join-Path $checkout '.agents/skills/wattplan-release'
        New-Item -ItemType Directory -Path $skillDirectory -Force | Out-Null
        Copy-Item -LiteralPath $releaseScript -Destination $skillDirectory
        New-Item -ItemType Directory -Path custom_components/wattplan -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $checkout 'custom_components/wattplan/manifest.json'), "{`"version`": `"0.6.7-alpha`"}`n")
        [IO.File]::WriteAllText((Join-Path $checkout 'pyproject.toml'), "[build-system]`nrequires = []`n[project]`nname = `"wattplan`"`nversion = `"0.6.7-alpha`"`n[tool.example]`nversion = `"untouched`"`n")
        Git-Test add . | Out-Null
        Git-Test commit -m initial | Out-Null
        $initial = Git-Test rev-parse HEAD
        Git-Test remote add origin $remote | Out-Null
        Git-Test push -u origin main | Out-Null
        $global:releaseTestevents = [Collections.Generic.List[string]]::new()
        $global:releaseTestpublishedNotes = ''
        $global:releaseTestcurrentBranch = ''
        switch ($global:releaseTestscenario) {
            'success' { Git-Test switch -c feature | Out-Null }
            'behind-main' {
                Git-Test switch -c feature | Out-Null
                Git-Test commit --allow-empty -m 'Remote advance' | Out-Null
                Git-Test push origin HEAD:main | Out-Null
            }
            'dirty' { Add-Content -LiteralPath pyproject.toml -Value '# user change' }
            'staged' {
                Add-Content -LiteralPath pyproject.toml -Value '# user change'
                Git-Test add pyproject.toml | Out-Null
            }
            'untracked' { [IO.File]::WriteAllText((Join-Path $checkout 'user.txt'), 'user data') }
            'local-ahead' { Git-Test commit --allow-empty -m unpublished | Out-Null }
            'local-tag' { Git-Test tag v0.6.8-alpha | Out-Null }
            'remote-tag' {
                Git-Test tag v0.6.8-alpha | Out-Null
                Git-Test push origin refs/tags/v0.6.8-alpha | Out-Null
                Git-Test tag -d v0.6.8-alpha | Out-Null
            }
        }
        $before = Git-Test rev-parse HEAD
        $beforeStatus = Git-Test status --porcelain=v1 --untracked-files=all
        $notes = "## Changes`n`nPrice fixes, æøå, literal `$variable and ``code``.`n"
        $parameters = @{ Tag = 'v0.6.8-alpha'; Notes = $notes }
        if ($global:releaseTestscenario -eq 'empty-notes') { $parameters.Notes = '   ' }
        if ($global:releaseTestscenario -eq 'invalid-tag') { $parameters.Tag = 'v0.6.8-01' }
        if ($global:releaseTestscenario -eq 'success') {
            $notesPath = Join-Path $caseRoot 'notes.md'
            [IO.File]::WriteAllText($notesPath, $notes)
            $parameters.Remove('Notes')
            $parameters.NotesFile = $notesPath
        }
        $failure = $null
        try { & (Join-Path $skillDirectory 'Release-WattPlan.ps1') @parameters | Out-Null }
        catch { $failure = $_.Exception.Message }
        if ($global:releaseTestscenario -in @('success', 'behind-main')) {
            Assert-Test ($null -eq $failure) "Unexpected failure: $failure"
            Assert-Test ($global:releaseTestpublishedNotes -ceq $notes) 'Notes were not preserved.'
            Assert-Test ((Git-Test branch --show-current) -eq 'main') 'Did not switch to main.'
            Assert-Test ((Git-Test status --porcelain) -eq '') 'Successful release left dirty files.'
            Assert-Test ((Git-Test cat-file -t refs/tags/v0.6.8-alpha) -eq 'tag') 'Tag is not annotated.'
            Assert-Test ((Get-Content -Raw pyproject.toml).Contains('version = "untouched"')) 'Changed unrelated version.'
            Assert-Test ((Get-Content -Raw pyproject.toml).Contains('version = "0.6.8-alpha"')) 'Project version was not updated.'
            Assert-Test ((Get-Content -Raw custom_components/wattplan/manifest.json | ConvertFrom-Json).version -eq '0.6.8-alpha') 'Manifest version was not updated.'
        } else {
            Assert-Test ($null -ne $failure) "Expected $global:releaseTestscenario to fail."
            $expectedError = switch ($global:releaseTestscenario) {
                { $_ -in @('dirty', 'staged', 'untracked') } { 'Working tree is unclean' }
                'local-ahead' { 'Local main contains unpublished commits' }
                { $_ -in @('local-tag', 'remote-tag') } { 'already exists' }
                { $_ -in @('main-ci-fails', 'tag-ci-fails') } { 'CI failed: failure' }
                'missing-asset' { 'missing wattplan.zip' }
                'notes-fail' { 'failed (exit 1)' }
                'empty-notes' { 'Release notes must not be empty' }
                'invalid-tag' { 'Numeric prerelease identifiers cannot have leading zeroes' }
            }
            Assert-Test ($failure.Contains($expectedError)) "Wrong failure for $global:releaseTestscenario`: $failure"
            Assert-Test (-not ($global:releaseTestevents -contains 'release edit') -or $global:releaseTestscenario -eq 'notes-fail') 'Notes changed after a failed gate.'
            if ($global:releaseTestscenario -in @('dirty', 'staged', 'untracked', 'local-ahead', 'local-tag', 'remote-tag', 'empty-notes', 'invalid-tag')) {
                Assert-Test ((Git-Test rev-parse HEAD) -eq $before) 'Preflight failure changed HEAD.'
                Assert-Test ((Git-Test status --porcelain=v1 --untracked-files=all) -eq $beforeStatus) 'Preflight failure changed user files.'
            }
            if ($global:releaseTestscenario -eq 'main-ci-fails') {
                Assert-Test (-not (Git-Test tag --list v0.6.8-alpha)) 'Failed main CI still created a tag.'
                Assert-Test ((Git-Test rev-parse HEAD) -ne $initial) 'Expected pushed release commit to be preserved.'
            }
            if ($global:releaseTestscenario -in @('tag-ci-fails', 'missing-asset', 'notes-fail')) {
                Assert-Test ([bool](Git-Test ls-remote --tags origin refs/tags/v0.6.8-alpha)) 'Pushed tag was not preserved.'
            }
        }
        Write-Host "PASS $global:releaseTestscenario"
    }
} finally {
    Set-Location $originalLocation
    $env:GIT_CONFIG_GLOBAL = $originalGlobalConfig
    # Keep fixtures for inspection; this script never deletes a repository.
    Write-Host "Offline Git fixtures: $testRoot"
}
