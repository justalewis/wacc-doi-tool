<#
.SYNOPSIS
  Install the WAC DOI deposit tool as a Windows service behind IIS/ARR, beside other sites.

.DESCRIPTION
  Run from an ELEVATED Windows PowerShell 5.1 window, from inside the git clone:

      cd C:\WaccDoi\app
      .\deploy\windows\Install-WaccDoi.ps1 -Plan      # checks only, changes nothing
      .\deploy\windows\Install-WaccDoi.ps1            # does it

  It touches only things named for this tool: the folder C:\WaccDoi, the service WaccDoi,
  the app pool and site WaccDoi, and one rewrite rule scoped to that site. It takes an IIS
  restore point first. It refuses to run if any of those names or the port already exist,
  so it cannot overwrite a neighbour.

  Targets PowerShell 5.1: no &&, no ternary, no null-coalescing.
#>
[CmdletBinding()]
param(
    [switch]$Plan,
    [string]$Root        = 'C:\WaccDoi',
    [string]$HostName    = 'deposit.wacclearinghouse.org',
    [int]   $Port        = 8081,
    [string]$ServiceName = 'WaccDoi',
    [string]$SiteName    = 'WaccDoi',
    [string]$Python      = 'C:\Program Files\Python312\python.exe',
    [string]$Nssm        = 'C:\nssm\win64\nssm-2.24\nssm-2.24\win64\nssm.exe',
    [string]$Mailto      = 'jlewis2@olympic.edu',
    [string]$Registrant  = 'WAC Clearinghouse',
    [string]$Prefix      = '10.37514',
    [securestring]$AccessCode
)

$ErrorActionPreference = 'Stop'
$AppDir  = Join-Path $Root 'app'
$VenvDir = Join-Path $Root 'venv'
$LogDir  = Join-Path $Root 'logs'
$Placeholder = "C:\inetpub\$SiteName"
$RuleName = 'ReverseProxyInboundRule1'

$script:failed = 0
function Check([string]$what, [bool]$ok, [string]$fix) {
    if ($ok) { Write-Host ("  PASS  " + $what) -ForegroundColor Green }
    else     { Write-Host ("  FAIL  " + $what + "  -> " + $fix) -ForegroundColor Red; $script:failed++ }
}
function Step([string]$msg) { Write-Host ""; Write-Host ("== " + $msg) -ForegroundColor Cyan }
function Run-Native([string]$exe, [string[]]$arguments) {
    $out = & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ("$exe failed (exit $LASTEXITCODE): " + ($out -join ' ')) }
    return $out
}

# ---------------------------------------------------------------- preflight
Step 'Preflight'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Check 'running elevated' $isAdmin 'open PowerShell with "Run as administrator" (a non-elevated token shows Administrators as deny-only)'
Check 'PowerShell 5.1 or later' ($PSVersionTable.PSVersion.Major -ge 5) 'use Windows PowerShell'

$here = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Check "script is running from the clone at $AppDir" ((Resolve-Path $here).Path -ieq $AppDir) "git clone the repo into $AppDir first, then run the script from there"
Check 'git is installed' ($null -ne (Get-Command git -ErrorAction SilentlyContinue)) 'install Git for Windows'
Check "Python at $Python" (Test-Path $Python) 'pass -Python with the right path'
Check "NSSM at $Nssm" (Test-Path $Nssm) 'pass -Nssm with the right path (it is not on PATH on this server)'

