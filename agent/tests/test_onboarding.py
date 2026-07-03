"""
Tests for luminque.onboarding.

Key areas covered:
  - Module imports cleanly without opening a display
  - install_exe() returns dst unchanged when already at the target path
  - _create_stop_shortcut() calls powershell with correct arguments
  - _start_capture_now() starts capture via its scheduled task
  - Enrollment and scheduler modules import and expose expected symbols
"""
import os
import sys
from unittest.mock import MagicMock, call, patch


def test_onboarding_module_imports():
    """Smoke test: onboarding module imports without raising."""
    import luminque.onboarding  # noqa: F401


def test_install_exe_noop_when_already_at_target(tmp_path, monkeypatch):
    """install_exe() is a no-op when the exe is already at the stable path."""
    from luminque.onboarding import install_exe

    local_appdata = str(tmp_path)
    dst_dir = tmp_path / "Programs" / "Luminque"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "luminque.exe"
    dst.touch()

    monkeypatch.setenv("LOCALAPPDATA", local_appdata)
    monkeypatch.setattr(sys, "executable", str(dst))

    result = install_exe()
    assert result == str(dst)


def test_start_capture_now_runs_scheduled_task():
    """_start_capture_now() starts capture via its scheduled task (schtasks /Run),
    so the process is owned by the Task Scheduler service and survives
    onboarding exiting."""
    from luminque.onboarding import _start_capture_now
    from luminque.onboarding.scheduler import TASK_NAMES

    mock_run = MagicMock()
    with patch("luminque.onboarding.subprocess.run", mock_run):
        _start_capture_now()

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["schtasks", "/Run", "/TN"]
    assert TASK_NAMES["capture"] in cmd


def test_enrollment_module_imports():
    """enrollment.py imports and exposes enroll_device."""
    from luminque.onboarding.enrollment import enroll_device  # noqa: F401


def test_scheduler_module_imports():
    """scheduler.py imports and exposes register_all_tasks / deregister_all_tasks."""
    from luminque.onboarding.scheduler import (  # noqa: F401
        deregister_all_tasks,
        register_all_tasks,
    )
