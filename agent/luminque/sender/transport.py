import logging

import requests

from luminque.sender.__version__ import SENDER_VERSION
from luminque.sender.constants import (
    API_HEARTBEAT_PATH,
    API_SCREENSHOTS_PATH,
    REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


def _headers(auth_token: str) -> dict:
    # Identity comes solely from X-Device-Token: the server derives agent_id
    # and tenant_id from it on every request.
    return {
        "X-Device-Token": auth_token,
        "X-Luminque-Sender-Version": SENDER_VERSION,
    }


def post_screenshot(
    png_bytes: bytes,
    captured_at: str,
    window_title: str | None,
    app_name: str | None,
    base_url: str,
    auth_token: str,
) -> requests.Response:
    """POST /v1/screenshots — one multipart request per frame.

    captured_at is the ISO-8601 UTC capture time; it is the server's dedupe
    key together with the agent identity, so 200 (duplicate) and 201 (stored)
    are both success for the caller.
    """
    url = base_url.rstrip("/") + API_SCREENSHOTS_PATH
    data = {"captured_at": captured_at}
    if window_title is not None:
        data["window_title"] = window_title
    if app_name is not None:
        data["app_name"] = app_name
    # No Content-Type header — requests sets the multipart boundary itself.
    return requests.post(
        url,
        files={"file": (f"{captured_at}.png", png_bytes, "image/png")},
        data=data,
        headers=_headers(auth_token),
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=True,
    )


def post_heartbeat(base_url: str, auth_token: str) -> requests.Response:
    """POST /v1/heartbeat — empty body; the token is the whole message."""
    url = base_url.rstrip("/") + API_HEARTBEAT_PATH
    return requests.post(
        url,
        headers=_headers(auth_token),
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=True,
    )
