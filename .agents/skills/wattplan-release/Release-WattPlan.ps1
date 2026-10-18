#requires -Version 7.0
<#
.SYNOPSIS
Publish WattPlan from a clean checkout using a target tag and Markdown notes.
.EXAMPLE
./Release-WattPlan.ps1 -Tag v0.6.8-alpha -NotesFile C:/Temp/notes.md
#>
[CmdletBinding(DefaultParameterSetName = 'File')]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$')]
    [string] $Tag,

    [Parameter(Mandatory, ParameterSetName = 'File')]
    [string] $NotesFile,

    [Parameter(Mandatory, ParameterSetName = 'Text')]
    [string] $Notes,

    [ValidateRange(1, 1440)]
    [int] $TimeoutMinutes = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Check native exit codes ourselves, including inside an interactive caller.
$PSNativeCommandUseErrorActionPreference = $false

function Invoke-Native {
    param([string] $Command, [string[]] $Arguments)
    $output = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($Arguments -join ' ') failed (exit $LASTEXITCODE)."
    }
    return ($output -join "`n")
}

function Invoke-Git {
    Invoke-Native git $args
}

function Invoke-Gh {
    Invoke-Native gh (@($args) + @('--repo', $script:repository))
}

function Assert-Clean {
    if (Invoke-Git status --porcelain=v1 --untracked-files=all) {
        throw 'Working tree is unclean (including staged and untracked files). Commit or move changes first.'
    }
}

function Assert-UnusedTag {
    if ((Invoke-Git tag --list $Tag) -or
        (Invoke-Git ls-remote --tags origin "refs/tags/$Tag")) {
        throw "Tag $Tag already exists. Choose a new version; tags are never moved."
    }
}

