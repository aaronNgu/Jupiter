# luminque-sender Phase 1 — Technical Design Document

**Status:** Draft  
**Date:** 2026-05-05  
**Scope:** Phase 1 — raw event shipping, no PII scrubbing, no browser events

---

## 1. Overview & Purpose

`luminque-sender` is a lightweight Windows process responsible for:

1. Reading captured GUI interaction data from a local SQLite database produced by `openadapt-capture` (`luminque-capture`).
2. Mapping `ActionEvent` rows to the server's EventType schema and batching them for upload.
3. Creating a server-side session and POSTing events, screenshots, and a health heartbeat to the Luminque API over HTTPS.
4. Advancing a persistent cursor so each event is sent exactly once.
5. Cleaning up stale screenshot blobs from the local DB.
6. Providing a health/heartbeat signal on every run, even when there are no new events.

The sender is **not a daemon**. It is invoked by Windows Task Scheduler every 30–60 minutes, completes its work, and exits. There is no persistent process to manage.

Analysis of the captured data is async and retrospective. There is no real-time delivery requirement — losing a single cycle is acceptable; losing data is not.

---

## 2. Implementation Locations

All sender work lives in the `luminque-ops` repo. The `openadapt-capture` repo is a read-only dependency — no changes are made to it.

### Component-to-file map

| Component | File | Type |
|---|---|---|
| Entry point (`run()` stub) | `luminque/sender/__init__.py` | Existing stub — replace body |
| Top-level orchestration | `luminque/sender/sender.py` | New file |
| CLI entry / logging setup | `luminque/sender/__main__.py` | New file |
| Version constant | `luminque/sender/__version__.py` | New file |
| All magic numbers and string constants | `luminque/sender/constants.py` | New file |
| Cursor state load / atomic save | `luminque/sender/state.py` | New file |
| Capture DB access and batch query | `luminque/sender/db.py` | New file |
| Post-send screenshot cleanup | `luminque/sender/db.py` | New file (same module as DB access) |
| ActionEvent → server EventType mapping | `luminque/sender/events.py` | New file |
| Session lifecycle (POST /sessions, /events) | `luminque/sender/session.py` | New file |
| Screenshot multipart upload (/media) | `luminque/sender/media.py` | New file |
| Device health heartbeat (/devices/health) | `luminque/sender/health.py` | New file |
| HTTPS transport helpers | `luminque/sender/transport.py` | New file |
| Windows Credential Manager access | `luminque/sender/credentials.py` | New file |
| 24h screenshot blob retention cap | `luminque/sender/retention.py` | New file |
| Unit tests | `tests/test_sender.py` | Existing stub — populate |

### Suggested sender module file breakdown

As the sender grows, keep each file focused on a single responsibility:

```
luminque/sender/
  __init__.py         ← updated: calls run_sender() from sender.py
  __main__.py         ← new: logging setup + sys.exit(run_sender())
  __version__.py      ← new: SENDER_VERSION = "1.0.0"
  constants.py        ← new: MAX_BATCH_EVENTS, timeouts, filenames, etc.
  sender.py           ← new: run_sender() orchestration (steps 1–10 from §3)
  state.py            ← new: load_state() / save_state() (atomic .tmp write)
  db.py               ← new: open_capture_db(), query_batch(), cleanup_sent_screenshots()
  events.py           ← new: map_event_type(), serialize_event_for_server()
  session.py          ← new: create_session(), post_events_batch()
  media.py            ← new: upload_screenshot()
  health.py           ← new: build_health_payload(), post_health()
  transport.py        ← new: post_json(), post_multipart() with auth headers
  credentials.py      ← new: get_credential(), configure_credentials()
  retention.py        ← new: enforce_retention_cap()
```

No changes are needed in `luminque/main.py` (already routes `--send` to `from luminque.sender import run; run()`) or `pyproject.toml` (dependencies already declared). The `openadapt-capture` repo at `/Users/aaron_other/Documents/Luminque.nosync/openadapt-capture.nosync/` is imported via `openadapt_capture.db.models` but never modified.

---

## 3. Full Sender Flow

Each invocation of `luminque-sender` executes the following steps in order. All steps must complete successfully for the cursor to advance.

