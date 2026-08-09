import os
import subprocess
import psutil

# LumniqueCapture is a legacy task name: capture now autostarts from a Startup
# shortcut, not a task. It stays in the delete list so `--stop` also cleans up
# machines upgraded from a build that did register the capture task.
TASK_NAMES = ["LumniqueCapture", "LumniqueSender", "LumniqueWatchdog"]
STOP_FLAGS = {"--capture", "--watchdog", "--send"}
CURRENT_PID = os.getpid()


def run():
    _delete_scheduled_tasks()
    _remove_capture_autostart()
    _kill_luminque_processes()


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


def _remove_capture_autostart():
    """Remove the capture Startup shortcut so login does not relaunch capture.
    Done before killing processes: with the shortcut gone and the tasks
    deleted, nothing can resurrect capture during the kill sweep."""
    try:
        from luminque.onboarding.scheduler import remove_capture_autostart
        remove_capture_autostart()
    except Exception:
        pass
