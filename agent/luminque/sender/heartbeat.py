import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def get_or_create_machine_id(appdata_dir: Path) -> str:
    id_path = appdata_dir / "machine_id"
    if id_path.exists():
        return id_path.read_text().strip()
    new_id = str(uuid.uuid4())
    id_path.write_text(new_id)
    return new_id


def _is_capture_running() -> bool:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = proc.info.get("name") or ""
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if "luminque-capture.exe" in name or "--capture" in cmdline:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def build_health_report(
    machine_id: str,
    tenant_id: str,
    appdata_dir: Path,
    queue_depth: int,
    last_successful_send_utc: str | None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    usage = shutil.disk_usage(appdata_dir)
    disk_usage_percent = usage.used / usage.total * 100

    capture_running = _is_capture_running()

    if disk_usage_percent > 90:
        overall_status = "unhealthy"
    elif not capture_running or disk_usage_percent > 75:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    capture_component = {
        "name": "capture",
        "status": "healthy" if capture_running else "unhealthy",
        "message": None if capture_running else "capture process not found",
        "last_check_at": now,
    }

    sender_component = {
        "name": "sender",
        "status": "healthy",
        "message": f"queue_depth: {queue_depth}",
        "last_check_at": now,
    }

    return {
        "device_id": machine_id,
        "tenant_id": tenant_id,
        "overall_status": overall_status,
        "components": [capture_component, sender_component],
        "queue_depth": queue_depth,
        "last_successful_upload_at": last_successful_send_utc,
        "disk_usage_percent": disk_usage_percent,
        "reported_at": now,
    }