```
1.  Load state             Read sender_state.json → get last_sent_action_event_id
2.  Load credentials       Read device_id, tenant_id, auth_token from keyring
3.  Open capture DB        Connect (read-only) to recording.db
4.  Query new events       SELECT ActionEvent, Screenshot, WindowEvent
                           WHERE ActionEvent.id > last_sent_action_event_id
                           ORDER BY ActionEvent.id ASC
                           LIMIT MAX_BATCH_EVENTS (default: 5000)
5.  POST /api/v1/sessions  Body: { device_id, tenant_id, user_id: null,
                                   started_at, metadata: {} }
                           → session_id
6.  Map ActionEvents       Convert each ActionEvent to server EventType (see §5a)
7.  POST /sessions/{id}/events  In batches; body: { "events": [...] }
                           → { "ingested": N }
8.  POST /sessions/{id}/media   For each screenshot in batch:
                           multipart/form-data, field "file"
                           → { "filename": "...", "size": N }
9.  POST /devices/health   Body: health payload (see §10)
                           → { "received": true }
10. Handle overall outcome
    a. All POSTs 201       → advance cursor, nullify sent screenshot blobs, save state
    b. 4xx (non-retryable) → log error, do NOT advance cursor, exit non-zero
    c. 5xx / network err   → log error, do NOT advance cursor, exit non-zero
                              (Task Scheduler will retry on next schedule)
11. Enforce retention cap  Nullify png_data for screenshots older than 24h,
                           regardless of send status
12. Exit 0
```

Step 11 (retention cap) runs unconditionally — even if any POST failed — to prevent unbounded local storage growth.

All device endpoints use the header `X-Device-Token: <auth_token>`. The server does not currently validate this token but it must be sent for forward-compatibility.

The sender never calls `POST /api/v1/devices/enroll`. Enrollment is handled once by the onboarding process (see §11 for credential storage).

---

## 4. Cursor Tracking

### State File

The cursor is persisted in a JSON state file:

```
%APPDATA%\Luminque\sender_state.json
```

Schema:

```json
{
  "last_sent_action_event_id": 0,
  "last_successful_send_utc": "2026-05-05T10:00:00Z"
}
```

- `last_sent_action_event_id`: The `ActionEvent.id` of the last event **confirmed delivered** (i.e., all endpoints in the cycle returned 201). Initialized to `0` on first run.
- `last_successful_send_utc`: ISO 8601 UTC timestamp of the last confirmed send. Used for diagnostics and retention cap calculations.

### Cursor Semantics

The cursor is an inclusive lower-bound. The query fetches:

```sql
SELECT * FROM action_event
WHERE id > :last_sent_action_event_id
ORDER BY id ASC
LIMIT :max_batch_events;
```

On confirmed 201 responses from all endpoints in a cycle (session creation, events, media, health), the cursor is advanced to `max(ActionEvent.id)` from the batch just sent. If the batch was empty (heartbeat-only), the cursor does not change and `last_successful_send_utc` is updated.

The cursor is **never advanced on failure**. This guarantees at-least-once delivery. The server must be idempotent on re-receipt of the same events (deduplicate on `(device_id, action_event.id)`).

### State File Location

```python
import os
from pathlib import Path

def get_state_path() -> Path:
    appdata = os.environ["APPDATA"]
    return Path(appdata) / "Luminque" / "sender_state.json"
```

State is loaded at startup and written atomically (write to `.tmp`, then `os.replace()`) after a successful send.

---

## 5. API Request/Response Schemas

The sender makes four HTTPS calls per send cycle. All requests use `Content-Type: application/json` except the media upload which is `multipart/form-data`. All endpoints require the header `X-Device-Token: <auth_token>`.

### POST /api/v1/sessions

Creates a new session for this send cycle.

```json
// Request
{
  "device_id": "abc123",
  "tenant_id": "tenant-xyz",
  "user_id": null,
  "started_at": "2026-05-18T10:00:00Z",
  "metadata": {}
}

// Response 201
{
  "id": "<session_id>",
  "device_id": "abc123",
  "tenant_id": "tenant-xyz",
  "status": "recording"
}
```

`session_id` from the response is used in all subsequent calls for this cycle.

### POST /api/v1/sessions/{id}/events

Posts a batch of mapped events. Called once per batch (the sender may split large sets across multiple calls up to `MAX_BATCH_EVENTS`).

```json
// Request
{
  "events": [
    { "type": "click",        "timestamp": "2026-05-18T10:00:01Z", "payload": { "x": 540, "y": 320, "button": "left" } },
    { "type": "keypress",     "timestamp": "2026-05-18T10:00:02Z", "payload": { "key": "Return", "char": "\n" } },
    { "type": "scroll",       "timestamp": "2026-05-18T10:00:03Z", "payload": { "dx": 0, "dy": -120 } },
    { "type": "window_focus", "timestamp": "2026-05-18T10:00:04Z", "payload": { "app": "Firefox", "title": "Order Mgmt" } },
    { "type": "screenshot",   "timestamp": "2026-05-18T10:00:05Z", "payload": { "file": "screenshot_0001.png" } },
    { "type": "custom",       "timestamp": "2026-05-18T10:00:06Z", "payload": { <full ActionEvent fields> } }
  ]
}

// Response 201
{ "ingested": 142 }
```

**Note:** `PATCH /api/v1/sessions/{id}` is explicitly not implemented. Do not call it.

### POST /api/v1/sessions/{id}/media

Uploads a single screenshot as `multipart/form-data`. Called once per screenshot in the batch.

