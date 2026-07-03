import os
import subprocess
import psutil

TASK_NAMES = ["LumniqueCapture", "LumniqueSender", "LumniqueWatchdog"]
STOP_FLAGS = {"--capture", "--watchdog", "--send"}
CURRENT_PID = os.getpid()


def run():
    _kill_luminque_processes()
    _delete_scheduled_tasks()


def _kill_luminque_processes():
    """Kill capture, sender, and watchdog processes.

    Watchdog is terminated first so it cannot restart capture
    before capture itself is killed.
    """
    targets = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if proc.pid == CURRENT_PID:
                continue
            exe = proc.info["exe"] or ""
            cmdline = proc.info["cmdline"] or []
            if "luminque" in exe.lower() and STOP_FLAGS.intersection(cmdline):
                targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Watchdog first (index -1 sorts before 0)
    targets.sort(key=lambda p: -1 if "--watchdog" in (p.info["cmdline"] or []) else 0)

    for proc in targets:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def _delete_scheduled_tasks():
    for name in TASK_NAMES:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True,
        )