Import-Module WebAdministration -ErrorAction SilentlyContinue
Check 'IIS WebAdministration module' ($null -ne (Get-Module WebAdministration)) 'install the IIS management scripts'
if (Get-Module WebAdministration) {
    $arr = (Get-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' -Filter 'system.webServer/proxy' -Name enabled -ErrorAction SilentlyContinue)
    $arrOn = ($null -ne $arr) -and ($arr.Value -eq $true)
    Check 'ARR proxy is enabled server-wide' $arrOn 'ARR should already be on for Pinakes; investigate before continuing'
    Check "no IIS site named $SiteName yet" ($null -eq (Get-Website -Name $SiteName -ErrorAction SilentlyContinue)) 'already installed? use Update-WaccDoi.ps1'
    Check "no app pool named $SiteName yet" (-not (Test-Path "IIS:\AppPools\$SiteName")) 'pick another -SiteName'
    $clash = Get-WebBinding -ErrorAction SilentlyContinue | Where-Object { $_.bindingInformation -like "*:$HostName" }
    Check "no existing binding for $HostName" ($null -eq $clash) 'that hostname already belongs to another site'
}
Check "no service named $ServiceName yet" ($null -eq (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) 'already installed? use Update-WaccDoi.ps1'
$listening = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
Check "port $Port is free" ($null -eq $listening) 'pass -Port with an unused port (Pinakes uses 8080)'
Check "$Root has no venv yet" (-not (Test-Path $VenvDir)) 'already installed? use Update-WaccDoi.ps1'

foreach ($u in @('https://www.crossref.org/schemas/crossref5.4.0.xsd', 'https://api.crossref.org/works/10.37514/wac-j.1997.8.01.14')) {
    $ok = $false
    try { [void](Invoke-WebRequest -Uri $u -UseBasicParsing -Method Head -TimeoutSec 20); $ok = $true } catch { }
    Check "server can reach $u" $ok 'outbound HTTPS to crossref.org is blocked; ask their IT'
}
$dns = Resolve-DnsName $HostName -ErrorAction SilentlyContinue
if ($null -eq $dns) { Write-Host "  NOTE  $HostName does not resolve yet. Install can finish; the site will not be reachable until DNS exists." -ForegroundColor Yellow }

if ($script:failed -gt 0) { Write-Host ""; throw "$($script:failed) preflight check(s) failed. Nothing was changed." }
if ($Plan) { Write-Host ""; Write-Host 'Plan mode: every check passed, nothing was changed.' -ForegroundColor Green; return }

# ---------------------------------------------------------------- passphrase
Step 'Passphrase'
if (-not $AccessCode) {
    $a = Read-Host 'Choose the shared passphrase staff will type (12+ characters)' -AsSecureString
    $b = Read-Host 'Type it again' -AsSecureString
    $pa = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
    $pb = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
    if ($pa -ne $pb) { throw 'The two entries differ. Nothing was changed.' }
    $code = $pa
} else {
    $code = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($AccessCode))
}
if ($code.Length -lt 12) { throw 'Use at least 12 characters. Nothing was changed.' }

# ---------------------------------------------------------------- restore point
Step 'IIS restore point'
$restore = 'before-wacdoi-' + (Get-Date -Format 'yyyyMMdd-HHmm')
Backup-WebConfiguration -Name $restore | Out-Null
Write-Host "  created. To undo the IIS side:  Restore-WebConfiguration -Name '$restore'"

# ---------------------------------------------------------------- python
Step 'Python environment'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
[void](Run-Native $Python @('-m', 'venv', $VenvDir))
$venvPy = Join-Path $VenvDir 'Scripts\python.exe'
[void](Run-Native $venvPy @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'))
[void](Run-Native $venvPy @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $AppDir 'requirements.txt')))
[void](Run-Native $venvPy @((Join-Path $AppDir 'tools\fetch_schemas.py')))
$schemaOk = & $venvPy -c "import sys; sys.path.insert(0, r'$AppDir'); import xmlbuild; print(xmlbuild.schemas_available())"
if ($schemaOk -ne 'True') { throw 'Crossref schemas did not install; the app would refuse every download.' }
Write-Host '  venv ready, schemas installed'

