# scripts/ci_test.ps1 - Local CI smoke script (Windows PowerShell)
#
# WHAT THIS SCRIPT DOES
# ---------------------
# Mirrors the GitHub Actions test job so you can verify the same checks
# before pushing. Run from the csv_to_dat package root.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

Write-Host "== unit tests =="
python tests\test_roundtrip.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "== CLI smoke =="
$tmp = Join-Path $env:TEMP ("csv_to_dat_ci_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  python run.py --help | Out-Null
  python run.py csv2dat tests\fixtures\VOL001_slice.csv "$tmp\out.dat" --field-names BEGDOC,ENDDOC,CODED,FILEPATH
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python run.py validate "$tmp\out.dat" --field-names BEGDOC,ENDDOC,CODED,FILEPATH
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python run.py opt "$tmp\out.dat" "$tmp\out.opt" --volume VOL001 --image-dir "IMAGES\001"
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python run.py roundtrip --direction csv2dat tests\fixtures\VOL001_slice.csv --field-names BEGDOC,ENDDOC,CODED,FILEPATH
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  Write-Host "CI smoke PASS"
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
