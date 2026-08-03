"""
luminque.onboarding.scheduler — Windows Task Scheduler registration.

All tasks run as the current user with no elevation (/RL LIMITED).
/IT (interactive) means the task only runs when the user is logged on —
this avoids needing a stored password, so no UAC prompt is required.
/RU is set to DOMAIN\\username explicitly because /RU "" defaults to
SYSTEM on Windows Server editions rather than the current user.
/F overwrites any existing task with the same name, making re-runs of
onboarding safe.
"""

import os
import subprocess

TASK_NAMES = {
    "capture":  "LumniqueCapture",
    "sender":   "LumniqueSender",
    "watchdog": "LumniqueWatchdog",
}


def _current_user() -> str:
    """Return the logged-on user's account name for the schtasks /RU argument.

    `whoami` reads the actual logon token, so it reports the principal
    schtasks can resolve — including AzureAD\\user and MicrosoftAccount
    forms on personal Windows 11 machines, where the USERDOMAIN/USERNAME
    env vars name an account schtasks rejects (task registration then fails
    right after enrollment, so the agent enrolls but never sends).
    The env-var derivation stays as a fallback if whoami is unavailable.
    """
    result = subprocess.run(["whoami"], capture_output=True, text=True)
    name = (result.stdout or "").strip()
    if result.returncode == 0 and name:
        return name
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    if domain and domain.upper() != os.environ.get("COMPUTERNAME", "").upper():
        # Genuine domain account — use DOMAIN\user form
        return f"{domain}\\{user}"
    return user  # local account — bare username is sufficient


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
        # (x or "") so a missing stream can never turn the real schtasks
        # error into an AttributeError that masks it.
        raise RuntimeError(
            f"schtasks failed:\n{(result.stderr or '').strip() or (result.stdout or '').strip()}"
        )


def _register_capture(exe_path: str) -> None:
    """Start at every login. 30-second delay avoids boot-time contention."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["capture"],
        "/TR", f'"{exe_path}" --capture',
        "/SC", "ONLOGON",
        "/RU", _current_user(),
        "/IT",           # only run when user is interactively logged on
        "/RL", "LIMITED",
        "/DELAY", "0000:30",
    ])


def _register_sender(exe_path: str) -> None:
    """Run every 2 minutes (increase to 45 before production release)."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["sender"],
        "/TR", f'"{exe_path}" --send',
        "/SC", "MINUTE",
        "/MO", "2",
        "/RU", _current_user(),
        "/IT",
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
        "/RU", _current_user(),
        "/IT",
        "/RL", "LIMITED",
    ])
