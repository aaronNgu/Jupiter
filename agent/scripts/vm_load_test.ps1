# vm_load_test.ps1 — synthetic activity to exercise the captureV2 agent
# without a human present. For VM testing only.
#
# WHY TYPING, NOT JUST MOUSE:
#   captureV2 keeps a screenshot only when the screen PIXELS change (dhash
#   dedupe), and the mouse cursor is NOT in the capture (CAPTUREBLT disabled).
#   So moving the mouse alone passes the activity gate but every frame is
#   deduped -> zero saved screenshots. Typing into Notepad changes real pixels
#   AND generates the keyboard events pynput needs. Mouse movement is added
#   only to vary the activity stream.
#
# TWO MODES:
#   (default)      Opens Notepad and types — self-contained: supplies BOTH the
#                  input activity AND the screen changes capture needs.
#   -ActivityOnly  No Notepad, no typing. Just a harmless {F15} keypress +
#                  cursor nudge each tick to hold the activity gate open, while
#                  something ELSE on screen (e.g. a playing video) supplies the
#                  pixel changes. Use this when you want to drive capture with a
#                  video: the video alone would stop capture after ~5 s because
#                  it generates no input, so this keeps the activity gate open
#                  without stealing focus from the player.
#
# RUN (from Git Bash / MINGW64 in the repo root):
#   powershell.exe -ExecutionPolicy Bypass -File scripts/vm_load_test.ps1
#   powershell.exe -ExecutionPolicy Bypass -File scripts/vm_load_test.ps1 -ActivityOnly
#   powershell.exe -ExecutionPolicy Bypass -File scripts/vm_load_test.ps1 -DurationMinutes 60 -IntervalSeconds 3
#
# STOP EARLY: close the PowerShell window, or Ctrl+C.

param(
    [int]$DurationMinutes = 60,
    [int]$IntervalSeconds = 4,
    [switch]$ActivityOnly
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Cursor { [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y); }
"@
Add-Type -AssemblyName System.Windows.Forms

$rand = New-Object System.Random
$end  = (Get-Date).AddMinutes($DurationMinutes)
$i = 0

if ($ActivityOnly) {
    Write-Host "Activity-only: holding the activity gate open for $DurationMinutes min."
    Write-Host "Make sure something is changing the screen (e.g. a playing video)."
    while ((Get-Date) -lt $end) {
        [Cursor]::SetCursorPos($rand.Next(100, 900), $rand.Next(100, 700)) | Out-Null
        # {F15} is a no-op key in virtually every app but still registers as a
        # real keyboard event for the activity gate — and does not steal focus.
        [System.Windows.Forms.SendKeys]::SendWait("{F15}")
        $i++
        Start-Sleep -Seconds $IntervalSeconds
    }
    Write-Host "Done: $i activity pulses over $DurationMinutes min."
}
else {
    # Notepad gives us a window whose content visibly changes as we type, so
    # captured frames are not all deduped away.
    $np = Start-Process notepad -PassThru
    Start-Sleep -Seconds 2
    $wsh = New-Object -ComObject WScript.Shell
    Write-Host "Load test: typing every $IntervalSeconds s for $DurationMinutes min (Ctrl+C to stop)..."
    while ((Get-Date) -lt $end) {
        # Keep Notepad focused so SendKeys lands there (focus can be stolen).
        $wsh.AppActivate($np.Id) | Out-Null
        # Move the cursor (varies the activity stream; not captured itself).
        [Cursor]::SetCursorPos($rand.Next(100, 900), $rand.Next(100, 700)) | Out-Null
        # Type a line -> real screen change -> a frame survives dedupe.
        [System.Windows.Forms.SendKeys]::SendWait("Luminque load test line $i {ENTER}")
        $i++
        Start-Sleep -Seconds $IntervalSeconds
    }
    Write-Host "Done: $i typing events over $DurationMinutes min."
}
