"""
luminque.onboarding.enrollment — device enrollment against the Luminque server.

Calls POST /v1/enroll and persists the returned device token and the endpoint
URL in Windows Credential Manager so the sender can read them on every run.
Identity comes from the token: the server derives the tenant from the
enrollment token and later resolves agent/tenant from X-Device-Token, so
nothing else needs persisting locally.
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
}


def enroll_device(api_url: str, enrollment_token: str) -> dict:
    """
    POST /v1/enroll and store the returned device token in keyring.

    Raises RuntimeError with a user-readable message on any failure.
    Returns the full response dict on success.
    """
    payload = {
        "enrollment_token":  enrollment_token,
        "hostname":          socket.gethostname(),
        "platform":          "windows",
        "os_version":        _get_os_version(),
    }

    try:
        resp = requests.post(
            f"{api_url.rstrip('/')}/v1/enroll",
            json=payload,
            timeout=30,
            verify=True,
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

    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["auth_token"],   data["auth_token"])
    keyring.set_password(KEYRING_SERVICE, KEYRING_KEYS["endpoint_url"], api_url)

    return data


def _get_os_version() -> str:
    try:
        return f"{platform.release()} ({platform.version()})"
    except Exception:
        return "unknown"
