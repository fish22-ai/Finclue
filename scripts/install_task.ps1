# Installs a Windows Task Scheduler entry that runs scripts\daily.bat every N days.
# Missed runs (PC off at trigger time) are launched once after the machine is
# available again thanks to StartWhenAvailable.
# Runs as the current user, interactive only, least privilege.
# No password/secret is stored anywhere.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -DaysInterval 2 -At "09:30"
#
# WHEN it runs is set here and nowhere else -- config.yaml has no schedule key,
# so there is no second place to keep in sync.
# The trigger uses WINDOWS LOCAL TIME, so the machine should stay on
# China Standard Time (Asia/Shanghai).
#
# NOTE ON pool_ttl_days: config.yaml's xhs_socai.pool_ttl_days MUST be larger than
# -DaysInterval, otherwise the candidate pool is judged stale on every single run
# and the whole pool-reuse feature silently does nothing. The default of 3 days
# pairs with pool_ttl_days: 4. If you change -DaysInterval, re-check that value.

param(
    [string]$TaskName     = 'CareerIntel',
    [int]$DaysInterval    = 3,
    [string]$At           = '10:00',
    [string]$RepoRoot     = ''
)

$ErrorActionPreference = 'Stop'

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
$RepoRoot  = (Resolve-Path $RepoRoot).Path
$BatchFile = Join-Path $PSScriptRoot 'daily.bat'
$VbsLauncher = Join-Path $PSScriptRoot 'run_hidden.vbs'
$ConfigYaml = Join-Path $RepoRoot 'config.yaml'

if ($At -notmatch '^\d{1,2}:\d{2}$') { throw ('-At format must be HH:MM, got: ' + $At) }
if ($DaysInterval -lt 1) { throw '-DaysInterval must be >= 1' }

Write-Host ('Repo      : ' + $RepoRoot)
Write-Host ('Task      : ' + $TaskName)
Write-Host ('Schedule  : every ' + $DaysInterval + ' day(s) at ' + $At + '  (Windows local time)')
Write-Host ''

# --- sanity checks ----------------------------------------------------------
if (-not (Test-Path -LiteralPath $BatchFile))  { throw ('daily.bat not found: ' + $BatchFile) }
if (-not (Test-Path -LiteralPath $ConfigYaml)) { throw ('config.yaml not found: ' + $ConfigYaml) }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Warning 'python not found on PATH - scheduled runs will fail.'
}
$socai = 'C:\Users\吃鱿鱼的鱿鱼\.socai\bin\socai.exe'
if (-not (Test-Path -LiteralPath $socai)) {
    Write-Warning ('socai.exe not found at ' + $socai + ' - scheduled runs will fail.')
} else {
    Write-Host 'socai     : found'
}

# The LLM key lives in cc-switch.db, not in an env var (see src/llm.py).
$ccdb = Join-Path $env:USERPROFILE '.cc-switch\cc-switch.db'
if (-not (Test-Path -LiteralPath $ccdb)) {
    Write-Warning ('cc-switch.db not found at ' + $ccdb + ' - LLM calls will fail.')
} else {
    Write-Host 'cc-switch : found'
}

# --- pool_ttl_days vs DaysInterval ------------------------------------------
# The one cross-file rule that silently breaks the pool feature if violated.
$ttl = $null
foreach ($line in (Get-Content -LiteralPath $ConfigYaml -Encoding UTF8)) {
    $t = $line.Trim()
    if ($t.StartsWith('#') -or $t -eq '') { continue }
    if ($t -match '^pool_ttl_days\s*:\s*(\d+)') { $ttl = [int]$Matches[1]; break }
}
if ($null -eq $ttl) {
    Write-Warning 'config.yaml has no pool_ttl_days - pool reuse will use the built-in default.'
} elseif ($ttl -le $DaysInterval) {
    Write-Warning ('pool_ttl_days (' + $ttl + ') <= run interval (' + $DaysInterval + ' days).')
    Write-Warning '  The pool would be stale on every run and pool reuse would do nothing.'
    Write-Warning ('  Set pool_ttl_days to something > ' + $DaysInterval + ' (currently 4 is intended).')
} else {
    Write-Host ('pool TTL  : ' + $ttl + ' days (run interval ' + $DaysInterval + ' -> a re-search every ' +
                [math]::Ceiling($ttl / $DaysInterval) + ' run(s))')
}

$tz = (Get-TimeZone).Id
if ($tz -notmatch 'China') {
    Write-Warning ('Machine timezone is ' + $tz + ' (not China Standard Time).')
    Write-Warning '  The trigger uses Windows local time, so ' + $At + ' means ' + $At + ' in that zone.'
}
Write-Host ''

# --- build the task ---------------------------------------------------------
# First run is TOMORROW at -At, so the 3-day cadence is anchored to a known date
# instead of to whenever this script happened to be run.
$hm = $At -split ':'
$start = (Get-Date).Date.AddDays(1).AddHours([int]$hm[0]).AddMinutes([int]$hm[1])

$trigger = $null
try {
    # -DaysInterval exists on newer Windows builds; fall back to setting the
    # property directly when it does not.
    $trigger = New-ScheduledTaskTrigger -Daily -DaysInterval $DaysInterval -At $start -ErrorAction Stop
} catch {
    $trigger = New-ScheduledTaskTrigger -Daily -At $start
    $trigger.DaysInterval = $DaysInterval
}

$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument ('//B "' + $VbsLauncher + '" "' + $BatchFile + '"') `
    -WorkingDirectory $RepoRoot

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$desc = 'Career Intelligence: run scripts\daily.bat every ' + $DaysInterval +
        ' day(s) at ' + $At + '; catches up after boot when the PC was off.'

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description $desc `
    -Force | Out-Null

Write-Host 'Registered/updated task.'
Write-Host ''
Write-Host 'Verify / run / remove:'
Write-Host ('  MSYS_NO_PATHCONV=1 schtasks /query /tn ' + $TaskName + ' /v   (Git Bash)')
Write-Host ('  schtasks /query /tn ' + $TaskName + ' /v                     (cmd)')
Write-Host ('  schtasks /run   /tn ' + $TaskName)
Write-Host ('  Unregister-ScheduledTask -TaskName ' + $TaskName + ' -Confirm:$false')
Write-Host ''
Write-Host ('Log: ' + (Join-Path $RepoRoot 'data\logs\cron.log'))
