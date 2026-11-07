# Locate the Python interpreter used to launch INTENT-win.
# Priority: self-contained runtime inside the app folder -> explicit override
#           -> legacy WorkBuddy bundled Python -> nothing (caller falls back to PATH).
# ASCII-only on purpose: avoids encoding issues when saved from editors.

$local = Join-Path $PSScriptRoot 'runtime\python\python.exe'
if (Test-Path -LiteralPath $local) { Write-Output $local; exit 0 }

if ($env:INTENT_PYTHON -and (Test-Path -LiteralPath $env:INTENT_PYTHON)) {
    Write-Output $env:INTENT_PYTHON
    exit 0
}

$exe = (Get-ChildItem "$env:USERPROFILE\.workbuddy\binaries\python\versions\*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -Last 1).FullName
if ($exe -and (Test-Path $exe)) { Write-Output $exe }
exit 0
