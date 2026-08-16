# Luminque Onboarding & Watchdog — Phase 1 Technical Design

**Status:** Draft  
**Date:** 2026-05-28  
**Scope:** Watchdog bug fixes, onboarding UI, device enrollment, Task Scheduler registration

---

## 1. Overview

This document covers two modules:

**Watchdog** (`luminque/watchdog/__init__.py`) — mostly implemented, four specific
bugs to fix before it works correctly on Windows.

**Onboarding** (`luminque/onboarding/`) — the `run()` entry point is a stub.
The tkinter UI, device enrollment, and Task Scheduler registration all need to
be built. Two new files are required.

---

## 2. Watchdog — Complete File

Four bugs in the current implementation:

1. `_start_capture()` passes `-m luminque` which is wrong when running as a
   PyInstaller exe. `sys.executable` is already `luminque.exe` — only
   `--capture` is needed.
2. `_start_capture()` is missing `creationflags`. Without
   `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, the capture process may die
   when the watchdog process exits.
3. No `proc.kill()` fallback after `proc.terminate()` times out — if capture
   hangs on shutdown the watchdog stalls.
4. No log file handler — `logger` is defined but logs go nowhere.

Replace `luminque/watchdog/__init__.py` with:

```python
"""
luminque.watchdog — keeps capture alive; handles memory drift.

Runs as a one-shot check every 5 minutes via Task Scheduler.
"""

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "Luminque" / "watchdog.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

RSS_LIMIT_BYTES = 500 * 1024 * 1024   # 500 MB


def _find_capture_process():
    """Return psutil.Process if capture is running, else None."""
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                exe = proc.info["exe"] or ""
                cmdline = proc.info["cmdline"] or []
                if "luminque" in exe.lower() and "--capture" in cmdline:
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        logger.warning(f"Error scanning processes: {e}")
    return None


def _start_capture() -> None:
    subprocess.Popen(
        [sys.executable, "--capture"],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def _kill_and_restart(proc) -> None:
    import psutil
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except psutil.TimeoutExpired:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass
    _start_capture()


def _is_midnight_window() -> bool:
    now = datetime.now()
    return now.hour == 0 and now.minute < 5


def run() -> None:
    """Run one watchdog check cycle."""
    import psutil

    proc = _find_capture_process()

    if proc is None:
        logger.info("Capture not found — starting")
        _start_capture()
        return

    try:
        rss = proc.memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        logger.info("Capture disappeared during check — restarting")
        _start_capture()
        return

    if rss > RSS_LIMIT_BYTES:
        logger.warning(f"Capture RSS {rss // (1024 * 1024)}MB > 500MB — restarting")
        _kill_and_restart(proc)
        return

    if _is_midnight_window():
        logger.info("Midnight window — daily restart")
        _kill_and_restart(proc)
        return

    logger.info(f"Capture healthy: PID={proc.pid}, RSS={rss // (1024 * 1024)}MB")
```

---

## 3. Onboarding — New File: `luminque/onboarding/enrollment.py`

Calls `POST /api/v1/devices/enroll`, gets back credentials, and stores them in
Windows Credential Manager under the same service name and keys that the sender
reads from (`luminque-sender`).

```python
"""
luminque.onboarding.enrollment — device enrollment against the Luminque server.

Calls POST /api/v1/devices/enroll and persists the returned credentials in
Windows Credential Manager so the sender can read them on every run.
"""

import platform
import socket

import keyring
import requests

# Must match the service name and key names in luminque/sender/credentials.py
KEYRING_SERVICE = "luminque-sender"
KEYRING_KEYS = {
    "auth_token":   "luminque_api_key",
    "endpoint_url": "luminque_endpoint_url",
    "tenant_id":    "luminque_tenant_id",
}


def enroll_device(api_url: str, enrollment_token: str) -> dict:
    """
    POST /api/v1/devices/enroll and store returned credentials in keyring.

    Raises RuntimeError with a user-readable message on any failure.
    Returns the full response dict on success.
    """
    payload = {
        "hostname":          socket.gethostname(),
        "platform":          "windows",
        "os_version":        _get_os_version(),
        "enrollment_token":  enrollment_token,
    }

    try:
        resp = requests.post(
            f"{api_url.rstrip('/')}/api/v1/devices/enroll",
            json=payload,
            timeout=30,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Could not connect to {api_url}.\n\n"
            f"Check the server URL and your network connection.\n\n({exc})"
        ) from exc
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Connection to {api_url} timed out. Check the server URL and try again."
        )

    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Enrollment failed (HTTP {resp.status_code}):\n{detail}")

    data = resp.json()

    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["auth_token"],   data["auth_token"])
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["endpoint_url"], api_url)
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["tenant_id"],    data["tenant_id"])

    return data


