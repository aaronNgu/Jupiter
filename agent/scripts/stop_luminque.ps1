# stop_luminque.ps1 — stop Luminque and (re)create the "Stop Luminque"
# desktop shortcut, WITHOUT depending on a working luminque.exe.
#
# WHY THIS EXISTS (the exe can stop itself — usually):
#   `luminque.exe --stop` (luminque/stop/__init__.py) already kills the three
#   background processes and deletes the three scheduled tasks, and onboarding
#   drops a desktop shortcut that runs it. But onboarding's install_exe() does
#   `shutil.rmtree()` on %LOCALAPPDATA%\Programs\Luminque before re-copying,
#   and that rmtree FAILS with [WinError 5] if a previous capture process still
#   has a DLL loaded (PIL\_imaging...pyd is the usual casualty). A failed
#   rmtree leaves the install half-deleted: luminque.exe may still be sitting
#   there, but its _internal\ directory is gutted, so the exe — and therefore
#   the Stop Luminque shortcut — will not run. That is exactly when you most
#   need a way to stop the thing. This script uses only schtasks, taskkill, and
#   file removal, so it works against a broken install.
#
# ORDER MATTERS — TASKS + AUTOSTART FIRST, THEN PROCESSES:
#   Two things can resurrect capture: the watchdog TASK (fires every 5 min and
#   relaunches capture) and the capture autostart shortcut in the Startup
#   folder (fires at next login). Delete the tasks AND remove the autostart
#   shortcut before killing processes, so nothing can undo the kill sweep.
#   Within the process kill, watchdog still goes first for the same reason.
#
# TASK NAMES ARE MISSPELLED ON PURPOSE:
#   They are registered as "Lumnique*" (not "Luminque*") — see
#   luminque/onboarding/scheduler.py TASK_NAMES. These strings must match that
#   file exactly or the delete silently no-ops.
#
# RUN (from Git Bash / MINGW64 in the repo root):
#   powershell.exe -ExecutionPolicy Bypass -File scripts/stop_luminque.ps1
#   powershell.exe -ExecutionPolicy Bypass -File scripts/stop_luminque.ps1 -ShortcutOnly
#   powershell.exe -ExecutionPolicy Bypass -File scripts/stop_luminque.ps1 -NoShortcut
#
# No admin/UAC needed: the tasks are per-user (/RL LIMITED) and the processes
# run as the current user.

param(
    # Only (re)create the desktop shortcut; do not stop anything.
    [switch]$ShortcutOnly,
    # Stop everything but do not touch the desktop shortcut.
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

# Scheduled tasks. LumniqueCapture is legacy — capture now autostarts from a
# Startup-folder shortcut, not a task — but it stays here so this script also
# cleans up machines upgraded from a build that registered the capture task.
$TaskNames = @("LumniqueWatchdog", "LumniqueCapture", "LumniqueSender")
# Must match luminque/stop/__init__.py STOP_FLAGS.
$StopFlags = @("--watchdog", "--capture", "--send")

$ExePath      = Join-Path $env:LOCALAPPDATA "Programs\Luminque\luminque.exe"
$ShortcutPath = Join-Path $env:USERPROFILE "Desktop\Stop Luminque.lnk"
# Must match CAPTURE_SHORTCUT_NAME / startup_dir() in scheduler.py.
$CaptureAutostart = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Luminque Capture.lnk"


function Remove-LuminqueTasks {
    Write-Host "`n=== Deleting scheduled tasks ===" -ForegroundColor Cyan
    foreach ($name in $TaskNames) {
        # 2>&1 | Out-Null: schtasks writes "cannot find the file specified" to
        # stderr when the task is already gone, which is a success for us.
        $null = schtasks /Delete /F /TN $name 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  deleted  $name"
        } else {
            Write-Host "  absent   $name" -ForegroundColor DarkGray
        }
    }
}


function Remove-CaptureAutostart {
    Write-Host "`n=== Capture autostart shortcut ===" -ForegroundColor Cyan
    if (Test-Path $CaptureAutostart) {
        Remove-Item $CaptureAutostart -Force
        Write-Host "  removed  $CaptureAutostart"
    } else {
        Write-Host "  absent   $CaptureAutostart" -ForegroundColor DarkGray
    }
}