```
field name:   "file"
filename:     "screenshot_0001.png"
content-type: "image/png"

// Response 201
{ "filename": "screenshot_0001.png", "size": 204800 }
```

The filename must match the `payload.file` value used in the corresponding `screenshot` event.

### POST /api/v1/devices/health

See Section 10 for the full request/response schema.

### Error responses

| Status | Meaning |
|---|---|
| 401 | Missing or invalid `X-Device-Token` |
| 404 | Session or device not found |
| 400 | Invalid request body |

---

## 5a. ActionEvent → Server EventType Mapping

`openadapt-capture` produces `ActionEvent` rows with a `name` field. The server accepts a fixed `EventType` enum. The sender maps between them in `events.py`.

**Server EventType enum:** `start` | `stop` | `screenshot` | `click` | `keypress` | `scroll` | `window_focus` | `window_close` | `custom`

| openadapt-capture `ActionEvent.name` | Server `EventType` | Notes |
|---|---|---|
| `mouse.singleclick` | `click` | payload: `{ "x": mouse_x, "y": mouse_y, "button": mouse_button_name }` |
| `mouse.doubleclick` | `click` | Same payload as singleclick |
| `key.type` | `keypress` | payload: `{ "key": key_name, "char": key_char }` |
| `key.down` | `keypress` | Same payload as key.type |
| `key.up` | `keypress` | Same payload as key.type |
| `mouse.scroll` | `scroll` | payload: `{ "dx": mouse_dx, "dy": mouse_dy }` |
| `focus` (window events) | `window_focus` | payload: `{ "app": <process name>, "title": <window title> }` |
| `close` (window events) | `window_close` | Same payload as window_focus |
| screenshot events | `screenshot` | payload: `{ "file": "<filename>.png" }` |
| anything else | `custom` | payload: full ActionEvent fields dict |

`timestamp` for each event is the `ActionEvent.timestamp` converted to ISO 8601 UTC string.

For `screenshot` events, the filename is derived from `ActionEvent.screenshot_id` — e.g., `screenshot_{screenshot_id:04d}.png`. The same filename is used as the multipart `filename` when uploading via `/media`.

**Note:** `browser_event_id` and `browser_event_timestamp` columns exist in the DB schema but are omitted from all event payloads in Phase 1. See Section 14.

---

## 6. Screenshot Handling

### Reading from DB

`Screenshot.png_data` is a `LargeBinary` column containing raw PNG bytes. The sender reads these directly via SQLAlchemy and sends them as-is via multipart/form-data to `POST /api/v1/sessions/{id}/media`.

### Multipart Upload

Each screenshot in the batch is uploaded individually:

```python
import requests

def upload_screenshot(
    session_id: str,
    screenshot_id: int,
    png_bytes: bytes,
    base_url: str,
    auth_token: str,
) -> dict:
    filename = f"screenshot_{screenshot_id:04d}.png"
    response = requests.post(
        f"{base_url}/api/v1/sessions/{session_id}/media",
        headers={"X-Device-Token": auth_token},
        files={"file": (filename, png_bytes, "image/png")},
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=True,
    )
    response.raise_for_status()
    return response.json()  # { "filename": "...", "size": N }
```

The filename `screenshot_{screenshot_id:04d}.png` must exactly match the `payload.file` value used when posting the corresponding `screenshot` event to `/events`.

### Screenshot Deduplication Query

```python
def get_screenshots_for_batch(
    session: Session,
    screenshot_ids: set[int],
) -> list[Screenshot]:
    return (
        session.query(Screenshot)
        .filter(Screenshot.id.in_(screenshot_ids))
        .all()
    )
```

`screenshot_ids` is built by collecting all non-null `ActionEvent.screenshot_id` values from the current batch. Only screenshots whose `id` appears in at least one `ActionEvent.screenshot_id` within the batch are uploaded.

---

## 7. HTTPS Transport

### Transport Helpers (`transport.py`)

Two transport helpers cover all calls in the send cycle:

```python
import requests

def post_json(
    url: str,
    body: dict,
    auth_token: str,
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "X-Device-Token": auth_token,
        "X-Luminque-Sender-Version": SENDER_VERSION,
    }
    response = requests.post(
        url,
        json=body,
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=True,          # NEVER set verify=False
    )
    return response


def post_multipart(
    url: str,
    filename: str,
    png_bytes: bytes,
    auth_token: str,
) -> requests.Response:
    headers = {
        "X-Device-Token": auth_token,
        "X-Luminque-Sender-Version": SENDER_VERSION,
    }
    response = requests.post(
        url,
        headers=headers,
        files={"file": (filename, png_bytes, "image/png")},
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=True,
    )
    return response
```

**Security requirement:** `verify=True` is non-negotiable. The sender must validate the server's TLS certificate. Any code path that sets `verify=False` must be rejected in code review.

### Timeouts

- Default: 60 seconds via `REQUEST_TIMEOUT_SECONDS`.
- Media uploads may need a higher timeout for large screenshots. Phase 1 uses the same constant for all calls; adjust `REQUEST_TIMEOUT_SECONDS` in `constants.py` if needed based on observed latency in staging.