def _get_os_version() -> str:
    try:
        return f"{platform.release()} ({platform.version()})"
    except Exception:
        return "unknown"
```

---

## 4. Onboarding — New File: `luminque/onboarding/scheduler.py`

Registers and deregisters the three Windows Scheduled Tasks via `schtasks.exe`.
No admin elevation required — tasks run as the current user.

```python
"""
luminque.onboarding.scheduler — Windows Task Scheduler registration.

All tasks run as the current user (/RU "") with no elevation (/RL LIMITED).
/F overwrites any existing task with the same name, making re-runs of
onboarding safe.
"""

import subprocess

TASK_NAMES = {
    "capture":  "LumniqueCapture",
    "sender":   "LumniqueSender",
    "watchdog": "LumniqueWatchdog",
}


def register_all_tasks(exe_path: str) -> None:
    _register_capture(exe_path)
    _register_sender(exe_path)
    _register_watchdog(exe_path)


def deregister_all_tasks() -> None:
    for name in TASK_NAMES.values():
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True,
        )


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )


def _register_capture(exe_path: str) -> None:
    """Start at every login. 30-second delay avoids boot-time contention."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["capture"],
        "/TR", f'"{exe_path}" --capture',
        "/SC", "ONLOGON",
        "/RU", "",
        "/RL", "LIMITED",
        "/DELAY", "0000:30",
    ])


def _register_sender(exe_path: str) -> None:
    """Run every 45 minutes."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["sender"],
        "/TR", f'"{exe_path}" --send',
        "/SC", "MINUTE",
        "/MO", "45",
        "/RU", "",
        "/RL", "LIMITED",
    ])


def _register_watchdog(exe_path: str) -> None:
    """Run every 5 minutes."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["watchdog"],
        "/TR", f'"{exe_path}" --watchdog',
        "/SC", "MINUTE",
        "/MO", "5",
        "/RU", "",
        "/RL", "LIMITED",
    ])
```

---

## 5. Onboarding — Complete File: `luminque/onboarding/__init__.py`

Replace the current stub with the full implementation. The flow is:

```
run()
  → consent screen  (Cancel → exit, I Agree → server setup screen)
  → server setup screen (Back → consent, Connect → _on_connect)
  → _on_connect()
      → validate inputs
      → install_exe()           copy exe to stable path
      → enroll_device()         POST to server, store credentials
      → register_all_tasks()    schtasks
      → _create_stop_shortcut() desktop shortcut
      → _start_capture_now()    detached subprocess
      → "Setup complete" dialog
```

```python
"""
luminque.onboarding — tkinter UI for first-run consent and task registration.

