"""
luminque.onboarding.scheduler — autostart registration for the agent.

Capture autostarts via a per-user shortcut in the **Startup folder**; the
sender and watchdog run as time-triggered **Scheduled Tasks**.

Why capture is NOT a scheduled task: an `/SC ONLOGON` task is a logon-trigger
task, and schtasks refuses to create one without administrator rights. That
made onboarding fail for standard users ("Access is denied" the moment the
capture task was registered — the agent enrolled but nothing ever ran). And
running onboarding elevated only moved the problem: /RU then bound the tasks
to the admin's identity, so they never fired in the real user's session. A
Startup-folder shortcut has neither problem — writing into the user's own
%APPDATA% needs no elevation, and the shortcut runs as whoever owns the
folder. The sender and watchdog stay as tasks because a standard user *can*
create time-triggered (`/SC MINUTE`) tasks that run as themselves.

Scheduled-task flags: /RL LIMITED (no UAC), /IT (only run while the user is
interactively logged on — no stored password), /RU <whoami> (explicit so it
does not default to SYSTEM on Server editions), /F (overwrite → re-runnable).
"""

import os
import subprocess

TASK_NAMES = {
    "sender":   "LumniqueSender",
    "watchdog": "LumniqueWatchdog",
}

# Older builds registered capture as this scheduled task. Kept only so that
# uninstall/stop on an upgraded machine deletes the stale task.
LEGACY_CAPTURE_TASK_NAME = "LumniqueCapture"

CAPTURE_SHORTCUT_NAME = "Luminque Capture.lnk"


def startup_dir() -> str:
    """The per-user Startup folder. Everything here is auto-launched by the
    shell at login, as that user, with no scheduler and no elevation."""
    return os.path.join(
        os.environ["APPDATA"],
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )


def capture_shortcut_path() -> str:
    return os.path.join(startup_dir(), CAPTURE_SHORTCUT_NAME)


def _current_user() -> str:
    """Return the logged-on user's account name for the schtasks /RU argument.

    `whoami` reads the actual logon token, so it reports the principal
    schtasks can resolve — including AzureAD\\user and MicrosoftAccount
    forms on personal Windows 11 machines, where the USERDOMAIN/USERNAME
    env vars name an account schtasks rejects. The env-var derivation stays
    as a fallback if whoami is unavailable.
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
    """Set up all three background components to autostart. Capture goes to the
    Startup folder (no admin); sender and watchdog become scheduled tasks.

    Safe to re-run: existing tasks are deleted first. `schtasks /Create /F`
    overwrites a task only if this account is allowed to — it fails with
    "Access is denied" against a task an *administrator* created (e.g. an
    earlier run of Setup via "Run as administrator"). Deleting first turns the
    overwrite into a plain create, so re-onboarding works without manual
    cleanup. It also clears the legacy capture task from older builds.
    """
    _delete_tasks()
    create_capture_autostart(exe_path)
    _register_sender(exe_path)
    _register_watchdog(exe_path)


def _delete_tasks() -> None:
    """Best-effort removal of every Luminque task. Silent by design: a missing
    task is success, and a task this account cannot delete is reported later,
    with remediation, by the create that follows."""
    for name in list(TASK_NAMES.values()) + [LEGACY_CAPTURE_TASK_NAME]:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True,
        )


def deregister_all_tasks() -> None:
    _delete_tasks()
    remove_capture_autostart()


def create_capture_autostart(exe_path: str) -> None:
    """Create the per-user Startup shortcut that launches capture at login.
    No admin required — it writes into the user's own %APPDATA%."""
    os.makedirs(startup_dir(), exist_ok=True)
    _create_shortcut(
        target=exe_path,
        arguments="--capture",
        description="Luminque background capture",
        shortcut_path=capture_shortcut_path(),
    )


def remove_capture_autostart() -> None:
    """Delete the capture Startup shortcut. Idempotent — missing is success."""
    try:
        os.remove(capture_shortcut_path())
    except FileNotFoundError:
        pass


def _create_shortcut(target: str, arguments: str, description: str,
                     shortcut_path: str) -> None:
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{shortcut_path}"); '
        f'$s.TargetPath = "{target}"; '
        f'$s.Arguments = "{arguments}"; '
        f'$s.Description = "{description}"; '
        f'$s.Save()'
    )
    result = subprocess.run(
        ["powershell", "-Command", ps], capture_output=True, text=True
    )
    # Exit code alone is not proof: `powershell -Command` can exit 0 while the
    # COM Save() failed (and security software commonly blocks writes to the
    # Startup folder, since a .lnk launching an exe from AppData is textbook
    # persistence behaviour). Verify the file actually landed, so this can
    # never report a silent false success.
    if result.returncode != 0 or not os.path.exists(shortcut_path):
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise RuntimeError(
            f"Failed to create shortcut at {shortcut_path}\n"
            f"(powershell exit={result.returncode}) {detail}\n"
            "If security software or policy blocks writes to the Startup "
            "folder, capture cannot be set to autostart."
        )


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Name the task in the message: "schtasks failed" alone cannot be told
        # apart between builds/tasks, which has cost real debugging cycles.
        # (x or "") so a missing stream can never turn the real schtasks
        # error into an AttributeError that masks it.
        task = cmd[cmd.index("/TN") + 1] if "/TN" in cmd else " ".join(cmd)
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        hint = ""
        if "access is denied" in detail.lower():
            # The tasks are deleted before creating, so reaching here means the
            # delete was refused too — i.e. the existing task belongs to another
            # account. Tell the user how to clear it instead of dead-ending.
            hint = (
                f"\n\nA scheduled task named '{task}' already exists and belongs to "
                "another account — usually an earlier setup run started with "
                "'Run as administrator'. Windows will not let this account "
                "replace it.\n\n"
                "To fix: open Command Prompt as administrator and run\n"
                f"    schtasks /Delete /F /TN {task}\n"
                "then run this setup again normally (a plain double-click — do "
                "NOT use 'Run as administrator')."
            )
        raise RuntimeError(f"schtasks failed creating task '{task}':\n{detail}{hint}")


def _register_sender(exe_path: str) -> None:
    """Run every 15 minutes."""
    _run([
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAMES["sender"],
        "/TR", f'"{exe_path}" --send',
        "/SC", "MINUTE",
        "/MO", "15",
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