### Request Size Estimate

Rough estimates per call:
- `POST /sessions` body: negligible
- `POST /sessions/{id}/events` body: ~500 bytes per event uncompressed (JSON metadata only, no blobs)
- `POST /sessions/{id}/media` body: ~1–4 MB per screenshot (raw PNG, 1080p)
- `POST /devices/health` body: ~500 bytes

Screenshots are now sent as separate multipart uploads rather than embedded in the events payload, eliminating the base64 overhead from Phase 1's original design. Monitor per-screenshot upload times in staging.

---

## 8. Retry and Failure Handling

### Retry Strategy

The sender has **no internal retry loop**. It attempts the POST once per invocation. If it fails, it exits with a non-zero code. Windows Task Scheduler is configured to retry on failure (recommended: 3 retries, 5-minute intervals).

This simplifies the sender significantly: no backoff state, no threading, no partial retry complexity.

### Failure Classification

| HTTP Status | Action |
|---|---|
| 201 Created | Expected success for all sender endpoints. |
| 4xx (e.g., 400, 401, 404, 422) | Log error body. Do not retry — these indicate a configuration or schema problem. Exit 1. |
| 429 Too Many Requests | Log and exit 1. Task Scheduler will retry. Honor `Retry-After` header if present in a future phase. |
| 5xx | Log error. Do not advance cursor. Exit 1. Task Scheduler retries. |
| Connection error / timeout | Log exception. Do not advance cursor. Exit 1. |

A 401 typically means the `X-Device-Token` is missing or invalid — check keyring credentials.
A 404 on `/sessions/{id}/events` or `/media` means the session was not found — the cycle should be aborted and retried from session creation on the next run.

### Partial Batch Handling

Each send cycle is all-or-nothing at the cursor level. If any call in the cycle fails (session creation, events, media, or health), the cursor is not advanced and the full cycle is retried on the next run. The server must deduplicate on `(device_id, action_event.id)`.

### State Integrity

State file writes are atomic:

```python
import os
import json
from pathlib import Path

def save_state(state_path: Path, state: dict) -> None:
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, state_path)  # atomic on Windows (same drive)
```

If the process is killed between POST success and state file write, the cursor is not advanced and events are resent on the next cycle. The server must handle this via deduplication.

---

## 9. Local Retention Cap (24h)

Even if sends repeatedly fail (e.g., network outage, server downtime), screenshot blobs must not accumulate indefinitely. Screenshot data is large; `ActionEvent` and `WindowEvent` rows are small and are retained longer without risk.

### Policy

- **Screenshot blobs:** Deleted if the screenshot's `timestamp` is older than 24 hours from the current run time, regardless of whether they have been sent.
- **ActionEvent rows:** Retained indefinitely in Phase 1. These are small (~500 bytes each) and are needed to maintain cursor integrity.

### Implementation

```python
import time
from sqlalchemy.orm import Session
from openadapt_capture.db.models import Screenshot

RETENTION_SECONDS = 24 * 60 * 60  # 24 hours

def enforce_retention_cap(session: Session) -> int:
    """
    Nullify png_data for screenshots older than 24h.
    Returns count of rows affected.
    
    Note: rows are nullified (not deleted) to preserve ActionEvent foreign key
    references and maintain cursor correctness.
    """
    cutoff_timestamp = time.time() - RETENTION_SECONDS
    result = (
        session.query(Screenshot)
        .filter(Screenshot.timestamp < cutoff_timestamp)
        .filter(Screenshot.png_data != None)
        .update(
            {
                Screenshot.png_data: None,
                Screenshot.png_diff_data: None,
                Screenshot.png_diff_mask_data: None,
            },
            synchronize_session=False,
        )
    )
    session.commit()
    return result
```

**Why nullify instead of delete:** Deleting `Screenshot` rows would break the `ActionEvent.screenshot_id` foreign key, corrupting the event graph. Setting `png_data = NULL` retains the row and metadata while freeing the storage.

### Post-Send Cleanup

On confirmed 200 OK, the sender also nullifies `png_data` for all screenshots with `id <= max_screenshot_id_in_batch`. This is independent of the 24h cap and runs first:

```python
def cleanup_sent_screenshots(session: Session, max_screenshot_id: int) -> None:
    session.query(Screenshot).filter(
        Screenshot.id <= max_screenshot_id,
        Screenshot.png_data != None,
    ).update(
        {Screenshot.png_data: None, Screenshot.png_diff_data: None, Screenshot.png_diff_mask_data: None},
        synchronize_session=False,
    )
    session.commit()
```

`enforce_retention_cap` runs unconditionally after the send attempt (success or failure), so blobs are cleaned up even during extended outages.

---

## 10. Device Health Heartbeat

### Endpoint

`POST /api/v1/devices/health`

