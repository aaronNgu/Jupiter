"""
luminque.watchdog — keeps capture alive; handles memory drift.

Runs as a one-shot check every 5 minutes via Task Scheduler.
"""

import logging
import os
import subprocess
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


CAPTURE_TASK_NAME = "LumniqueCapture"


def _start_capture() -> None:
    """Restart capture via its scheduled task.

    The watchdog is a short-lived scheduled task that exits immediately after
    this call. Spawning capture as a child subprocess would tie its lifetime to
    the watchdog (it dies when the watchdog exits inside a kill-on-close Job
    Object). Running the scheduled task hands ownership to the Task Scheduler
    service instead, so capture outlives the watchdog run.
    """
    subprocess.run(
        ["schtasks", "/Run", "/TN", CAPTURE_TASK_NAME],
        capture_output=True,
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