function Wait-CI {
    param([string] $Branch, [string] $Sha)
    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes)
    $runId = $null
    do {
        if ($null -eq $runId) {
            $runs = @(Invoke-Gh run list --workflow ci.yml --event push --branch $Branch `
                --commit $Sha --limit 100 --json 'databaseId,headSha,headBranch,event,status,conclusion,url' |
                ConvertFrom-Json)
            $matching = @($runs | Where-Object {
                $_.headSha -eq $Sha -and $_.headBranch -eq $Branch -and $_.event -eq 'push'
            })
            if ($matching.Count -gt 1) {
                throw "Multiple push CI runs match $Branch at $Sha; inspect them before continuing."
            }
            if ($matching.Count -eq 1) {
                $runId = $matching[0].databaseId
                Write-Host "CI for ${Branch}: $($matching[0].url)"
            }
        }
        if ($null -ne $runId) {
            $run = Invoke-Gh run view $runId --json 'status,conclusion,url' | ConvertFrom-Json
            Write-Host "CI ${runId}: $($run.status) $($run.conclusion)"
            if ($run.status -eq 'completed') {
                if ($run.conclusion -ne 'success') {
                    throw "CI failed: $($run.conclusion). Inspect $($run.url)."
                }
                return
            }
        }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw "Timed out waiting for push CI on $Branch at $Sha."
        }
        Start-Sleep -Seconds 10
    } while ($true)
}

$stage = 'preflight'
$notesTemp = $null
$pushedLocation = $false
try {
    Get-Command git, gh -ErrorAction Stop | Out-Null
    if ($PSCmdlet.ParameterSetName -eq 'File') {
        $Notes = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $NotesFile).Path)
    }
    if ([string]::IsNullOrWhiteSpace($Notes)) { throw 'Release notes must not be empty.' }
    $version = $Tag.Substring(1)
    if ($version.Contains('-')) {
        foreach ($identifier in $version.Split('-', 2)[1].Split('.')) {
            if ($identifier -match '^0[0-9]+$') { throw 'Numeric prerelease identifiers cannot have leading zeroes.' }
        }
    }
    $root = Invoke-Native git @('-C', $PSScriptRoot, 'rev-parse', '--show-toplevel')
    Push-Location -LiteralPath $root
    $pushedLocation = $true
    Assert-Clean
    $origin = Invoke-Git remote get-url origin
    # Resolve origin explicitly; never let GH_REPO or another default redirect publication.
    $script:repository = Invoke-Native gh @('repo', 'view', $origin, '--json', 'nameWithOwner', '--jq', '.nameWithOwner')
    if ($script:repository -cne 'LordMike/WattPlan') {
        throw "Expected origin LordMike/WattPlan, got $script:repository."
    }
    Invoke-Git fetch --no-tags origin '+refs/heads/main:refs/remotes/origin/main' | Write-Host
    Assert-UnusedTag

    $stage = 'checkout main'
    if ((Invoke-Git branch --show-current) -ne 'main') {
        if (Invoke-Git branch --list main) {
            Invoke-Git switch main | Write-Host
        } else {
            Invoke-Git switch --create main --track origin/main | Write-Host
        }
    }
    if ((Invoke-Git rev-list --count origin/main..main) -ne '0') {
        throw 'Local main contains unpublished commits. Reconcile main before releasing.'
    }
    Invoke-Git merge --ff-only origin/main | Write-Host
    Assert-Clean

    $stage = 'update versions'
    $manifestPath = Join-Path $root 'custom_components/wattplan/manifest.json'
    $projectPath = Join-Path $root 'pyproject.toml'
    $manifestText = [IO.File]::ReadAllText($manifestPath)
    $projectText = [IO.File]::ReadAllText($projectPath)
    $oldVersion = ($manifestText | ConvertFrom-Json).version
    $projectPattern = [regex]::new('(?ms)(^\[project\]\r?\n(?:(?!^\[).)*?^version\s*=\s*")([^"\r\n]+)(")')
    $manifestPattern = [regex]::new('("version"\s*:\s*")([^"\r\n]+)(")')
    if ($manifestPattern.Matches($manifestText).Count -ne 1 -or
        $projectPattern.Matches($projectText).Count -ne 1 -or
        $projectPattern.Match($projectText).Groups[2].Value -ne $oldVersion) {
        throw 'Expected one matching version in the manifest and [project] table.'
    }
    if ($oldVersion -eq $version) { throw "Version is already $version. Inspect release state before continuing." }
    $replaceVersion = { param($match) $match.Groups[1].Value + $version + $match.Groups[3].Value }
    $newManifest = $manifestPattern.Replace($manifestText, $replaceVersion)
    $newProject = $projectPattern.Replace($projectText, $replaceVersion)
    if (($newManifest | ConvertFrom-Json).version -ne $version -or
        $projectPattern.Match($newProject).Groups[2].Value -ne $version) {
        throw 'Version update validation failed.'
    }
    $notesTemp = [IO.Path]::GetTempFileName()
    [IO.File]::WriteAllText($notesTemp, $Notes, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($manifestPath, $newManifest, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($projectPath, $newProject, [Text.UTF8Encoding]::new($false))
    Invoke-Git diff --check | Write-Host
    Invoke-Git add -- custom_components/wattplan/manifest.json pyproject.toml | Write-Host
    Invoke-Git commit -m "Prepare $version release" | Write-Host
    $sha = Invoke-Git rev-parse HEAD
    Write-Host "Release commit: $sha"

    # A version-only commit cannot break main CI; the tag CI builds, tests, and
    # validates the manifest version before anything is published.
    $stage = 'push main'
    Invoke-Git -c push.followTags=false push origin 'HEAD:refs/heads/main' | Write-Host

    $stage = 'create and push tag'
    Assert-UnusedTag
    Invoke-Git tag -a $Tag $sha -m "WattPlan $version" | Write-Host
    Invoke-Git -c push.followTags=false push origin "refs/tags/${Tag}:refs/tags/$Tag" | Write-Host
    Write-Host "Release tag pushed: $Tag at $sha"

    $stage = 'wait for tag CI'
    Wait-CI $Tag $sha
    $stage = 'verify published release'
    $release = Invoke-Gh release view $Tag --json 'tagName,isPrerelease,isDraft,assets,url' | ConvertFrom-Json
    if ($release.tagName -ne $Tag -or $release.isDraft -or
        $release.isPrerelease -ne $version.Contains('-') -or
        @($release.assets | Where-Object { $_ -and $_.name -eq 'wattplan.zip' -and $_.size -gt 0 }).Count -ne 1) {
        throw 'Published release has an unexpected tag, draft/prerelease state, or missing wattplan.zip.'
    }
    $stage = 'apply release notes'
    Invoke-Gh release edit $Tag --notes-file $notesTemp | Write-Host
    $published = Invoke-Gh release view $Tag --json 'body,url' | ConvertFrom-Json
    if ($published.body.Replace("`r`n", "`n").TrimEnd() -cne $Notes.Replace("`r`n", "`n").TrimEnd()) {
        throw 'Published release notes differ from the requested notes.'
    }
    Write-Host "Released $Tag`: $($published.url)"
} catch {
    throw "Release stopped during '${stage}'. Existing commits, pushes, and tags are preserved. $($_.Exception.Message)"
} finally {
    if ($notesTemp) { Remove-Item -LiteralPath $notesTemp -ErrorAction SilentlyContinue }
    if ($pushedLocation) { Pop-Location }
}
