"""
luminque.onboarding.enrollment — device enrollment against the Luminque server.

Calls POST /api/v1/devices/enroll and persists the returned credentials in
Windows Credential Manager so the sender can read them on every run.
"""

import platform
import socket

import keyring
import requests

# Must match the service name and key names in luminque/sender/credentials.py
KEYRING_SERVICE = "luminque-sender"
KEYRING_KEYS = {
    "auth_token":   "luminque_api_key",
    "endpoint_url": "luminque_endpoint_url",
    "tenant_id":    "luminque_tenant_id",
    "device_id":    "luminque_device_id",
}


def enroll_device(api_url: str, enrollment_token: str, tenant_id: str) -> dict:
    """
    POST /api/v1/devices/enroll and store returned credentials in keyring.

    Raises RuntimeError with a user-readable message on any failure.
    Returns the full response dict on success.
    """
    payload = {
        "tenant_id":         tenant_id,
        "hostname":          socket.gethostname(),
        "platform":          "windows",
        "os_version":        _get_os_version(),
        "enrollment_token":  enrollment_token,
    }

    try:
        resp = requests.post(
            f"{api_url.rstrip('/')}/api/v1/devices/enroll",
            json=payload,
            timeout=30,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Could not connect to {api_url}.\n\n"
            f"Check the server URL and your network connection.\n\n({exc})"
        ) from exc
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Connection to {api_url} timed out. Check the server URL and try again."
        )

    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Enrollment failed (HTTP {resp.status_code}):\n{detail}")

    data = resp.json()

    # The server assigns its own device_id; the sender must echo it back as
    # device_id in session/health payloads (the API validates it against the
    # token's device). It is NOT our local machine_id.
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["auth_token"],   data["auth_token"])
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["endpoint_url"], api_url)
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["tenant_id"],    data["tenant_id"])
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["device_id"],    str(data["device_id"]))

    return data


def _get_os_version() -> str:
    try:
        return f"{platform.release()} ({platform.version()})"
    except Exception:
        return "unknown"