Shown when luminque.exe is double-clicked (no CLI arguments).
"""

import logging
import os
import shutil
import subprocess
import sys

import tkinter as tk
from tkinter import messagebox

logger = logging.getLogger(__name__)

CONSENT_TEXT = (
    "What we collect:\n"
    "  • Screenshots of your screen activity\n"
    "  • Keyboard and mouse interaction metadata\n"
    "  • Window titles and active application\n\n"
    "Why:\n"
    "  This data is used to understand how you work so we can\n"
    "  build tools that help you do it faster.\n\n"
    "Your data is stored locally and sent securely to Luminque servers.\n\n"
    'By clicking "I Agree", you consent to this data collection.'
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    root = tk.Tk()
    root.title("Luminque — Setup")
    root.geometry("480x340")
    root.resizable(False, False)
    _center(root, 480, 340)
    _show_consent(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# Screen 1 — Consent
# ---------------------------------------------------------------------------

def _show_consent(root: tk.Tk) -> None:
    _clear(root)
    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(frame, text="Luminque — Data Collection Setup",
             font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))
    tk.Label(frame, text=CONSENT_TEXT, justify=tk.LEFT,
             wraplength=440, font=("Segoe UI", 9)).pack(anchor="w")

    btn = tk.Frame(frame)
    btn.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))
    tk.Button(btn, text="Cancel", width=10,
              command=root.destroy).pack(side=tk.LEFT)
    tk.Button(btn, text="I Agree", width=10, default=tk.ACTIVE,
              command=lambda: _show_server_setup(root)).pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# Screen 2 — Server setup
# ---------------------------------------------------------------------------

def _show_server_setup(root: tk.Tk) -> None:
    _clear(root)
    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(frame, text="Luminque — Server Setup",
             font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))

    tk.Label(frame, text="Server URL:", font=("Segoe UI", 9)).pack(anchor="w")
    url_var = tk.StringVar()
    tk.Entry(frame, textvariable=url_var, width=52).pack(anchor="w", pady=(2, 10))

    tk.Label(frame, text="Enrollment token:", font=("Segoe UI", 9)).pack(anchor="w")
    token_var = tk.StringVar()
    tk.Entry(frame, textvariable=token_var, show="*", width=52).pack(anchor="w", pady=(2, 0))

    btn = tk.Frame(frame)
    btn.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))
    tk.Button(btn, text="Back", width=10,
              command=lambda: _show_consent(root)).pack(side=tk.LEFT)
    tk.Button(btn, text="Connect", width=10, default=tk.ACTIVE,
              command=lambda: _on_connect(
                  root, url_var.get().strip(), token_var.get().strip()
              )).pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# Connect handler
# ---------------------------------------------------------------------------

def _on_connect(root: tk.Tk, api_url: str, enrollment_token: str) -> None:
    if not api_url:
        messagebox.showerror("Missing Field", "Please enter the server URL.")
        return
    if len(enrollment_token) < 32:
        messagebox.showerror(
            "Missing Field",
            "Enrollment token must be at least 32 characters.",
        )
        return

    root.withdraw()
    try:
        exe_path = install_exe()

        from luminque.onboarding.enrollment import enroll_device
        enroll_device(api_url=api_url, enrollment_token=enrollment_token)

        from luminque.onboarding.scheduler import register_all_tasks
        register_all_tasks(exe_path)

        _create_stop_shortcut(exe_path)
        _start_capture_now(exe_path)

        messagebox.showinfo(
            "Luminque",
            "Setup complete.\n\n"
            "Luminque is now running in the background.\n"
            "It will start automatically each time you log in.",
        )
    except Exception as e:
        messagebox.showerror("Setup Failed", str(e))
    finally:
        root.destroy()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def install_exe() -> str:
    """
    Return the path to luminque.exe at its stable installed location.

    With the SFX installer, the exe is already at the target path —
    this function is a no-op in that case. It handles the edge case where
    the exe is run from a different location (e.g. during development).
    """
    src = sys.executable
    dst_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Luminque")
    dst = os.path.join(dst_dir, "luminque.exe")

    if os.path.abspath(src).lower() == os.path.abspath(dst).lower():
        return dst

    # Edge case: running from outside the install directory.
    # Copy the entire parent directory so all DLLs come with it.
    src_dir = os.path.dirname(src)
    if os.path.exists(dst_dir):
        shutil.rmtree(dst_dir)
    shutil.copytree(src_dir, dst_dir)
    return dst


def _start_capture_now(exe_path: str) -> None:
    subprocess.Popen(
        [exe_path, "--capture"],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def _create_stop_shortcut(exe_path: str) -> None:
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    shortcut = os.path.join(desktop, "Stop Luminque.lnk")
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{shortcut}"); '
        f'$s.TargetPath = "{exe_path}"; '
        f'$s.Arguments = "--stop"; '
        f'$s.Description = "Stop all Luminque processes and scheduled tasks"; '
        f'$s.Save()'
    )
    try:
        subprocess.run(["powershell", "-Command", ps], capture_output=True, check=True)
    except Exception as e:
        logger.warning(f"Could not create Stop shortcut: {e}")


def _clear(root: tk.Tk) -> None:
    for widget in root.winfo_children():
        widget.destroy()


def _center(win: tk.Tk, w: int, h: int) -> None:
    win.update_idletasks()
    x = (win.winfo_screenwidth() - w) // 2
    y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"{w}x{h}+{x}+{y}")
```

---

## 6. Implementation Locations

| File | What to do |
|---|---|
| `luminque/watchdog/__init__.py` | Replace entire file with the implementation in §2. |
| `luminque/onboarding/enrollment.py` | Create new file with the implementation in §3. |
| `luminque/onboarding/scheduler.py` | Create new file with the implementation in §4. |
| `luminque/onboarding/__init__.py` | Replace entire file with the implementation in §5. |

---

## 7. Notes for Implementation

**Keyring alignment:** `enrollment.py` writes credentials under service name
`luminque-sender` with keys `luminque_api_key`, `luminque_endpoint_url`,
`luminque_tenant_id`. These must match exactly what `luminque/sender/credentials.py`
reads. Do not change the key names in either file without updating both.

**enrollment_token length:** The 32-character minimum is validated in the UI
before the network call. The server may impose its own stricter validation —
any server-side rejection surfaces as a `RuntimeError` from `enroll_device()`
and is shown to the user in the "Setup Failed" dialog.

**Re-running onboarding:** `schtasks /Create /F` overwrites existing tasks
safely. `install_exe()` skips the copy if already at the target path. Running
onboarding a second time is safe.

**Testing without a live server:** To test the install flow (Task Scheduler
registration, shortcut creation, capture starting) without a server, temporarily
stub `enroll_device()` to return a hardcoded dict and call
`configure_credentials()` directly with test values. Remove the stub before
shipping.
