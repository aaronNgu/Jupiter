import logging

import requests

from luminque.sender.__version__ import SENDER_VERSION
from luminque.sender.constants import (
    API_EVENTS_PATH,
    API_HEALTH_PATH,
    API_SESSIONS_PATH,
    REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


def _headers(api_key: str) -> dict:
    # The ingestion API authenticates device routes via X-Device-Token (the
    # device auth_token from enrollment, which is what `api_key` holds).
    # Authorization is kept for any non-device middleware but is ignored by
    # require_device_auth.
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Device-Token": api_key,
        "X-Luminque-Sender-Version": SENDER_VERSION,
    }


def post_health(body: dict, base_url: str, api_key: str) -> requests.Response:
    url = base_url.rstrip("/") + API_HEALTH_PATH
    return requests.post(url, json=body, headers=_headers(api_key), timeout=REQUEST_TIMEOUT_SECONDS, verify=True)


def create_session(body: dict, base_url: str, api_key: str) -> requests.Response:
    url = base_url.rstrip("/") + API_SESSIONS_PATH
    return requests.post(url, json=body, headers=_headers(api_key), timeout=REQUEST_TIMEOUT_SECONDS, verify=True)


def post_events(session_id: str, body: dict, base_url: str, api_key: str) -> requests.Response:
    path = API_EVENTS_PATH.format(session_id=session_id)
    url = base_url.rstrip("/") + path
    return requests.post(url, json=body, headers=_headers(api_key), timeout=REQUEST_TIMEOUT_SECONDS, verify=True)


def post_media(session_id: str, filename: str, png_bytes: bytes, base_url: str, api_key: str) -> requests.Response:
    url = base_url.rstrip("/") + f"/api/v1/sessions/{session_id}/media"
    # No Content-Type here — requests sets the multipart boundary itself.
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Device-Token": api_key,
        "X-Luminque-Sender-Version": SENDER_VERSION,
    }
    return requests.post(url, files={"file": (filename, png_bytes, "image/png")}, headers=auth_headers, timeout=REQUEST_TIMEOUT_SECONDS, verify=True)