The health call is sent once per cycle, after all session/event/media calls complete. It reports device and sender health to the server.

### Request / Response

```json
// Request
{
  "device_id": "abc123",
  "tenant_id": "tenant-xyz",
  "overall_status": "healthy",
  "components": [
    {
      "name": "capture",
      "status": "healthy",
      "message": null,
      "last_check_at": "2026-05-18T10:00:00Z"
    }
  ],
  "queue_depth": 0,
  "last_successful_upload_at": "2026-05-18T09:30:00Z",
  "disk_usage_percent": 42.5,
  "reported_at": "2026-05-18T10:00:30Z"
}

// Response 201
{ "received": true, "reported_at": "2026-05-18T10:00:30Z" }
```

**`overall_status`** is `"healthy"` | `"degraded"` | `"unhealthy"`. Phase 1 logic:
- `"healthy"`: capture process is running and disk usage is below warning threshold.
- `"degraded"`: capture process is not running OR disk usage is above warning threshold.
- `"unhealthy"`: disk usage is above critical threshold.

### Field Sources

| Field | Type | How Obtained |
|---|---|---|
| `device_id` | string | Read from keyring (see §11) |
| `tenant_id` | string | Read from keyring (see §11) |
| `overall_status` | string | Derived from component statuses (see above) |
| `components[].name` | string | Always `"capture"` in Phase 1 |
| `components[].status` | string | `"healthy"` if `luminque-capture.exe` is running, else `"degraded"` |
| `components[].message` | string\|null | Error message if status != healthy, else `null` |
| `components[].last_check_at` | string (ISO 8601 UTC) | `datetime.now(timezone.utc).isoformat()` |
| `queue_depth` | integer | Count of unsent `ActionEvent` rows (rows with id > last_sent_action_event_id) |
| `last_successful_upload_at` | string\|null | `last_successful_send_utc` from state file, or `null` if never sent |
| `disk_usage_percent` | float | `shutil.disk_usage(appdata_dir).used / shutil.disk_usage(appdata_dir).total * 100` |
| `reported_at` | string (ISO 8601 UTC) | `datetime.now(timezone.utc).isoformat()` |

### Implementation (`health.py`)

```python
import shutil
import psutil
from datetime import datetime, timezone

def build_health_payload(
    device_id: str,
    tenant_id: str,
    appdata_dir: Path,
    queue_depth: int,
    last_successful_upload_at: str | None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    capture_running = _is_capture_running()
    disk = shutil.disk_usage(appdata_dir)
    disk_pct = disk.used / disk.total * 100

    component_status = "healthy" if capture_running else "degraded"
    if disk_pct >= DISK_CRITICAL_PERCENT:
        overall = "unhealthy"
    elif disk_pct >= DISK_WARNING_PERCENT or not capture_running:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "device_id": device_id,
        "tenant_id": tenant_id,
        "overall_status": overall,
        "components": [
            {
                "name": "capture",
                "status": component_status,
                "message": None if capture_running else "luminque-capture.exe not running",
                "last_check_at": now,
            }
        ],
        "queue_depth": queue_depth,
        "last_successful_upload_at": last_successful_upload_at,
        "disk_usage_percent": round(disk_pct, 1),
        "reported_at": now,
    }


def _is_capture_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == "luminque-capture.exe":
            return True
    return False
```

---

## 11. Credentials Management

### Enrollment vs. Sender

Device credentials are written **once** by the onboarding process, which calls `POST /api/v1/devices/enroll` and receives:

```json
{ "device_id": "...", "auth_token": "...", "expires_at": "..." }
```

The onboarding process stores these in Windows Credential Manager. **The sender never calls the enroll endpoint.** It only reads credentials that are already present.

### Keyring Keys

| Keyring username | Content | Required by |
|---|---|---|
| `device_id` | Device identifier returned by enrollment | All API requests (body field) |
| `tenant_id` | Tenant identifier provisioned at enrollment | All API requests (body field) |
| `auth_token` | Auth token returned by enrollment | `X-Device-Token` header on all requests |
| `endpoint_url` | Base URL of the Luminque server | Transport layer |

### Storage

All credentials are stored in **Windows Credential Manager** via the `keyring` library under the service name `luminque-sender`. This avoids plaintext credentials in config files, environment variables, or the registry.

```python
import keyring

SERVICE_NAME = "luminque-sender"

def get_credential(key: str) -> str:
    value = keyring.get_password(SERVICE_NAME, key)
    if value is None:
        raise RuntimeError(
            f"Missing credential '{key}' in Windows Credential Manager. "
            f"Complete device enrollment before running the sender."
        )
    return value
```

The sender reads `device_id`, `tenant_id`, `auth_token`, and `endpoint_url` at startup via `get_credential()`. If any are missing, it raises immediately with a clear error.

### Provisioning (onboarding side only)

The onboarding process writes credentials after enrollment:

