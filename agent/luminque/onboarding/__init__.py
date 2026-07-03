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
    root.geometry("480x390")
    root.resizable(False, False)
    _center(root, 480, 390)
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
    tk.Entry(frame, textvariable=token_var, show="*", width=52).pack(anchor="w", pady=(2, 10))

    tk.Label(frame, text="Tenant ID:", font=("Segoe UI", 9)).pack(anchor="w")
    tenant_var = tk.StringVar()
    tk.Entry(frame, textvariable=tenant_var, width=52).pack(anchor="w", pady=(2, 0))

    btn = tk.Frame(frame)
    btn.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))
    tk.Button(btn, text="Back", width=10,
              command=lambda: _show_consent(root)).pack(side=tk.LEFT)
    tk.Button(btn, text="Connect", width=10, default=tk.ACTIVE,
              command=lambda: _on_connect(
                  root, url_var.get().strip(), token_var.get().strip(),
                  tenant_var.get().strip(),
              )).pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# Connect handler
# ---------------------------------------------------------------------------

def _on_connect(root: tk.Tk, api_url: str, enrollment_token: str, tenant_id: str) -> None:
    if not api_url:
        messagebox.showerror("Missing Field", "Please enter the server URL.")
        return
    if len(enrollment_token) < 32:
        messagebox.showerror(
            "Missing Field",
            "Enrollment token must be at least 32 characters.",
        )
        return
    if not tenant_id:
        messagebox.showerror("Missing Field", "Please enter the Tenant ID.")
        return

    root.withdraw()
    try:
        exe_path = install_exe()

        from luminque.onboarding.enrollment import enroll_device
        enroll_device(api_url=api_url, enrollment_token=enrollment_token, tenant_id=tenant_id)

        from luminque.onboarding.scheduler import register_all_tasks
        register_all_tasks(exe_path)

        _create_stop_shortcut(exe_path)
        _start_capture_now()

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


def _start_capture_now() -> None:
    """Kick off capture by running its scheduled task.

    A process started by the Task Scheduler service is owned by that service,
    not by onboarding, so it survives onboarding exiting. This is more reliable
    than spawning a DETACHED_PROCESS subprocess, which dies with the parent
    when the parent lives inside a kill-on-close Job Object (common in RDP/VM
    sessions). Requires register_all_tasks() to have run first.
    """
    from luminque.onboarding.scheduler import TASK_NAMES

    subprocess.run(
        ["schtasks", "/Run", "/TN", TASK_NAMES["capture"]],
        capture_output=True,
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
