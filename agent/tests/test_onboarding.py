"""
Tests for luminque.onboarding.

Key areas covered:
  - Module imports cleanly without opening a display
  - install_exe() returns dst unchanged when already at the target path
  - _start_capture_now() starts capture via its scheduled task
  - enroll_device() speaks the v1 contract: POST /v1/enroll with
    {enrollment_token, hostname, platform, os_version} — no tenant_id —
    and persists exactly auth_token + endpoint_url in keyring
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


def _mock_enroll_response(status_code=201):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"agent_id": "agent-uuid", "auth_token": "device-tok"}
    return resp


def test_enroll_device_posts_v1_contract_payload():
    """POST /v1/enroll with the four contract fields; identity comes from the
    enrollment token, so tenant_id must not appear in the payload."""
    from luminque.onboarding.enrollment import enroll_device

    with patch("luminque.onboarding.enrollment.requests.post",
               return_value=_mock_enroll_response()) as mock_post, \
         patch("luminque.onboarding.enrollment.keyring.set_password"):
        data = enroll_device(api_url="https://ingest.example.com/",
                             enrollment_token="enroll-tok")

    assert data["auth_token"] == "device-tok"
    assert mock_post.call_args[0][0] == "https://ingest.example.com/v1/enroll"
    payload = mock_post.call_args[1]["json"]
    assert payload == {
        "enrollment_token": "enroll-tok",
        "hostname": payload["hostname"],
        "platform": "windows",
        "os_version": payload["os_version"],
    }
    assert "tenant_id" not in payload
    assert mock_post.call_args[1]["verify"] is True


def test_enroll_device_persists_only_token_and_endpoint():
    """Keyring shrinks to two entries — tenant_id and device_id are no longer
    sent anywhere, so they are no longer stored."""
    from luminque.onboarding.enrollment import (
        KEYRING_KEYS,
        KEYRING_SERVICE,
        enroll_device,
    )

    with patch("luminque.onboarding.enrollment.requests.post",
               return_value=_mock_enroll_response()), \
         patch("luminque.onboarding.enrollment.keyring.set_password") as mock_set:
        enroll_device(api_url="https://ingest.example.com",
                      enrollment_token="enroll-tok")

    assert mock_set.call_args_list == [
        call(KEYRING_SERVICE, KEYRING_KEYS["auth_token"], "device-tok"),
        call(KEYRING_SERVICE, KEYRING_KEYS["endpoint_url"], "https://ingest.example.com"),
    ]


def test_enroll_device_raises_on_401():
    from luminque.onboarding.enrollment import enroll_device

    resp = MagicMock(status_code=401)
    resp.json.return_value = {"detail": "unknown enrollment token"}
    with patch("luminque.onboarding.enrollment.requests.post", return_value=resp), \
         patch("luminque.onboarding.enrollment.keyring.set_password") as mock_set:
        try:
            enroll_device(api_url="https://ingest.example.com", enrollment_token="bad")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "401" in str(e)
    mock_set.assert_not_called()


def test_scheduler_module_imports():
    """scheduler.py imports and exposes register_all_tasks / deregister_all_tasks."""
    from luminque.onboarding.scheduler import (  # noqa: F401
        deregister_all_tasks,
        register_all_tasks,
    )
