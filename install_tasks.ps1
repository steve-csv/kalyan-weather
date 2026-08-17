<#
    Registers the two Windows scheduled tasks that run the forecasting agent.

        Kalyan Weather Agent - Daily    06:15 every day
        Kalyan Weather Agent - Weekly   07:00 every Sunday

    Run this once, from this folder:

        powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1

    To remove them again:

        powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1 -Uninstall

    Neither task needs administrator rights - they are registered under your own
    user account and run only when you are logged on.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$DailyTime  = "06:15",
    [string]$WeeklyTime = "07:00",
    [string]$WeeklyDay  = "Sunday"
)

$ErrorActionPreference = "Stop"

$Root       = $PSScriptRoot
$DailyName  = "Kalyan Weather Agent - Daily"
$WeeklyName = "Kalyan Weather Agent - Weekly"

function Remove-AgentTask([string]$Name) {
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  removed: $Name"
    }
}

if ($Uninstall) {
    Write-Host "Removing scheduled tasks..."
    Remove-AgentTask $DailyName
    Remove-AgentTask $WeeklyName
    Write-Host "Done. The scripts and past bulletins are untouched."
    return
}

# --- locate python --------------------------------------------------------
# Windows ships zero-byte "app execution alias" stubs in WindowsApps that
# forward to the Microsoft Store. Those work interactively but can fail or
# launch the Store when invoked from Task Scheduler, so they are rejected here.
function Test-RealExe([string]$Path) {
    if (-not $Path) { return $false }
    if (-not (Test-Path $Path)) { return $false }
    return (Get-Item $Path).Length -gt 0
}

$python = $null

# Best source of truth: ask the interpreter that actually runs where it lives.
try {
    $resolved = & python -c "import sys, os; print(os.path.join(os.path.dirname(os.path.realpath(sys.executable)), 'pythonw.exe'))" 2>$null
    if (Test-RealExe $resolved) { $python = $resolved }
    if (-not $python) {
        $resolved = & python -c "import sys, os; print(os.path.realpath(sys.executable))" 2>$null
        if (Test-RealExe $resolved) { $python = $resolved }
    }
} catch { }

# Fallback: PATH lookup, skipping the stubs.
if (-not $python) {
    foreach ($name in @("pythonw.exe", "python.exe")) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if (Test-RealExe $cmd.Source) { $python = $cmd.Source; break }
        }
        if ($python) { break }
    }
}

if (-not $python) {
    throw "No usable Python found. The WindowsApps entries are zero-byte Store aliases, not interpreters. Install Python from python.org and re-run."
}

Write-Host "Using interpreter: $python"
if ($python -notmatch "pythonw") {
    Write-Warning "pythonw.exe not available - a brief console window will appear when each task runs."
}

Write-Host "Project root:      $Root"
Write-Host ""

# --- daily ----------------------------------------------------------------
Remove-AgentTask $DailyName

$dailyAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m wxagent daily" `
    -WorkingDirectory $Root

$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $DailyTime

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $DailyName `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -Settings $settings `
    -Description "Daily rain bulletin for Kalyan West. Writes a markdown file to .\forecasts and raises a desktop notification." | Out-Null

Write-Host "  registered: $DailyName  (daily at $DailyTime)"

# --- weekly ---------------------------------------------------------------
Remove-AgentTask $WeeklyName

$weeklyAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m wxagent weekly" `
    -WorkingDirectory $Root

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyDay -At $WeeklyTime

Register-ScheduledTask `
    -TaskName $WeeklyName `
    -Action $weeklyAction `
    -Trigger $weeklyTrigger `
    -Settings $settings `
    -Description "Weekly Mumbai MMR wind-pattern outlook. Writes a markdown file to .\forecasts and raises a desktop notification." | Out-Null

Write-Host "  registered: $WeeklyName  ($WeeklyDay at $WeeklyTime)"
Write-Host ""

# --- StartWhenAvailable note ---------------------------------------------
Write-Host "Both tasks use -StartWhenAvailable, so a run missed because the PC"
Write-Host "was off will fire shortly after you next log on."
Write-Host ""
Write-Host "Verify with:   Get-ScheduledTask -TaskName 'Kalyan Weather Agent*'"
Write-Host "Run one now:   Start-ScheduledTask -TaskName '$DailyName'"
Write-Host "Bulletins land in: $(Join-Path $Root 'forecasts')"
