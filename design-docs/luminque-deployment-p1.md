# Luminque Deployment — Phase 1 Technical Design

**Status:** Draft  
**Date:** 2026-05-05  
**Scope:** Single signed `.exe`, self-install onboarding, Task Scheduler process management, watchdog, build pipeline

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Main Entry Point](#2-main-entry-point)
3. [Onboarding UI](#3-onboarding-ui)
4. [Task Scheduler Registration](#4-task-scheduler-registration)
5. [Watchdog Logic](#5-watchdog-logic)
6. [PyInstaller .spec File](#6-pyinstaller-spec-file)
7. [GitHub Actions Build Pipeline](#7-github-actions-build-pipeline)
8. [Code Signing](#8-code-signing)
9. [Directory Structure on User Machine](#9-directory-structure-on-user-machine)
10. [Uninstall Story](#10-uninstall-story)
11. [Out of Scope for Phase 1](#11-out-of-scope-for-phase-1)

---

## 1. System Overview

Luminque is a background agent that runs on Windows machines and records GUI interactions for analysis. It is distributed as a single signed `.exe` containing three logical processes:

| Process | Role | Trigger |
|---|---|---|
| `luminque-capture` | Records GUI interactions to a local SQLite DB | ONLOGON via Task Scheduler |
| `luminque-sender` | Reads DB, ships data to server, sends heartbeat | Every 30–60 min via Task Scheduler |
| `luminque-watchdog` | Keeps capture and sender alive; handles memory drift | Every 5 min via Task Scheduler |

All three processes are invoked by running the same `.exe` with a different CLI flag. There is no separate installer or service installer — the user runs the `.exe` once, sees a consent UI, clicks agree, and is done.

### Process Relationships

```
luminque.exe --onboard
    └── registers 3 scheduled tasks
    └── starts luminque.exe --capture immediately (subprocess)
    └── shows confirmation and exits

luminque.exe --capture        (long-running, ONLOGON)
    └── records GUI events → SQLite DB at %APPDATA%\Luminque\recordings\recording.db

luminque.exe --send           (short-lived, every 30–60 min)
    └── reads DB → POST to server → sends heartbeat

luminque.exe --watchdog       (short-lived, every 5 min)
    └── checks capture process is alive + memory < 500 MB
    └── restarts capture if needed
    └── triggers daily midnight restart of capture
```

No admin/UAC elevation is required at any point. All tasks are registered under the current user's context.

---

## Implementation Locations

Every piece of work described in this document maps to a specific file in `luminque-ops`. Use this table as the authoritative "what do I touch to implement X?" reference.

| Component | File | Status | Change |
|---|---|---|---|
| Entry point routing | `luminque/main.py` | Exists (complete) | No changes needed. Dispatches all four modes via `sys.argv[1]`. Imports `luminque.onboarding.run`, `luminque.capture.run`, `luminque.sender.run`, `luminque.watchdog.run`. |
| Onboarding UI | `luminque/onboarding/__init__.py` | Exists (stub) | Implement full tkinter onboarding flow (§3): 480×340 window, consent screen → server setup screen → enrollment → task registration → start capture. Wire `_on_connect()` → `install_exe()` → `enroll_device()` → `register_all_tasks()` → `_start_capture_now()`. Enrollment failure must abort before task registration. Rename or alias stub `run()` to match import in `main.py`. |
| Device enrollment | `luminque/onboarding/enrollment.py` | New file | Create. Implement `enroll_device(api_url, enrollment_token)`: POST to `/api/v1/devices/enroll`, raise `RuntimeError` on failure, store `device_id`/`tenant_id`/`auth_token`/`api_url` in Windows Credential Manager via `keyring` (§3). |
| Exe self-install | `luminque/onboarding/__init__.py` | Exists (stub) | Add `install_exe()` helper (§9): copies `sys.executable` to `%LOCALAPPDATA%\Programs\Luminque\luminque.exe` if not already there; returns destination path for use by `register_all_tasks()`. |
| schtasks registration | `luminque/onboarding/scheduler.py` | New file | Create. Implement `register_all_tasks(exe_path)`, `_register_capture()`, `_register_sender()`, `_register_watchdog()`, `deregister_all_tasks()` using `schtasks.exe` CLI (§4). Module does not exist yet; `luminque/onboarding/` is currently a single-file package with no `scheduler.py`. |
| Watchdog logic | `luminque/watchdog/__init__.py` | Exists (stub) | Implement full watchdog cycle (§5): `_find_capture_process()`, `_start_capture()`, `_kill_and_restart()`, `_is_midnight_window()`. Add `psutil` + `logging` imports; set up log file at `%APPDATA%\Luminque\watchdog.log`. Replace stub `run()` body with complete `run_watchdog()` logic. |
| PyInstaller spec | `luminque.spec` | Exists (stub) | Uncomment `icon="assets/luminque.ico"` and `version="version_info.txt"` lines once those assets exist. Add any additional `hiddenimports` discovered during test builds. Entry point (`luminque/main.py`) and all existing hidden imports are already correct. |
| Build pipeline — test job | `.github/workflows/build.yml` | Exists (incomplete) | Add a `test-mac` job (macOS, `uv sync --extra dev`, `pytest tests/unit/`) that runs on push to main and on PRs. Gate `build-windows` on `test-mac` via `needs: test-mac`. Add `pull_request: branches: [main]` trigger. |
| Build pipeline — signing step | `.github/workflows/build.yml` | Exists (stubbed) | Uncomment and complete the DigiCert KeyLocker commands inside the `Sign with EV certificate` step once the EV cert is provisioned and the three secrets (`DIGICERT_API_KEY`, `DIGICERT_CERT_ID`, `SM_HOST`) are added to the repository. |
| Windows VERSIONINFO resource | `version_info.txt` | New file | Create at repo root. Use the `VSVersionInfo(...)` template from §6. Embed `CompanyName`, `FileDescription`, `FileVersion`, `ProductName`. Referenced by `luminque.spec` once uncommented. |
| Windows icon | `assets/luminque.ico` | New file | Create. Must be a valid `.ico` with 16×16, 32×32, 48×48, and 256×256 variants. Referenced by `luminque.spec` once uncommented. The `assets/` directory does not exist yet. |
| Unit tests | `tests/unit/` | New directory | Create. Required by the `test-mac` CI job (§7). Minimum coverage: watchdog logic (`_find_capture_process`, `_is_midnight_window`, `_kill_and_restart` — mock `psutil`), scheduler (`register_all_tasks` — mock `subprocess.run`). |

**Notes on actual file state vs. design doc pseudocode:**
- `main.py` calls `from luminque.onboarding import run` (not `run_onboarding`); the stub in `luminque/onboarding/__init__.py` already exports `run()` — keep that name or add an alias.
- `build.yml` uses `uv sync --extra dev` (not `pip install -r requirements.txt`). `requirements.txt` and `requirements-dev.txt` are **not** needed; dependencies are managed via `pyproject.toml`.
- `luminque.spec` lists the entry point as `luminque/main.py` (not `main.py` at repo root). Correct as-is.
- No changes are needed in the `openadapt-capture` fork for the work described in this document.

---

## 2. Main Entry Point

The single `.exe` detects its operating mode from the first CLI argument. This keeps PyInstaller packaging simple — one entry point, no multiprocessing module issues.

### `main.py`

```python
import sys

def main():
    if len(sys.argv) < 2:
        # No args: treat as double-click — launch onboarding
        from luminque.onboard import run_onboarding
        run_onboarding()
        return

    mode = sys.argv[1]

    if mode == "--onboard":
        from luminque.onboard import run_onboarding
        run_onboarding()

    elif mode == "--capture":
        from luminque.capture import run_capture
        run_capture()

    elif mode == "--send":
        from luminque.sender import run_send
        run_send()

    elif mode == "--watchdog":
        from luminque.watchdog import run_watchdog
        run_watchdog()

    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### Notes

- `--windowed` in PyInstaller suppresses the console window. All modes run silently except `--onboard`, which shows a tkinter window.
- The onboarding path is triggered by a bare double-click (no args) for UX simplicity — Windows users don't type CLI flags.
- Modes are intentionally simple strings, not subcommands, to avoid argparse dependency issues under PyInstaller.

---

## 3. Onboarding UI

The onboarding UI is a minimal tkinter window. Its sole purposes are:

1. Obtain explicit informed consent from the user.
2. Collect the server URL and enrollment token.
3. Call `POST /api/v1/devices/enroll` and store the result in Windows Credential Manager.
4. Register the three scheduled tasks.
5. Start capture immediately (no reboot required).
6. Confirm success and close.

No other UI exists in Phase 1. No system tray icon, no settings window.

### Onboarding Flow

```
1. Consent screen       — user reads consent text, clicks Cancel or I Agree
2. Server setup screen  — user enters server URL + enrollment token
3. Enroll device        — POST /api/v1/devices/enroll → store device_id,
                          tenant_id, auth_token in Windows Credential Manager
                          (if this fails, show error and stop — do not register tasks)
4. Register tasks       — schtasks registers LumniqueCapture, LumniqueSender,
                          LumniqueWatchdog
5. Start capture now    — subprocess.Popen(luminque.exe --capture)
6. Done screen          — "Setup complete. Luminque is now running."
```

### Screen Layout

**Screen 1 — Consent**
```
┌─────────────────────────────────────────────┐
│  Luminque — Data Collection Setup          │
│                                             │
│  What we collect:                           │
│  • Screenshots of your screen activity      │
│  • Keyboard and mouse interaction metadata  │
│  • Window titles and active application     │
│                                             │
│  Why:                                       │
│  [one sentence explaining the purpose]      │
│                                             │
│  Your data is stored locally and sent       │
│  securely to [org name] servers.            │
│                                             │
│  By clicking "I Agree", you consent to      │
│  this data collection.                      │
│                                             │
│  [ Cancel ]              [ I Agree ]        │
└─────────────────────────────────────────────┘
```

**Screen 2 — Server Setup** (shown after I Agree)
```
┌─────────────────────────────────────────────┐
│  Luminque — Server Setup                   │
│                                             │
│  Server URL:                                │
│  [ https://                              ]  │
│                                             │
│  Enrollment token:                          │
│  [ ********************************      ]  │
│                                             │
│  [ Back ]                [ Connect ]        │
└─────────────────────────────────────────────┘
```

The enrollment token is a ≥32-character string provided by the IT admin. In MVP any string of that length is accepted by the server. The server URL is the base URL of the Luminque server (e.g. `https://api.luminque.com`). Both fields may be pre-baked into the binary at build time (embedded as constants) to simplify deployment — the screen is then skipped.

Window dimensions: 480×340px, not resizable, centered on screen.

### `luminque/onboarding/__init__.py`

```python
import tkinter as tk
from tkinter import messagebox
import subprocess
import sys
import os

EXE_PATH = sys.executable  # path to luminque.exe at runtime

CONSENT_TEXT = (
    “What we collect:\n”
    “  • Screenshots of your screen activity\n”
    “  • Keyboard and mouse interaction metadata\n”
    “  • Window titles and active application\n\n”
    “Why:\n”
    “  This data is used to understand how you work so we can\n”
    “  build tools that help you do it faster.\n\n”
    “Your data is stored locally and sent securely to Luminque servers.\n\n”
    “By clicking \”I Agree\”, you consent to this data collection.”
)


def run():
    “””Entry point called by luminque/main.py for --onboard (or no args).”””
    root = tk.Tk()
    root.title(“Luminque — Setup”)
    root.geometry(“480x340”)
    root.resizable(False, False)
    _center_window(root, 480, 340)

    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(
        frame,
        text=”Luminque — Data Collection Setup”,
        font=(“Segoe UI”, 12, “bold”),
    ).pack(anchor=”w”, pady=(0, 12))

    tk.Label(
        frame,
        text=CONSENT_TEXT,
        justify=tk.LEFT,
        wraplength=440,
        font=(“Segoe UI”, 9),
    ).pack(anchor=”w”)

    btn_frame = tk.Frame(frame)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))

    tk.Button(
        btn_frame,
        text=”Cancel”,
        width=10,
        command=root.destroy,
    ).pack(side=tk.LEFT)

    tk.Button(
        btn_frame,
        text=”I Agree”,
        width=10,
        default=tk.ACTIVE,
        command=lambda: _show_server_setup(root),
    ).pack(side=tk.RIGHT)

    root.mainloop()


def _show_server_setup(root):
    “””Replace consent content with the server URL + enrollment token form.”””
    for widget in root.winfo_children():
        widget.destroy()

    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(
        frame,
        text=”Luminque — Server Setup”,
        font=(“Segoe UI”, 12, “bold”),
    ).pack(anchor=”w”, pady=(0, 12))

    tk.Label(frame, text=”Server URL:”, font=(“Segoe UI”, 9)).pack(anchor=”w”)
    url_var = tk.StringVar()
    tk.Entry(frame, textvariable=url_var, width=52).pack(anchor=”w”, pady=(2, 10))

    tk.Label(frame, text=”Enrollment token:”, font=(“Segoe UI”, 9)).pack(anchor=”w”)
    token_var = tk.StringVar()
    tk.Entry(frame, textvariable=token_var, show=”*”, width=52).pack(anchor=”w”, pady=(2, 0))

    btn_frame = tk.Frame(frame)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))

    tk.Button(
        btn_frame,
        text=”Back”,
        width=10,
        command=lambda: _restart_consent(root),
    ).pack(side=tk.LEFT)

    tk.Button(
        btn_frame,
        text=”Connect”,
        width=10,
        default=tk.ACTIVE,
        command=lambda: _on_connect(root, url_var.get().strip(), token_var.get().strip()),
    ).pack(side=tk.RIGHT)


def _restart_consent(root):
    “””Rebuild the consent screen (Back button on server setup screen).”””
    for widget in root.winfo_children():
        widget.destroy()
    # Re-invoke run() logic inline to rebuild consent widgets inside the
    # existing root window rather than opening a second window.
    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)
    tk.Label(frame, text=”Luminque — Data Collection Setup”,
             font=(“Segoe UI”, 12, “bold”)).pack(anchor=”w”, pady=(0, 12))
    tk.Label(frame, text=CONSENT_TEXT, justify=tk.LEFT,
             wraplength=440, font=(“Segoe UI”, 9)).pack(anchor=”w”)
    btn_frame = tk.Frame(frame)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))
    tk.Button(btn_frame, text=”Cancel”, width=10,
              command=root.destroy).pack(side=tk.LEFT)
    tk.Button(btn_frame, text=”I Agree”, width=10, default=tk.ACTIVE,
              command=lambda: _show_server_setup(root)).pack(side=tk.RIGHT)


def _on_connect(root, api_url: str, enrollment_token: str):
    “””Validate inputs, enroll the device, then complete onboarding.”””
    if not api_url:
        messagebox.showerror(“Missing Field”, “Please enter the server URL.”)
        return
    if len(enrollment_token) < 32:
        messagebox.showerror(
            “Missing Field”,
            “Enrollment token must be at least 32 characters.”,
        )
        return

    root.withdraw()
    try:
        # 1. Install exe to stable path
        exe_path = install_exe()

        # 2. Enroll device — must succeed before tasks are registered
        from luminque.onboarding.enrollment import enroll_device
        enroll_device(api_url=api_url, enrollment_token=enrollment_token)

        # 3. Register scheduled tasks
        from luminque.onboarding.scheduler import register_all_tasks
        register_all_tasks(exe_path)

        # 4. Start capture immediately
        _start_capture_now(exe_path)

        messagebox.showinfo(
            “Luminque”,
            “Setup complete.\n\nLuminque is now running in the background.\n”
            “It will start automatically each time you log in.”,
        )
    except Exception as e:
        messagebox.showerror(“Setup Failed”, str(e))
    finally:
        root.destroy()


def _center_window(win, w, h):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    win.geometry(f”{w}x{h}+{x}+{y}”)


def _start_capture_now(exe_path: str):
    “””Launch capture as a detached background process.”””
    subprocess.Popen(
        [exe_path, “--capture”],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
```

### `luminque/onboarding/enrollment.py`

```python
import platform
import socket
import os
import keyring
import requests

KEYRING_SERVICE = “luminque-device”


def enroll_device(api_url: str, enrollment_token: str) -> dict:
    “””
    Call POST /api/v1/devices/enroll and persist credentials in Windows
    Credential Manager (keyring).

    Raises RuntimeError with a user-readable message on any failure so the
    caller (onboarding UI) can display it directly.

    Returns the full response dict on success.
    “””
    hostname = socket.gethostname()
    os_version = _get_os_version()

    payload = {
        “tenant_id”: _get_tenant_id(api_url),   # see note below
        “hostname”: hostname,
        “platform”: “windows”,
        “os_version”: os_version,
        “enrollment_token”: enrollment_token,
    }

    try:
        resp = requests.post(
            f”{api_url.rstrip('/')}/api/v1/devices/enroll”,
            json=payload,
            timeout=30,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f”Could not connect to {api_url}.\n\nCheck the server URL and your network connection.\n\n({exc})”
        ) from exc
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f”Connection to {api_url} timed out. Check the server URL and try again.”
        )

    if resp.status_code != 201:
        try:
            detail = resp.json().get(“detail”, resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(
            f”Enrollment failed (HTTP {resp.status_code}):\n{detail}”
        )

    data = resp.json()

    # Persist credentials in Windows Credential Manager
    keyring.set_password(KEYRING_SERVICE, “device_id”,  data[“device_id”])
    keyring.set_password(KEYRING_SERVICE, “tenant_id”,  data[“tenant_id”])
    keyring.set_password(KEYRING_SERVICE, “auth_token”, data[“auth_token”])
    keyring.set_password(KEYRING_SERVICE, “api_url”,    api_url)

    return data


def _get_os_version() -> str:
    try:
        return f”{platform.release()} ({platform.version()})”
    except Exception:
        return “unknown”


def _get_tenant_id(api_url: str) -> str:
    “””
    In MVP the tenant_id is returned by the server during enrollment and does
    not need to be known in advance. However the /enroll endpoint currently
    requires it in the request body. Pass the value read from keyring if a
    previous enrollment stored it, otherwise use an empty string — the server
    should derive it from the enrollment_token.

    TODO: confirm final API contract with backend team; remove this helper once
    the request schema is finalised.
    “””
    existing = keyring.get_password(KEYRING_SERVICE, “tenant_id”)
    return existing or “”
```

**Keyring keys stored after successful enrollment:**

| Service | Username | Value |
|---|---|---|
| `luminque-device` | `device_id` | UUID assigned by server |
| `luminque-device` | `tenant_id` | Tenant UUID |
| `luminque-device` | `auth_token` | Long-lived device token |
| `luminque-device` | `api_url` | Server base URL entered by user |

The `auth_token` is used by the sender process (`luminque.exe --send`) for all subsequent API calls. It is read from keyring at the start of each send cycle:

```python
import keyring
auth_token = keyring.get_password(“luminque-device”, “auth_token”)
api_url     = keyring.get_password(“luminque-device”, “api_url”)
```

### Error Handling

- If `enroll_device()` raises, show an error dialog with the exception message. **Do not proceed to task registration.** The device cannot function without a valid `device_id` and `auth_token`.
- If `_register_tasks()` fails after enrollment succeeds, show an error dialog. The user may need to re-run onboarding. Credentials stored in keyring during the enrollment step are harmless to leave in place — re-running enrollment with the same token will overwrite them.
- If `_start_capture_now()` fails after tasks are registered, show a non-fatal warning: “Capture will start on your next login.”
- Never silently swallow errors during onboarding — the user has no other way to know something went wrong.

---

## 4. Task Scheduler Registration

All three tasks are registered using `schtasks.exe`. No admin privileges are required because tasks run as the current user (`/RU ""` uses the logged-in user).

### `luminque/scheduler.py`

```python
import subprocess
import os
import sys

TASK_NAMES = {
    "capture": "LumniqueCapture",
    "sender":  "LumniqueSender",
    "watchdog": "LumniqueWatchdog",
}

def register_all_tasks(exe_path: str):
    _register_capture(exe_path)
    _register_sender(exe_path)
    _register_watchdog(exe_path)


def _run(cmd: list[str]):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks failed:\n{result.stderr.strip() or result.stdout.strip()}"
        )


def _register_capture(exe_path: str):
    """Run at every login, indefinitely."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["capture"],
        "/TR", f'"{exe_path}" --capture',
        "/SC", "ONLOGON",
        "/RU", "",               # current user
        "/RL", "LIMITED",        # no elevation
        "/DELAY", "0000:30",     # 30-second delay after logon
    ])


def _register_sender(exe_path: str):
    """Run every 45 minutes, indefinitely."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["sender"],
        "/TR", f'"{exe_path}" --send',
        "/SC", "MINUTE",
        "/MO", "45",
        "/RU", "",
        "/RL", "LIMITED",
        "/ST", "00:00",
        "/ET", "23:59",
        "/K",                    # stop at end time (restarts next day)
    ])


def _register_watchdog(exe_path: str):
    """Run every 5 minutes, indefinitely."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["watchdog"],
        "/TR", f'"{exe_path}" --watchdog',
        "/SC", "MINUTE",
        "/MO", "5",
        "/RU", "",
        "/RL", "LIMITED",
    ])


def deregister_all_tasks():
    for name in TASK_NAMES.values():
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True,
        )
```

### Exact schtasks Commands (for reference / manual testing)

**Capture:**
```
schtasks /Create /F /TN "LumniqueCapture" /TR "\"C:\...\luminque.exe\" --capture" /SC ONLOGON /RU "" /RL LIMITED /DELAY 0000:30
```

**Sender:**
```
schtasks /Create /F /TN "LumniqueSender" /TR "\"C:\...\luminque.exe\" --send" /SC MINUTE /MO 45 /RU "" /RL LIMITED /ST 00:00 /ET 23:59 /K
```

**Watchdog:**
```
schtasks /Create /F /TN "LumniqueWatchdog" /TR "\"C:\...\luminque.exe\" --watchdog" /SC MINUTE /MO 5 /RU "" /RL LIMITED
```

### Notes

- `/F` overwrites an existing task with the same name — safe to re-run onboarding.
- `/RU ""` — empty string means "current interactive user". Does not prompt for a password.
- `/RL LIMITED` — explicitly requests no UAC elevation for the task.
- The 30-second logon delay for capture prevents resource contention at boot.
- `schtasks` writes tasks to `%USERPROFILE%\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup` equivalent in the Task Scheduler store; these survive across Windows updates.

---

## 5. Watchdog Logic

The watchdog runs every 5 minutes via Task Scheduler. Each invocation is a short-lived process: check, act if needed, exit.

### Responsibilities

1. **Liveness check** — is `luminque-capture` running? Restart if not.
2. **Memory check** — is `luminque-capture` RSS > 500 MB? Restart if so.
3. **Daily midnight restart** — restart capture once per day around midnight to clear memory drift.

### `luminque/watchdog.py`

```python
import psutil
import subprocess
import sys
import os
import datetime
import logging

EXE_PATH = sys.executable
CAPTURE_MARKER = "--capture"       # argv substring that identifies the capture process
MEMORY_LIMIT_MB = 500
LOG_PATH = os.path.join(os.environ["APPDATA"], "Luminque", "watchdog.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def run_watchdog():
    capture_proc = _find_capture_process()

    if capture_proc is None:
        log.info("Capture process not found — restarting.")
        _start_capture()
        return

    try:
        rss_mb = capture_proc.memory_info().rss / (1024 * 1024)
    except psutil.NoSuchProcess:
        log.info("Capture process disappeared during memory check — restarting.")
        _start_capture()
        return

    if rss_mb > MEMORY_LIMIT_MB:
        log.warning(f"Capture RSS {rss_mb:.1f} MB > {MEMORY_LIMIT_MB} MB — restarting.")
        _kill_and_restart(capture_proc)
        return

    if _is_midnight_window():
        log.info("Midnight window detected — performing daily restart of capture.")
        _kill_and_restart(capture_proc)
        return

    log.info(f"Capture healthy. RSS={rss_mb:.1f} MB.")


def _find_capture_process() -> psutil.Process | None:
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
            exe = proc.info["exe"] or ""
            if CAPTURE_MARKER in cmdline and "luminque" in exe.lower():
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _start_capture():
    subprocess.Popen(
        [EXE_PATH, "--capture"],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def _kill_and_restart(proc: psutil.Process):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    _start_capture()


def _is_midnight_window() -> bool:
    """
    Returns True if current time is between 00:00 and 00:05.
    The watchdog runs every 5 min, so this window is hit at most once per day.
    """
    now = datetime.datetime.now().time()
    return now >= datetime.time(0, 0) and now < datetime.time(0, 5)
```

### Decision Rationale

- **psutil** is used rather than `tasklist` or WMI because it gives RSS memory directly and is already a dependency of openadapt-capture.
- **Terminate then kill** — gives capture a graceful shutdown window (10 s) before SIGKILL, so it can flush the SQLite WAL.
- **Midnight window** — the 5-min watchdog schedule means the `00:00–00:05` window is visited exactly once per calendar day under normal operation. If the machine is off at midnight, the restart is skipped until the next midnight.
- **Memory limit 500 MB** — conservative ceiling. Capture is expected to use 50–150 MB; 500 MB indicates a leak.

---

## 6. PyInstaller .spec File

### Overview

- `--onefile` — single `.exe`, no unpacked folder to manage.
- `--windowed` — no console window (all modes are silent or use tkinter).
- Hidden imports are required because PyInstaller cannot trace dynamic imports inside openadapt-capture's dependencies.
- spaCy models are NOT bundled (Phase 2 concern — they are large and model selection is not finalized).

### `luminque.spec`

```python
# luminque.spec
# Build with: pyinstaller luminque.spec

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect data files for packages that need them at runtime
datas = []
datas += collect_data_files("alembic")          # migration scripts (if used)
datas += collect_data_files("certifi")          # SSL certs for requests

hidden_imports = [
    # SQLAlchemy
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.orm",

    # pynput — Windows backend selected at runtime, not statically detectable
    "pynput.keyboard._win32",
    "pynput.mouse._win32",

    # Pillow — C extension loaded dynamically
    "PIL._imaging",
    "PIL.Image",
    "PIL.ImageGrab",

    # psutil
    "psutil._pswindows",
    "psutil._psutil_windows",

    # tkinter (may need explicit listing on some Python builds)
    "tkinter",
    "tkinter.messagebox",

    # keyring — Windows Credential Manager backend selected at runtime
    "keyring.backends.Windows",
    "keyring.backends._win_crypto",

    # openadapt-capture internals (add as discovered during test builds)
    # "openadapt.capture.something",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "spacy",              # not bundled in Phase 1
        "torch",              # not needed for capture/send
        "numpy",              # exclude if not required; reduces size
        "matplotlib",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="luminque",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                # compress with UPX for smaller file
    upx_exclude=[],
    runtime_tmpdir=None,     # extract to temp dir each run (onefile default)
    console=False,           # --windowed
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # signing done post-build (see §8)
    entitlements_file=None,
    icon="assets/luminque.ico",
    version="version_info.txt",  # Windows VERSIONINFO resource
)
```

### `version_info.txt` (Windows VERSIONINFO)

```
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(u'040904B0', [
        StringStruct(u'CompanyName', u'Luminque'),
        StringStruct(u'FileDescription', u'Luminque Background Agent'),
        StringStruct(u'FileVersion', u'1.0.0.0'),
        StringStruct(u'InternalName', u'luminque'),
        StringStruct(u'ProductName', u'Luminque'),
        StringStruct(u'ProductVersion', u'1.0.0.0'),
      ])
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
```

### Build Notes

- Run `pyinstaller luminque.spec` from the repo root inside a Windows environment (or GitHub Actions Windows runner).
- The `--onefile` mode extracts to a temp directory (`%TEMP%\_MEIxxxxxx`) on each run. This is normal and acceptable for Phase 1.
- First build: run with `debug=True` and `console=True` to catch missing imports; revert for release.
- If a hidden import causes an import error at runtime, add it to the `hiddenimports` list and rebuild.

---

## 7. GitHub Actions Build Pipeline

The `.exe` is built exclusively on a Windows runner to ensure Windows-specific binaries (pynput `_win32`, psutil `_pswindows`) are included correctly.

Mac is used for unit tests of business logic only. Integration tests use Parallels/VMware on Mac (out of scope for this pipeline).

### `.github/workflows/build.yml`

```yaml
name: Build luminque.exe

on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:
    branches: [main]

jobs:
  test-mac:
    name: Unit tests (macOS)
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements-dev.txt

      - name: Run unit tests
        run: pytest tests/unit/ -v

  build-windows:
    name: Build .exe (Windows)
    runs-on: windows-latest
    needs: test-mac

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          architecture: "x64"

      - name: Install dependencies
        run: pip install -r requirements.txt pyinstaller

      - name: Build exe
        run: pyinstaller luminque.spec

      - name: Verify exe launches
        run: |
          # Smoke-test: exe should print usage and exit cleanly with unknown flag
          .\dist\luminque.exe --_smoke_test || true

      - name: Upload unsigned artifact
        uses: actions/upload-artifact@v4
        with:
          name: luminque-unsigned
          path: dist/luminque.exe
          retention-days: 7

  sign-windows:
    name: Sign .exe
    runs-on: windows-latest
    needs: build-windows
    # Only sign on version tags, not every PR
    if: startsWith(github.ref, 'refs/tags/v')

    steps:
      - name: Download unsigned artifact
        uses: actions/download-artifact@v4
        with:
          name: luminque-unsigned
          path: dist/

      - name: Sign with EV certificate (DigiCert)
        env:
          DIGICERT_API_KEY: ${{ secrets.DIGICERT_API_KEY }}
          DIGICERT_CERT_ID: ${{ secrets.DIGICERT_CERT_ID }}
          SM_HOST: ${{ secrets.SM_HOST }}
        run: |
          # Uses DigiCert KeyLocker / Software Trust Manager
          # Install smctl if not present on runner image
          curl -X GET https://one.digicert.com/signingmanager/api-ui/v1/releases/smtools-windows-x64.msi/download `
            -H "x-api-key: $env:DIGICERT_API_KEY" -o smtools.msi
          msiexec /i smtools.msi /quiet /qn
          smctl sign --fingerprint $env:DIGICERT_CERT_ID --input dist\luminque.exe

      - name: Upload signed artifact
        uses: actions/upload-artifact@v4
        with:
          name: luminque-signed
          path: dist/luminque.exe
          retention-days: 30

      - name: Create GitHub release
        uses: softprops/action-gh-release@v2
        with:
          files: dist/luminque.exe
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Pipeline Notes

- `requirements.txt` — production deps (pynput, psutil, sqlalchemy, Pillow, requests, etc.)
- `requirements-dev.txt` — adds pytest, pytest-mock, etc.
- The sign step is gated to version tags (`v*`) to avoid burning signing quota on every commit.
- DigiCert KeyLocker is the recommended EV signing approach for CI (hardware token is not required; signing happens via API). Adjust to your actual CA as needed.
- The unsigned artifact is retained for 7 days for debugging; signed artifact for 30 days.

---

## 8. Code Signing

### Why It Matters

Without a valid Authenticode signature, Windows SmartScreen displays "Windows protected your PC" and hides the "Run anyway" option by default. Non-technical users will not know how to proceed. **All production builds must be signed.**

### Certificate Recommendation

Use an **Extended Validation (EV) code signing certificate**. EV certificates carry immediate SmartScreen reputation with Microsoft, whereas standard OV certificates require accumulating download reputation over time (which can take weeks).

Recommended CA: DigiCert, Sectigo, or GlobalSign (all support cloud/KeyLocker signing for CI use).

Cost: ~$400–$700/year for an EV cert with KeyLocker.

### Signing in the Pipeline

Signing happens **after** PyInstaller builds the `.exe`, as a separate job (see §7). The unsigned artifact is passed between jobs via GitHub Actions artifact storage.

The tool used is `signtool.exe` (part of Windows SDK) or the CA's own CLI (`smctl` for DigiCert). Example using `signtool` directly if the certificate is in the Windows certificate store:

```powershell
signtool sign `
  /tr http://timestamp.digicert.com `
  /td sha256 `
  /fd sha256 `
  /sha1 <CERT_THUMBPRINT> `
  dist\luminque.exe
```

### Timestamp

Always timestamp the signature (`/tr` flag). Without a timestamp, the signature becomes invalid when the certificate expires, making all previously distributed `.exe` files untrusted.

### Verification

After signing, verify with:

```powershell
signtool verify /pa dist\luminque.exe
```

Expected output: `Successfully verified: dist\luminque.exe`

---

## 9. Directory Structure on User Machine

All Luminque files live under `%APPDATA%\Luminque\` (i.e., `C:\Users\<username>\AppData\Roaming\Luminque\`). This location does not require admin access and survives Windows user profile roaming if enabled.

```
%APPDATA%\Luminque\
├── recordings\
│   ├── recording.db        # SQLite database written by luminque-capture (single file, appended across restarts)
│   ├── recording.db-wal    # SQLite WAL file (normal; flushed on clean shutdown)
│   └── recording.db-shm    # SQLite shared memory file
├── sender_state.json       # Sender cursor + last send timestamp
├── logs\
│   ├── sender-YYYY-MM-DD.log
│   └── watchdog.log
└── config.json             # (future) local config overrides; not used in Phase 1

%LOCALAPPDATA%\Programs\Luminque\
└── luminque.exe            # The installed .exe (copied here by onboarding)
```

### Why Two Locations

- `%APPDATA%` — for data files (DB, logs). Survives across .exe updates; not deleted on reinstall.
- `%LOCALAPPDATA%\Programs\Luminque\` — for the executable. Standard per-user app install location; excluded from roaming profiles (binary should not roam).

### Onboarding File Installation

The onboarding flow should copy `luminque.exe` from its download location (typically `%USERPROFILE%\Downloads\`) to `%LOCALAPPDATA%\Programs\Luminque\luminque.exe` before registering tasks, so that the task paths remain stable regardless of where the user originally ran the installer.

```python
import shutil, os

def install_exe():
    src = sys.executable
    dst_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Luminque")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "luminque.exe")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    return dst
```

Call `install_exe()` inside `_on_agree()` before `_register_tasks()`, and pass the returned path to `register_all_tasks()`.

---

## 10. Uninstall Story

There is no uninstaller `.exe` in Phase 1. Removal is manual but straightforward. Document this in the user-facing FAQ.

### Full Removal Steps (for user documentation)

1. **Delete scheduled tasks:**
   ```
   schtasks /Delete /F /TN "LumniqueCapture"
   schtasks /Delete /F /TN "LumniqueSender"
   schtasks /Delete /F /TN "LumniqueWatchdog"
   ```

2. **Stop any running capture process:**
   ```
   taskkill /F /IM luminque.exe
   ```

3. **Delete the exe:**
   ```
   rmdir /S /Q "%LOCALAPPDATA%\Programs\Luminque"
   ```

4. **Delete data (optional — user's choice):**
   ```
   rmdir /S /Q "%APPDATA%\Luminque"
   ```

### Programmatic Uninstall (Phase 1 stretch goal)

If time permits, add a `--uninstall` mode to the exe that runs steps 1–3 above and prompts the user about data deletion. This is not required for Phase 1 launch but makes the experience cleaner.

```python
elif mode == "--uninstall":
    from luminque.uninstall import run_uninstall
    run_uninstall()
```

---

## Implementation Locations

This section maps every piece of work described in this design doc to a specific file in `luminique-ops`. It is the authoritative reference for "what do I touch to implement X?"

No changes are needed in the `openadapt-capture` fork. All deployment-layer code (onboarding, watchdog, task registration, build pipeline) lives exclusively in `luminique-ops`.

### Files to modify (exist, need implementation)

| File | Current state | What to implement |
|---|---|---|
| `luminque/watchdog/__init__.py` | Stub — `run()` prints placeholder | Full watchdog logic from §5: `_find_capture_process()`, `_start_capture()`, `_kill_and_restart()`, `_is_midnight_window()`. Add `psutil` imports and `logging` setup writing to `%APPDATA%\Luminque\watchdog.log`. Replace stub `run()` with the complete `run_watchdog()` body. |
| `luminque/onboarding/__init__.py` | Stub — `run()` prints placeholder | Full tkinter onboarding flow from §3: consent screen, server setup screen, enrollment call, task registration, start capture. Wire `_on_connect()` → `install_exe()` → `enroll_device()` → `register_all_tasks()` → `_start_capture_now()`. Enrollment failure must abort before task registration (see §3 Error Handling). Keep the existing `run()` name — `main.py` already imports it. |
| `luminque.spec` | Skeleton exists — `icon` and `version` lines are commented out | Uncomment and set `icon="assets/luminque.ico"` and `version="version_info.txt"` once those assets are created. Add any additional `hiddenimports` discovered during test builds. The `luminque/main.py` entry point and all existing `hiddenimports` are already correct. |
| `.github/workflows/build.yml` | Exists — signing step body is commented out with `TODO` | Uncomment and complete the DigiCert KeyLocker signing commands inside the `Sign with EV certificate` step once the EV certificate is provisioned and the three secrets (`DIGICERT_API_KEY`, `DIGICERT_CERT_ID`, `SM_HOST`) are added to the repository. Add a `pull_request: branches: [main]` trigger and a `test-mac` job (macOS unit tests) that must pass before `build-windows` runs, per the pipeline design in §7. |

### New files to create (do not exist yet)

| File | Purpose | Source in this doc |
|---|---|---|
| `luminque/onboarding/enrollment.py` | `enroll_device(api_url, enrollment_token)` — POST to `/api/v1/devices/enroll`, raise `RuntimeError` on failure, persist `device_id`, `tenant_id`, `auth_token`, `api_url` in Windows Credential Manager via `keyring`. | §3 enrollment code snippet. Add `keyring` and `requests` to `pyproject.toml` dependencies. |
| `luminque/onboarding/scheduler.py` | `register_all_tasks(exe_path)`, `_register_capture()`, `_register_sender()`, `_register_watchdog()`, `deregister_all_tasks()` — all schtasks calls. Extracted from `__init__.py` so the task logic is independently testable. | §4 (`luminque/scheduler.py` in the design; the actual module path in the repo should be `luminque/onboarding/scheduler.py` to keep onboarding self-contained.) |
| `assets/luminque.ico` | Windows icon for the `.exe` (used by PyInstaller and shown in Task Scheduler). Must be a valid `.ico` file with 16×16, 32×32, 48×48, and 256×256 variants. | §6 — currently commented out in `luminque.spec` |
| `version_info.txt` | Windows VERSIONINFO resource embedded in the `.exe` by PyInstaller. Contains `CompanyName`, `FileDescription`, `FileVersion`, `ProductName`. | §6 — currently commented out in `luminque.spec` |
| `requirements.txt` | Production dependency list for the Windows build runner (`pip install -r requirements.txt pyinstaller`). Derive from `pyproject.toml` dependencies. Required by the design-doc pipeline YAML (§7); the actual `build.yml` uses `uv sync` so this file is only needed if the `pip`-based fallback path in §7 is kept. Confirm and reconcile with `build.yml`. | §7 pipeline notes |
| `requirements-dev.txt` | Dev + test dependencies for the macOS unit-test runner. Derive from `[dependency-groups] dev` in `pyproject.toml`. Same caveat as `requirements.txt` above. | §7 pipeline notes |
| `tests/unit/` directory | Unit tests for watchdog logic (`_find_capture_process`, `_is_midnight_window`, `_kill_and_restart`), scheduler (`register_all_tasks` — mock `subprocess.run`), and enrollment (`enroll_device` — mock `requests.post` and `keyring`; cover success, HTTP error, connection error, and token length validation paths). Required by the `test-mac` CI job. | §7 |

### Quick-reference: flag → module mapping

| CLI flag | Module called | File |
|---|---|---|
| _(no args)_ or `--onboard` | `luminque.onboarding.run()` | `luminque/onboarding/__init__.py` |
| `--capture` | `luminque.capture.run()` | `luminque/capture/__init__.py` |
| `--send` | `luminque.sender.run()` | `luminque/sender/__init__.py` |
| `--watchdog` | `luminque.watchdog.run()` | `luminque/watchdog/__init__.py` |

The dispatch logic in `luminque/main.py` is already implemented and does not need changes for Phase 1.

### openadapt-capture fork — no changes needed

The `openadapt-capture` fork requires exactly one code change (action-gated screenshots), which is documented in `luminque-capture-p1.md` §3 and is unrelated to the deployment work described in this doc. Everything in this document — onboarding, watchdog, task registration, PyInstaller spec, and the build pipeline — is implemented entirely within `luminique-ops`.

---

## 11. Out of Scope for Phase 1

The following are explicitly deferred to later phases. Do not implement or design these in the Phase 1 codebase.

| Item | Notes |
|---|---|
| spaCy NLP / ML model | Model selection not finalized; too large to bundle |
| System tray icon | Adds complexity; background-only is sufficient for Phase 1 |
| Auto-update mechanism | Requires separate infrastructure; manual re-download is acceptable |
| Programmatic uninstaller | Manual steps are sufficient; `--uninstall` mode is a stretch goal only |
| Multi-user / machine-wide install | All Phase 1 installs are per-user, no admin required |
| Remote configuration / kill switch | Server-side config, not in scope yet |
| Data encryption at rest | SQLite DB is unencrypted in Phase 1 |
| Crash reporting / Sentry integration | Logging to local files only |
| Custom Task Scheduler XML | `schtasks` CLI is sufficient; XML gives finer control but is not needed |
| Windows installer (.msi / NSIS) | Self-extracting `.exe` + copy-to-Programs is sufficient |
| Linux / macOS support | Windows only |

---

## Appendix A: Key Dependencies

| Package | Version (pin in requirements.txt) | Purpose |
|---|---|---|
| `pynput` | `>=1.7.6` | GUI event capture |
| `psutil` | `>=5.9` | Process inspection (watchdog) |
| `sqlalchemy` | `>=2.0` | ORM for SQLite DB |
| `Pillow` | `>=10.0` | Screenshots |
| `requests` | `>=2.31` | HTTP to server (sender + enrollment) |
| `keyring` | `>=24.0` | Windows Credential Manager storage (enrollment credentials) |
| `pyinstaller` | `>=6.0` | Packaging (build dep only) |

## Appendix B: Testing Checklist Before Release

- [ ] Fresh Windows 10 VM: double-click `.exe`, complete onboarding (enter server URL + enrollment token), verify all 3 tasks appear in Task Scheduler
- [ ] Verify `device_id`, `tenant_id`, `auth_token`, `api_url` appear in Windows Credential Manager under service name `luminque-device` after onboarding
- [ ] Onboarding with invalid enrollment token (< 32 chars or wrong token): verify error dialog appears and no tasks are registered
- [ ] Onboarding with unreachable server URL: verify connection error dialog and no tasks are registered
- [ ] Reboot VM: verify capture restarts automatically via ONLOGON task
- [ ] Kill capture manually: wait 5 min, verify watchdog restarts it
- [ ] Run with memory leak simulation (or just reduce limit temporarily): verify watchdog kills and restarts
- [ ] Wait until midnight (or mock system clock): verify daily restart fires
- [ ] Verify SmartScreen behavior with signed `.exe` — should show publisher name, not warning
- [ ] Verify SmartScreen behavior with unsigned `.exe` — should show "Windows protected your PC"
- [ ] Run sender manually: `luminque.exe --send` — verify data reaches server, heartbeat recorded
- [ ] Verify `%APPDATA%\Luminque\` directory is created with correct files
- [ ] Manual uninstall steps: verify all tasks removed, no processes remain
