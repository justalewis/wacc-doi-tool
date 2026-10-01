<#
.SYNOPSIS
  Pull the latest code from GitHub and restart the WAC DOI tool.

.DESCRIPTION
  Elevated Windows PowerShell 5.1:   C:\WaccDoi\app\deploy\windows\Update-WaccDoi.ps1

  Fast-forward only. If the working copy has local edits it stops rather than discarding
  them. Prints the commit it came from, so rolling back is `git checkout <that commit>`
  followed by a restart.
#>
[CmdletBinding()]
param(
    [string]$Root        = 'C:\WaccDoi',
    [string]$ServiceName = 'WaccDoi',
    [int]   $Port        = 8081
)

$ErrorActionPreference = 'Stop'
$AppDir = Join-Path $Root 'app'
$venvPy = Join-Path $Root 'venv\Scripts\python.exe'

function Run-Native([string]$exe, [string[]]$arguments) {
    $out = & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ("$exe failed (exit $LASTEXITCODE): " + ($out -join ' ')) }
    return $out
}

Set-Location $AppDir
$dirty = Run-Native 'git' @('status', '--porcelain')
if ($dirty) { throw "Local changes in $AppDir. Look at 'git status' before updating:`n$($dirty -join "`n")" }

$before = (Run-Native 'git' @('rev-parse', '--short', 'HEAD')) | Select-Object -First 1
[void](Run-Native 'git' @('pull', '--ff-only'))
$after = (Run-Native 'git' @('rev-parse', '--short', 'HEAD')) | Select-Object -First 1
if ($before -eq $after) { Write-Host "Already at $after. Nothing to do."; return }

[void](Run-Native $venvPy @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $AppDir 'requirements.txt')))
[void](Run-Native $venvPy @((Join-Path $AppDir 'tools\fetch_schemas.py')))
Restart-Service $ServiceName

$healthy = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($h.ok -eq $true -and $h.schemas -eq $true) { $healthy = $true; break }
    } catch { }
}
if (-not $healthy) { throw "Updated $before -> $after but /health is not answering. Roll back: git checkout $before ; Restart-Service $ServiceName" }
Write-Host "Updated $before -> $after and healthy. Roll back with: git checkout $before ; Restart-Service $ServiceName"