```python
def configure_credentials(
    device_id: str,
    tenant_id: str,
    auth_token: str,
    endpoint_url: str,
) -> None:
    keyring.set_password(SERVICE_NAME, "device_id", device_id)
    keyring.set_password(SERVICE_NAME, "tenant_id", tenant_id)
    keyring.set_password(SERVICE_NAME, "auth_token", auth_token)
    keyring.set_password(SERVICE_NAME, "endpoint_url", endpoint_url)
```

This is invoked by the installer or onboarding wizard. The sender itself only reads credentials, never writes them.

### Keyring Backend

On Windows, `keyring` defaults to `WinVaultKeyring` (Windows Credential Manager). No additional configuration is needed. The credentials appear in Credential Manager under `Control Panel > Credential Manager > Windows Credentials` as generic credentials named `luminque-sender`.

---

## 12. Process Lifecycle

### Task Scheduler Configuration

The sender is registered as a Windows Scheduled Task:

- **Trigger:** On a schedule, every 30–60 minutes (configurable at deployment time).
- **Action:** `luminque-sender.exe` (or `python -m luminque_sender` if running from source).
- **Run As:** The logged-in user (so `%APPDATA%` resolves correctly and Credential Manager is accessible in the user's vault).
- **Settings:**
  - Start the task only if the user is logged on.
  - If the task is already running, do not start a new instance.
  - On failure: restart task, up to 3 times, with 5-minute delay.

### Entry Point

```python
# luminque_sender/__main__.py

import sys
import logging
from luminque_sender.sender import run_sender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(get_log_path()),
        logging.StreamHandler(sys.stdout),
    ],
)

if __name__ == "__main__":
    sys.exit(run_sender())
```

`run_sender()` returns `0` on success, `1` on any failure. Task Scheduler interprets the exit code for retry logic.

### Concurrency

The sender does not use locking. Windows Task Scheduler is configured with "If the task is already running, do not start a new instance." This is sufficient — the sender is designed to complete in well under 30 minutes for any realistic batch size.

### Log Location

```
%APPDATA%\Luminque\logs\sender-YYYY-MM-DD.log
```

Logs are rotated daily. The sender does not manage log rotation itself in Phase 1 — old logs accumulate. Phase 2 should add `RotatingFileHandler`.

---

## 13. Implementation Locations (Detailed)

### No changes needed in `openadapt-capture`

All sender logic is implemented entirely within `luminique-ops`. The `openadapt-capture` repo at `/Users/aaron_other/Documents/Luminque.nosync/openadapt-capture.nosync/` is consumed as a read-only dependency (installed via `pyproject.toml`). The sender imports from it (`openadapt_capture.db.models`) but makes no modifications to it.

### Files to create inside `luminque/sender/`

The current state of `/Users/aaron_other/Documents/Luminque.nosync/luminque-ops.nosync/luminque/sender/` is a single stub:

```
luminque/sender/
  __init__.py     ← exists (stub — run() not implemented)
```

The following files must be created in that directory:

| File | Responsibility |
|---|---|
| `luminque/sender/__init__.py` | **Replace stub.** Export `run() -> None` which calls `run_sender()` from `sender.py` and maps the return code to an exit. This is the entry point called by `luminque/main.py` via `--send`. |
| `luminque/sender/sender.py` | Top-level orchestration: calls load_state → load_credentials → open_capture_db → query_batch → create_session → post_events_batch → upload_screenshot (per screenshot) → post_health → handle response → cleanup_sent_screenshots → save_state → enforce_retention_cap. Returns `0` on success, `1` on failure. |
| `luminque/sender/__main__.py` | Logging setup (FileHandler to `%APPDATA%\Luminque\logs\sender-YYYY-MM-DD.log` + StreamHandler) and `sys.exit(run_sender())`. Allows `python -m luminque.sender` invocation during development. |
| `luminque/sender/__version__.py` | Single constant: `SENDER_VERSION = "1.0.0"`. |
| `luminque/sender/constants.py` | All magic numbers and strings: `MAX_BATCH_EVENTS`, `RETENTION_SECONDS`, `REQUEST_TIMEOUT_SECONDS`, `KEYRING_SERVICE_NAME`, `DB_FILENAME`, `STATE_FILENAME`, `LOG_DIR`, `DISK_WARNING_PERCENT`, `DISK_CRITICAL_PERCENT`. See Appendix B for exact values. |
| `luminque/sender/state.py` | `load_state(state_path: Path) -> dict` and `save_state(state_path: Path, state: dict) -> None`. Atomic write via `.tmp` + `os.replace()`. Manages `sender_state.json` at `%APPDATA%\Luminque\sender_state.json`. |
| `luminque/sender/db.py` | `open_capture_db(db_path: Path) -> Session`, `query_batch(session, last_id, limit) -> tuple[list[ActionEvent], list[Screenshot], list[WindowEvent]]`, and `cleanup_sent_screenshots(session, max_screenshot_id)`. Reads `recording.db` in read-only mode via SQLAlchemy; imports models from `openadapt_capture.db.models`. |
| `luminque/sender/events.py` | `map_event_type(action_event: ActionEvent) -> str` and `serialize_event_for_server(action_event: ActionEvent) -> dict`. Converts `ActionEvent.name` to server `EventType` enum and builds the per-event dict (type, timestamp, payload) per the mapping table in §5a. |
| `luminque/sender/session.py` | `create_session(device_id, tenant_id, base_url, auth_token) -> str` (returns session_id) and `post_events_batch(session_id, events, base_url, auth_token) -> int` (returns ingested count). Calls `POST /api/v1/sessions` and `POST /api/v1/sessions/{id}/events`. |
| `luminque/sender/media.py` | `upload_screenshot(session_id, screenshot_id, png_bytes, base_url, auth_token) -> dict`. Posts to `POST /api/v1/sessions/{id}/media` as `multipart/form-data`. Filename is `screenshot_{screenshot_id:04d}.png`. |
| `luminque/sender/health.py` | `build_health_payload(device_id, tenant_id, appdata_dir, queue_depth, last_successful_upload_at) -> dict` and `post_health(payload, base_url, auth_token) -> dict`. Calls `POST /api/v1/devices/health`. |
| `luminque/sender/transport.py` | `post_json(url, body, auth_token) -> requests.Response` and `post_multipart(url, filename, png_bytes, auth_token) -> requests.Response`. Sets `X-Device-Token` and `X-Luminque-Sender-Version` headers. `verify=True` always. |
| `luminque/sender/credentials.py` | `get_credential(key: str) -> str` and `configure_credentials(device_id, tenant_id, auth_token, endpoint_url) -> None`. Reads/writes to Windows Credential Manager via `keyring` under service name `luminque-sender`. Keyring keys: `device_id`, `tenant_id`, `auth_token`, `endpoint_url`. |
| `luminque/sender/retention.py` | `enforce_retention_cap(session: Session) -> int`. Nullifies `png_data`, `png_diff_data`, `png_diff_mask_data` on `Screenshot` rows older than 24 hours. Returns count of rows affected. |

### Other files in `luminique-ops` that need to be touched

| File | Change required |
|---|---|
| `luminque/sender/__init__.py` | Replace the current stub `run()` with a real implementation that imports and calls `run_sender()` from `luminque/sender/sender.py`. The signature `run() -> None` must be preserved — `luminque/main.py` calls it directly with no return value check. |
| `luminque/main.py` | No changes needed. It already routes `--send` to `from luminque.sender import run; run()`. |
| `pyproject.toml` | No new dependencies needed — `requests`, `psutil`, and `keyring` are already declared. Verify `openadapt-capture` is pinned to a commit that includes the `ActionEvent`, `Screenshot`, and `WindowEvent` models used by `db.py`. |
| `tests/test_sender.py` | Already exists as a stub. Populate with unit tests covering: cursor advance on 201, no advance on 4xx/5xx, atomic state write, retention cap nullification, health payload assembly, ActionEvent → EventType mapping, session creation, media upload. |

### Summary tree after implementation

```
luminque-ops/
  luminque/
    sender/
      __init__.py         ← updated (calls run_sender, not print stub)
      __main__.py         ← new
      __version__.py      ← new
      constants.py        ← new
      sender.py           ← new (orchestration)
      state.py            ← new
      db.py               ← new
      events.py           ← new (ActionEvent → EventType mapping)
      session.py          ← new (POST /sessions, /events)
      media.py            ← new (POST /sessions/{id}/media)
      health.py           ← new (POST /devices/health)
      transport.py        ← new (post_json, post_multipart)
      credentials.py      ← new
      retention.py        ← new
    main.py               ← no changes
  tests/
    test_sender.py        ← populate (file already exists)
  pyproject.toml          ← no changes needed
```

---

## 14. Out of Scope for Phase 1

The following are explicitly deferred to Phase 2 or later:

### No PII Scrubbing

Raw data is sent as-is. `ActionEvent.key_char`, `ActionEvent.key_name`, `WindowEvent.title`, `WindowEvent.state`, and all other fields are transmitted without any redaction. PII scrubbing (via `openadapt-privacy` or equivalent) is a Phase 2 requirement.

### No Browser Events

`BrowserEvent` rows are not queried, serialized, or included in the payload. The `browser_event_id` and `browser_event_timestamp` columns on `ActionEvent` are present in the DB but are not included in the Phase 1 payload schema. The server schema should not expect them.

### No Audio Data

`AudioInfo` rows (`flac_data`, `transcribed_text`) are not sent. Audio is high-value but large; deferring to a later phase when a separate audio upload path can be designed.

### No Server-Side Deduplication Logic (Sender Responsibility)

The sender does not implement deduplication on its end. The server is responsible for idempotent ingestion.

### No Real-Time or Push Delivery

The sender is scheduled-pull only. No WebSocket, SSE, or long-polling mechanism.

### No Compression Format Negotiation

Phase 1 always sends gzip. No `Accept-Encoding` negotiation.

### No Payload Size Splitting

If a batch exceeds practical HTTP size limits (hypothetically, e.g., 5000 events each with a 4MB screenshot), Phase 1 does not split it into sub-batches. `MAX_BATCH_EVENTS` should be tuned conservatively enough to avoid this. Phase 2 should add adaptive batch splitting.

### No Windows Event Log Integration

Errors are written to the file log only. Integration with Windows Event Log (for IT monitoring via SCCM/Intune) is a future enhancement.

---

## Appendix A: File and Directory Layout

```
%APPDATA%\Luminque\
  sender_state.json                 # Cursor + last send timestamp
  recordings\
    recording.db                    # Written by luminque-capture (single file, appended across restarts)
    recording.db-wal
    recording.db-shm
  logs\
    sender-2026-05-05.log
```

`device_id`, `tenant_id`, and `auth_token` are stored in Windows Credential Manager (not as flat files). See §11.

---

## Appendix B: Constants

```python
# luminque_sender/constants.py

SENDER_VERSION = "1.0.0"
MAX_BATCH_EVENTS = 5000
RETENTION_SECONDS = 24 * 60 * 60   # 24 hours
REQUEST_TIMEOUT_SECONDS = 60
KEYRING_SERVICE_NAME = "luminque-sender"
DB_FILENAME = "recording.db"
STATE_FILENAME = "sender_state.json"
LOG_DIR = "logs"
DISK_WARNING_PERCENT = 80.0
DISK_CRITICAL_PERCENT = 95.0
```

---

## Appendix C: Module Structure

```
luminque_sender/
  __init__.py
  __main__.py          # Entry point, logging setup, sys.exit(run_sender())
  __version__.py       # SENDER_VERSION = "1.0.0"
  constants.py         # All magic numbers and string constants
  sender.py            # run_sender() — top-level orchestration
  state.py             # load_state(), save_state()
  db.py                # open_capture_db(), query_batch(), cleanup_sent_screenshots()
  events.py            # map_event_type(), serialize_event_for_server()
  session.py           # create_session(), post_events_batch()
  media.py             # upload_screenshot()
  health.py            # build_health_payload(), post_health()
  transport.py         # post_json(), post_multipart()
  credentials.py       # get_credential(), configure_credentials()
  retention.py         # enforce_retention_cap()
```

---

## Appendix D: Key Function Signatures

```python
# sender.py
def run_sender() -> int: ...
    """Main entry point. Returns 0 on success, 1 on failure."""

# state.py
def load_state(state_path: Path) -> dict: ...
def save_state(state_path: Path, state: dict) -> None: ...

# db.py
def open_capture_db(db_path: Path) -> Session: ...
def query_batch(
    session: Session,
    last_id: int,
    limit: int = MAX_BATCH_EVENTS,
) -> tuple[list[ActionEvent], list[Screenshot], list[WindowEvent]]: ...
def cleanup_sent_screenshots(session: Session, max_screenshot_id: int) -> None: ...

# events.py
def map_event_type(action_event: ActionEvent) -> str: ...
def serialize_event_for_server(action_event: ActionEvent) -> dict: ...
    """Returns { "type": ..., "timestamp": ..., "payload": { ... } }"""

# session.py
def create_session(
    device_id: str,
    tenant_id: str,
    base_url: str,
    auth_token: str,
) -> str: ...
    """POSTs /api/v1/sessions. Returns session_id."""

def post_events_batch(
    session_id: str,
    events: list[dict],
    base_url: str,
    auth_token: str,
) -> int: ...
    """POSTs /api/v1/sessions/{id}/events. Returns ingested count."""

# media.py
def upload_screenshot(
    session_id: str,
    screenshot_id: int,
    png_bytes: bytes,
    base_url: str,
    auth_token: str,
) -> dict: ...
    """POSTs /api/v1/sessions/{id}/media. Returns { filename, size }."""

# health.py
def build_health_payload(
    device_id: str,
    tenant_id: str,
    appdata_dir: Path,
    queue_depth: int,
    last_successful_upload_at: str | None,
) -> dict: ...
def post_health(
    payload: dict,
    base_url: str,
    auth_token: str,
) -> dict: ...
    """POSTs /api/v1/devices/health. Returns { received, reported_at }."""

# transport.py
def post_json(url: str, body: dict, auth_token: str) -> requests.Response: ...
def post_multipart(url: str, filename: str, png_bytes: bytes, auth_token: str) -> requests.Response: ...

# credentials.py
def get_credential(key: str) -> str: ...
def configure_credentials(
    device_id: str,
    tenant_id: str,
    auth_token: str,
    endpoint_url: str,
) -> None: ...

# retention.py
def enforce_retention_cap(session: Session) -> int: ...
```