function Stop-LuminqueProcesses {
    Write-Host "`n=== Killing processes ===" -ForegroundColor Cyan
    $me = $PID
    # Match the same way luminque/stop does: a luminque image running with one
    # of the mode flags. This deliberately spares an onboarding/GUI process
    # (no flag) and this shell.
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'luminque.exe'" |
        Where-Object { $_.ProcessId -ne $me -and $_.CommandLine } |
        Where-Object {
            $cmd = $_.CommandLine
            ($StopFlags | Where-Object { $cmd -like "*$_*" }).Count -gt 0
        }

    if (-not $procs) {
        Write-Host "  none running" -ForegroundColor DarkGray
        return
    }

    # Watchdog first so it cannot restart capture mid-sweep.
    $ordered = $procs | Sort-Object @{ Expression = {
        if ($_.CommandLine -like "*--watchdog*") { 0 } else { 1 }
    }}

    foreach ($p in $ordered) {
        $mode = ($StopFlags | Where-Object { $p.CommandLine -like "*$_*" }) -join ","
        $null = taskkill /PID $p.ProcessId /F /T 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  killed   pid $($p.ProcessId)  [$mode]"
        } else {
            Write-Host "  FAILED   pid $($p.ProcessId)  [$mode]" -ForegroundColor Red
        }
    }
}


function New-StopShortcut {
    Write-Host "`n=== Desktop shortcut ===" -ForegroundColor Cyan
    # Same properties onboarding's _create_stop_shortcut() sets.
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($ShortcutPath)
    $s.TargetPath   = $ExePath
    $s.Arguments    = "--stop"
    $s.Description  = "Stop all Luminque processes and scheduled tasks"
    $s.Save()
    Write-Host "  created  $ShortcutPath"
    Write-Host "  target   $ExePath --stop"

    if (-not (Test-Path $ExePath)) {
        Write-Host "  WARNING: that exe does not exist — the shortcut will not" -ForegroundColor Yellow
        Write-Host "           work until Luminque is installed there." -ForegroundColor Yellow
    } elseif (-not (Test-Path (Join-Path (Split-Path $ExePath) "_internal"))) {
        Write-Host "  WARNING: exe is present but _internal\ is missing — this is" -ForegroundColor Yellow
        Write-Host "           the half-deleted state a failed setup leaves behind." -ForegroundColor Yellow
        Write-Host "           The exe will not launch. Re-run the installer." -ForegroundColor Yellow
    }
}


function Show-State {
    Write-Host "`n=== Remaining state ===" -ForegroundColor Cyan
    $left = Get-CimInstance Win32_Process -Filter "Name = 'luminque.exe'" |
        Where-Object { $_.ProcessId -ne $PID }
    if ($left) {
        Write-Host "  luminque.exe still running:" -ForegroundColor Yellow
        $left | ForEach-Object { Write-Host "    pid $($_.ProcessId)  $($_.CommandLine)" }
    } else {
        Write-Host "  no luminque.exe processes" -ForegroundColor Green
    }

    $tasksLeft = @()
    foreach ($name in $TaskNames) {
        $null = schtasks /Query /TN $name 2>&1
        if ($LASTEXITCODE -eq 0) { $tasksLeft += $name }
    }
    if ($tasksLeft) {
        Write-Host "  tasks still registered: $($tasksLeft -join ', ')" -ForegroundColor Yellow
    } else {
        Write-Host "  no Luminque scheduled tasks" -ForegroundColor Green
    }

    if (Test-Path $CaptureAutostart) {
        Write-Host "  capture autostart shortcut still present" -ForegroundColor Yellow
    } else {
        Write-Host "  no capture autostart shortcut" -ForegroundColor Green
    }
}


if (-not $ShortcutOnly) {
    # Tasks and the autostart shortcut go first, then processes: with nothing
    # left to relaunch capture, the kill sweep cannot be undone mid-run.
    Remove-LuminqueTasks
    Remove-CaptureAutostart
    Stop-LuminqueProcesses
}
if (-not $NoShortcut) {
    New-StopShortcut
}
if (-not $ShortcutOnly) {
    Show-State
    Write-Host "`nSafe to re-run setup now.`n" -ForegroundColor Green
}
