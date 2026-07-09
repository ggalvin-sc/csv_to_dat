# build_concordance_volume.ps1
# PAGE PURPOSE:
#   PowerShell wrapper around scripts/enrich_volume.py for Windows 11.
#   Builds a load-ready Concordance package (DAT/DCT/OPT + TEXT) from a thin
#   volume CSV that lacks native paths.
#
# FUNCTIONS:
#   (script body) Invokes enrich_volume.py with --source / optional --output.
#
# EXAMPLE:
#   powershell -File .\scripts\build_concordance_volume.ps1 `
#     -Source "G:\...\KVH_DAVILLA_Niger\KVH_DAVILLA_Niger"

param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    [string]$Output = "",

    [string]$Repo = "",

    [ValidateSet("classic", "relative")]
    [string]$Layout = "classic",

    [ValidateSet("auto", "copy", "hardlink", "symlink")]
    [string]$CopyMode = "auto",

    [string]$DataDir = "",

    [switch]$SelfContained,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if (-not $Repo) {
    $Repo = Split-Path -Parent $PSScriptRoot
}

$py = Join-Path $Repo "scripts\enrich_volume.py"
if (-not (Test-Path -LiteralPath $py)) {
    throw "enrich_volume.py not found at $py"
}

$argsList = @($py, "--source", $Source, "--repo", $Repo, "--layout", $Layout)
if ($Output) {
    $argsList += @("--output", $Output)
}
if ($SelfContained) {
    $argsList += "--self-contained"
}
elseif ($CopyMode -ne "auto") {
    $argsList += @("--copy-mode", $CopyMode)
}
if ($DataDir) {
    $argsList += @("--data-dir", $DataDir)
}
if ($DryRun) {
    $argsList += "--dry-run"
}

Write-Host "Running: python $($argsList -join ' ')"
& python @argsList
exit $LASTEXITCODE