# ---------------------------------------------------------------- service
Step 'Windows service'
$waitress = Join-Path $VenvDir 'Scripts\waitress-serve.exe'
[void](Run-Native $Nssm @('install', $ServiceName, $waitress, "--listen=127.0.0.1:$Port", 'app:app'))
[void](Run-Native $Nssm @('set', $ServiceName, 'AppDirectory', $AppDir))
[void](Run-Native $Nssm @('set', $ServiceName, 'DisplayName', 'WAC DOI deposit tool'))
[void](Run-Native $Nssm @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START'))
[void](Run-Native $Nssm @('set', $ServiceName, 'AppStdout', (Join-Path $LogDir 'out.log')))
[void](Run-Native $Nssm @('set', $ServiceName, 'AppStderr', (Join-Path $LogDir 'err.log')))
[void](Run-Native $Nssm @('set', $ServiceName, 'AppRotateFiles', '1'))
[void](Run-Native $Nssm @('set', $ServiceName, 'AppRotateBytes', '1048576'))
# Written straight to the registry as a REG_MULTI_SZ so no value needs shell quoting.
$envBlock = [string[]]@(
    "WACDOI_ACCESS_CODE=$code",
    'WACDOI_SECURE_COOKIES=1',
    "WACDOI_PREFIX=$Prefix",
    "WACDOI_REGISTRANT=$Registrant",
    "CROSSREF_MAILTO=$Mailto"
)
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters" -Name AppEnvironmentExtra -Value $envBlock -Type MultiString
$code = $null
Start-Service $ServiceName

$healthy = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($h.ok -eq $true -and $h.schemas -eq $true) { $healthy = $true; break }
    } catch { }
}
if (-not $healthy) { throw "Service started but /health never answered. See $LogDir\err.log. IIS has not been touched beyond the restore point." }
Write-Host "  service up on 127.0.0.1:$Port"

# The gate must be on, or the site would be open to anyone who finds the name.
$gated = $false
try { [void](Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 5) }
catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 302) { $gated = $true } }
if (-not $gated) { throw 'The passphrase gate is not active (the home page did not redirect to /login). Stopping before IIS exposes it.' }
Write-Host '  passphrase gate confirmed (home page redirects to /login)'

# ---------------------------------------------------------------- IIS
Step 'IIS site and reverse proxy'
New-Item -ItemType Directory -Force -Path $Placeholder | Out-Null
New-WebAppPool -Name $SiteName | Out-Null
Set-ItemProperty "IIS:\AppPools\$SiteName" -Name managedRuntimeVersion -Value ''
New-Website -Name $SiteName -PhysicalPath $Placeholder -ApplicationPool $SiteName -Port 443 -HostHeader $HostName -Ssl | Out-Null

# Rule scoped to THIS site only. A server-wide change would affect every other site.
$loc = @{ PSPath = 'MACHINE/WEBROOT/APPHOST'; Location = $SiteName }
Add-WebConfigurationProperty @loc -Filter 'system.webServer/rewrite/rules' -Name '.' -Value @{ name = $RuleName; patternSyntax = 'Regular Expressions'; stopProcessing = 'True' }
$rule = "system.webServer/rewrite/rules/rule[@name='$RuleName']"
Set-WebConfigurationProperty @loc -Filter "$rule/match"  -Name url  -Value '(.*)'
Set-WebConfigurationProperty @loc -Filter "$rule/action" -Name type -Value 'Rewrite'
Set-WebConfigurationProperty @loc -Filter "$rule/action" -Name url  -Value "http://127.0.0.1:$Port/{R:1}"
Write-Host '  site, app pool and rewrite rule created'
Get-WebBinding -Name $SiteName | Format-Table protocol, bindingInformation, sslFlags -AutoSize | Out-String | Write-Host

# ---------------------------------------------------------------- done
Step 'Done'
Write-Host "Check through IIS with real TLS and the right name (works even before DNS exists):"
Write-Host "  curl.exe --resolve ${HostName}:443:127.0.0.1 https://$HostName/health"
Write-Host "Then browse to https://$HostName/ and sign in with the passphrase."
Write-Host "Logs: $LogDir     Update later with: $AppDir\deploy\windows\Update-WaccDoi.ps1"
