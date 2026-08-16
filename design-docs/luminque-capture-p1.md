# luminque-capture Phase 1 — Technical Design Document

**Status:** Draft  
**Date:** 2026-05-05  
**Author:** Aaron (aaronnfw@gmail.com)

---

## 1. Overview & Purpose

`luminque-capture` is a lightweight background agent that runs on Windows machines to record GUI interactions for aggregate, **asynchronous** business process analysis. It is **not** a real-time monitoring system. Analysis is retrospective: recordings are written to disk, and a separate analysis pipeline processes them later to reconstruct end-to-end business workflows across multiple applications (e.g., tracking an order through an ERP, a browser, and a file system).

Phase 1 focuses exclusively on the capture side: starting reliably on user login, recording mouse and keyboard events with action-gated screenshots, persisting everything to a local SQLite database, and restarting cleanly after crashes or excessive memory use.

---

## 2. Upstream: openadapt-capture

`luminque-capture` is a thin wrapper and configuration layer on top of the open-source library `openadapt-capture` located at `openadapt-capture.nosync/`. The upstream library is used **as-is except for one modification** described in Section 3. Do not diverge from upstream beyond that single change.

The entry point in the upstream library is `recorder.py` → `record()`. That function:

1. Creates a `Recording` row in a per-capture SQLite database.
2. Spawns reader threads: `read_screen_events`, `read_keyboard_events`, `read_mouse_events`, (optionally `read_window_events`, `run_browser_event_server`).
3. Spawns a `process_events` thread that drains `event_q` and fans out to typed write queues.
4. Spawns writer processes for each event type: `screen_event_writer`, `action_event_writer`, `window_event_writer`, `browser_event_writer`, `video_writer`, `audio_recorder`.
5. Runs until `terminate_processing` is set, then drains all write queues before exiting.

---

## 3. The One Modification: Action-Gated Screenshots

### What the upstream code does today

`read_screen_events()` (recorder.py, line 793) runs in its own thread and captures screenshots on a **fixed FPS timer**, entirely independently of user activity:

```python
# recorder.py lines 819–835
while not terminate_processing.is_set():
    t_start = time.perf_counter()
    screenshot = utils.take_screenshot()
    ...
    event_q.put(Event(utils.get_timestamp(), "screen", screenshot))
    if min_interval > 0:
        elapsed = time.perf_counter() - t_start
        sleep_time = min_interval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
```

At the default `SCREEN_CAPTURE_FPS = 10`, this produces ~600 screenshot objects per minute regardless of whether the user is doing anything.

The `process_events` thread (line 325) already implements a form of action-gating on the **write** side — it only flushes `prev_screen_event` to the DB when an action event arrives:

```python
# recorder.py lines 325–334
if prev_saved_screen_timestamp < prev_screen_event.timestamp:
    process_event(prev_screen_event, screen_write_q, ...)
    prev_saved_screen_timestamp = prev_screen_event.timestamp
```

But this does **not** prevent the screenshots from being captured and held in memory. During idle periods, `prev_screen_event` is continuously overwritten with fresh PIL `Image` objects that are never GC'd until the next assignment. Under Python's reference counting this is usually fine, but large screenshots (e.g., 4K monitors) held across the frame interval still consume significant RSS.

### The change

Replace the timer-loop in `read_screen_events` with an event-triggered model: a screenshot is only taken when a mouse or keyboard event fires. This eliminates idle frame captures entirely.

**Implementation plan:**

1. Add a `threading.Event` called `action_event` to `recorder.py` (or pass it in as a parameter alongside `event_q`).

2. In `trigger_action_event()` (line 646), set `action_event` after putting the event on the queue:
   ```python
   def trigger_action_event(event_q, action_event_args, *, screenshot_trigger=None):
       ...
       event_q.put(Event(utils.get_timestamp(), "action", action_event_args))
       if screenshot_trigger is not None:
           screenshot_trigger.set()
   ```

3. Replace the body of `read_screen_events()` with a wait-then-capture pattern:
   ```python
   def read_screen_events(
       event_q, terminate_processing, recording, started_event,
       screenshot_trigger=None, _screen_timing=None,
   ):
       utils.set_start_time(recording.timestamp)
       logger.info("Starting (action-gated mode)")
       started_event.set()  # signal ready immediately; no initial screenshot needed

       while not terminate_processing.is_set():
           # Block until an action fires or termination is requested
           triggered = screenshot_trigger.wait(timeout=1.0)
           if not triggered:
               continue  # timeout — just check terminate flag and loop
           screenshot_trigger.clear()

           t_start = time.perf_counter()
           screenshot = utils.take_screenshot()
           t_screenshot = time.perf_counter()

           if screenshot is None:
               logger.warning("Screenshot was None")
               continue

           event_q.put(Event(utils.get_timestamp(), "screen", screenshot))

           if _screen_timing is not None:
               t_end = time.perf_counter()
               _screen_timing.append((t_screenshot - t_start, t_end - t_start))

       logger.info("Done")
   ```

4. In `record()` (line 1388), create `screenshot_trigger = threading.Event()` and thread it through:
   - Pass `screenshot_trigger` to both `read_screen_events` (via `args`) and to every call path that reaches `trigger_action_event` (on_move, on_click, on_scroll, handle_key).

5. Remove the `SCREEN_CAPTURE_FPS` usage from this code path. The config key can remain for upstream compatibility but is unused by Luminque.

**Timing note:** The screenshot is captured synchronously on the action thread immediately after the event is queued. This means the screenshot timestamp will be fractionally later than the action timestamp — that is acceptable. `process_events` already handles the association via `prev_screen_event`.

**Race condition:** If two actions fire in rapid succession before the screenshot thread wakes, `screenshot_trigger.set()` is idempotent (second set is a no-op). Only one screenshot is taken per burst. This is the desired behavior — it bounds screenshot rate naturally to the speed of `utils.take_screenshot()` (~50–150ms on typical hardware).

---

## 4. Configuration

All flags are set via environment variables or a `.env` file loaded by `openadapt_capture/config.py` (pydantic-settings). Luminque sets the following at process startup before calling `record()`.

| Config key | Luminque value | Reason |
|---|---|---|
| `RECORD_VIDEO` | `False` | No video pipeline needed; screenshots per action are sufficient |
| `RECORD_AUDIO` | `False` | Out of scope for P1 |
| `RECORD_BROWSER_EVENTS` | `False` | Browser extension not deployed in P1 |
| `RECORD_WINDOW_DATA` | `True` | Window title + geometry needed to identify application context |
| `RECORD_READ_ACTIVE_ELEMENT_STATE` | `False` | Requires UI Automation accessibility; accept UIPI limitation gracefully |
| `RECORD_IMAGES` | `True` | PNG data must be written to DB (otherwise screenshot rows contain no pixel data) |
| `RECORD_FULL_VIDEO` | `False` | Video disabled |
| `SCREEN_CAPTURE_FPS` | N/A (unused) | Replaced by action-gating; value is irrelevant but leave default `10.0` |
| `LOG_MEMORY` | `False` | Perf overhead not wanted in production |
| `PLOT_PERFORMANCE` | `False` | No UI; also suppresses the `memory_writer` process |
| `DB_ECHO` | `False` | SQLAlchemy SQL logging disabled |

Setting these at startup using `config_override` from `config.py`:

```python
from openadapt_capture.config import RecordingConfig, config_override

luminque_config = RecordingConfig(
    capture_video=False,
    capture_audio=False,
    capture_browser_events=False,
    capture_window_data=True,
    capture_images=True,
    capture_full_video=False,
    log_memory=False,
    plot_performance=False,
)

with config_override(luminque_config):
    record(task_description="luminque-background", capture_dir=capture_dir)
```

The assertion at recorder.py line 1420 requires `RECORD_VIDEO or RECORD_IMAGES` — setting `RECORD_IMAGES=True` satisfies this.

---

## 5. Data Schema

All data is written to a SQLite file named `recording.db` inside the capture directory. The schema is defined in `openadapt_capture/db/models.py`.

### 5.1 `recording` table

One row per recording session (process lifetime or explicit stop).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `timestamp` | NUMERIC(10,2) | Unix timestamp at session start |
| `monitor_width` | INTEGER | Primary monitor width in pixels |
| `monitor_height` | INTEGER | Primary monitor height in pixels |
| `double_click_interval_seconds` | NUMERIC | From OS settings |
| `double_click_distance_pixels` | NUMERIC | From OS settings |
| `platform` | TEXT | `"win32"` on Windows |
| `task_description` | TEXT | `"luminque-background"` for background sessions |
| `config` | JSON | Reserved; currently unused |

### 5.2 `action_event` table

One row per mouse or keyboard event. This is the primary analysis table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `recording_id` | FK → recording.id | |
| `timestamp` | NUMERIC(10,2) | Unix timestamp of the event |
| `recording_timestamp` | NUMERIC(10,2) | Timestamp of the parent recording |
| `name` | TEXT | Event type: `"move"`, `"click"`, `"scroll"`, `"press"`, `"release"` |
| `mouse_x` / `mouse_y` | NUMERIC | Cursor position (mouse events) |
| `mouse_dx` / `mouse_dy` | NUMERIC | Scroll delta (scroll events) |
| `mouse_button_name` | TEXT | `"left"`, `"right"`, `"middle"` (click events) |
| `mouse_pressed` | BOOLEAN | True=down, False=up (click events) |
| `key_name` | TEXT | Key name (keyboard events) |
| `key_char` | TEXT | Character produced (keyboard events) |
| `key_vk` | TEXT | Virtual key code (keyboard events) |
| `canonical_key_name/char/vk` | TEXT | Layout-normalized key (keyboard events) |
| `screenshot_timestamp` | NUMERIC | Timestamp of the associated screenshot |
| `screenshot_id` | FK → screenshot.id | Populated during post-processing |
| `window_event_timestamp` | NUMERIC | Timestamp of the active window at event time |
| `window_event_id` | FK → window_event.id | Populated during post-processing |
| `element_state` | JSON | Null in P1 (RECORD_READ_ACTIVE_ELEMENT_STATE=False) |
| `disabled` | BOOLEAN | Default False; reserved for analysis filters |

### 5.3 `screenshot` table

One row per action-gated screenshot.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `recording_id` | FK → recording.id | |
| `timestamp` | NUMERIC(10,2) | Capture time (after action that triggered it) |
| `recording_timestamp` | NUMERIC(10,2) | |
| `png_data` | BLOB | Full PNG bytes of the screenshot |
| `png_diff_data` | BLOB | Nullable; pixel diff from previous screenshot (populated offline) |
| `png_diff_mask_data` | BLOB | Nullable; diff mask (populated offline) |

`png_diff_data` and `png_diff_mask_data` are not populated during capture; they are reserved for an offline analysis step.

### 5.4 `window_event` table

One row per window focus or geometry change. Deduplicated by title+window_id in `read_window_events`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `recording_id` | FK → recording.id | |
| `timestamp` | NUMERIC(10,2) | |
| `title` | TEXT | Window title bar text |
| `window_id` | TEXT | OS-level window handle as string |
| `left` / `top` / `width` / `height` | INTEGER | Window geometry |
| `state` | JSON | Full accessibility tree snapshot; null when RECORD_READ_ACTIVE_ELEMENT_STATE=False |

### 5.5 Other tables

- `browser_event`: Not populated in P1.
- `audio_info`: Not populated in P1.
- `performance_stat`: Populated by `performance_stats_writer`; records (event_type, start_time, end_time) for each write operation. Useful for diagnosing write latency.
- `memory_stat`: Not populated in P1 (PLOT_PERFORMANCE=False suppresses the `memory_writer` process).

---

## 6. Memory Management

### Bounded queues

The upstream `record()` function (line 1444) creates unbounded queues:

```python
event_q = queue.Queue()           # inbound from all readers
screen_write_q = sq.SynchronizedQueue()
action_write_q = sq.SynchronizedQueue()
window_write_q = sq.SynchronizedQueue()
...
```

For Luminque, these must be created with `maxsize=50`:

```python
event_q = queue.Queue(maxsize=50)
screen_write_q = sq.SynchronizedQueue(maxsize=50)   # requires SynchronizedQueue change — see below
action_write_q = sq.SynchronizedQueue(maxsize=50)
window_write_q = sq.SynchronizedQueue(maxsize=50)
perf_q = sq.SynchronizedQueue(maxsize=50)
```

`SynchronizedQueue.__init__` (extensions/synchronized_queue.py, line 74) currently passes no `maxsize`:

```python
def __init__(self) -> None:
    super().__init__(ctx=multiprocessing.get_context())
```

This needs a second modification: accept and forward `maxsize`:

```python
def __init__(self, maxsize=0) -> None:
    super().__init__(maxsize=maxsize, ctx=multiprocessing.get_context())
```

With `maxsize=50`, producers block when the queue is full. This provides back-pressure: if the writer process falls behind, the reader thread that calls `event_q.put()` will block. Because `read_screen_events` is action-gated and each screenshot is ~1–4MB of PIL Image data, the in-flight memory ceiling becomes approximately `50 × 4MB = 200MB` across all queues combined, well within the 500MB watchdog threshold.

### Explicit PIL Image close

`write_screen_event` (recorder.py, line 382) writes the PIL Image to PNG bytes but does not close the image:

```python
def write_screen_event(db, recording, event, perf_q):
    image = event.data
    if config.RECORD_IMAGES:
        with io.BytesIO() as output:
            image.save(output, format="PNG")
            png_data = output.getvalue()
        event_data = {"png_data": png_data}
    ...
```

Add an explicit close after writing:

```python
    if config.RECORD_IMAGES:
        with io.BytesIO() as output:
            image.save(output, format="PNG")
            png_data = output.getvalue()
        image.close()           # release native pixel buffer
        event_data = {"png_data": png_data}
```

PIL's `Image.close()` releases the file handle and internal buffer immediately under CPython rather than waiting for GC. On Windows with large screen resolutions this is measurable.

### Watchdog RSS limit

A separate watchdog process monitors the capture process RSS (via `psutil.Process.memory_info().rss`). If RSS exceeds 500MB, the watchdog sends `SIGTERM` (or `taskkill /F /PID` on Windows) and restarts the process. The watchdog is described in Section 7.

---

## 7. Storage Location and File Structure

### Root directory

```
%APPDATA%\Luminque\recordings\
```

Resolved at runtime:

```python
import os
base_dir = os.path.join(os.environ["APPDATA"], "Luminque", "recordings")
```

### Per-session capture directory

Each recording session gets its own subdirectory named by session start timestamp:

```
%APPDATA%\Luminque\recordings\
    <unix_timestamp>/
        recording.db         ← SQLite database (all tables)
```

At 10fps-equivalent with action-gating on a busy workday (~50,000 actions/day, ~500KB average PNG), a day's `recording.db` will be approximately 25GB. Storage rotation policy is out of scope for P1 — flag for P2.

### Passing the capture directory

```python
import os, time

session_ts = int(time.time())
capture_dir = os.path.join(base_dir, str(session_ts))
os.makedirs(capture_dir, exist_ok=True)

record(task_description="luminque-background", capture_dir=capture_dir)
```

`record()` calls `create_recording()` (line 1001) which calls `os.makedirs(capture_dir, exist_ok=True)` internally, so the makedirs call above is just a safety guard.

---

## 8. Process Lifecycle

### 8.1 Start on login

Managed by Windows Task Scheduler (`schtasks`). The task is created once during installation:

```
schtasks /Create /TN "Luminque\Capture" /TR "<python_exe> -m luminque_capture" /SC ONLOGON /RU "%USERNAME%" /F
```

- `ONLOGON` fires once per interactive login session.
- Runs as the logged-in user — no admin elevation required.
- The task should be set to **restart on failure** with a 30-second delay and up to 3 retries before the watchdog takes over.

### 8.2 Normal stop

The process respects the upstream stop sequences. For background operation, Luminque sends `terminate_processing.set()` through a signal handler or a named pipe sentinel. A clean stop drains all write queues before exit — upstream code handles this in the `while not terminate_processing.is_set() or not write_q.empty()` loops inside each writer process.

To stop from Task Scheduler or an installer: `taskkill /IM luminque_capture.exe /F`.

### 8.3 Crash and restart

The watchdog is a separate lightweight process that:
1. Monitors the capture process PID via `psutil`.
2. If the process is not running, restarts it after a 5-second backoff.
3. If RSS > 500MB, kills and restarts the process.

```python
# watchdog sketch (runs as a sibling Task Scheduler entry)
import psutil, subprocess, time, os

CAPTURE_CMD = ["python", "-m", "luminque_capture"]
RSS_LIMIT_BYTES = 500 * 1024 * 1024  # 500 MB
CHECK_INTERVAL = 10  # seconds

def start_capture():
    return subprocess.Popen(CAPTURE_CMD)

proc = start_capture()
while True:
    time.sleep(CHECK_INTERVAL)
    try:
        p = psutil.Process(proc.pid)
        rss = p.memory_info().rss
        if rss > RSS_LIMIT_BYTES:
            p.terminate()
            proc.wait(timeout=10)
            proc = start_capture()
    except psutil.NoSuchProcess:
        time.sleep(5)
        proc = start_capture()
```

The watchdog itself should be registered as a separate ONLOGON task with restart-on-failure enabled.

### 8.4 Process tree

On a running system:

```
svchost.exe (Task Scheduler)
  └── python.exe  (watchdog)
  └── python.exe  (luminque-capture / record())
        ├── thread: read_screen_events      (waits on screenshot_trigger)
        ├── thread: read_keyboard_events    (pynput Listener)
        ├── thread: read_mouse_events       (pynput Listener)
        ├── thread: read_window_events      (polls active window ~10/s)
        ├── thread: process_events          (drains event_q, fans to write queues)
        ├── process: screen_event_writer    (drains screen_write_q → SQLite)
        ├── process: action_event_writer    (drains action_write_q → SQLite)
        ├── process: window_event_writer    (drains window_write_q → SQLite)
        └── process: perf_stats_writer      (drains perf_q → SQLite)
```

---

## 9. Known Limitations

### 9.1 UIPI (User Interface Privilege Isolation)

Windows UIPI prevents a normal-user process from reading accessibility data (WM_GETTEXT, UI Automation) from windows running at higher integrity levels (administrator elevation). Concretely: if the user Alt-Tabs into an elevated `cmd.exe` or a UAC-elevated application, `window.get_active_window_data()` and `window.get_active_element_state()` will fail with an access-denied or empty result.

**Mitigation:** Wrap all window data collection calls in a broad `try/except Exception` and log at `DEBUG` level. Return an empty dict / None on failure. Do not crash. `RECORD_READ_ACTIVE_ELEMENT_STATE = False` (set in P1 config) already disables the more invasive UI Automation path; only `get_active_window_data()` needs the guard.

The action and screenshot stream is not affected — pynput hooks and `PIL.ImageGrab` operate at a lower level and are not subject to UIPI.

### 9.2 No browser event capture

`RECORD_BROWSER_EVENTS = False`. The WebSocket bridge to the browser extension is not started. URL, DOM, and network-level context is not available in P1. Business process analysis relying on URL context (e.g., distinguishing ERP screens) must use window titles only.

### 9.3 Multi-monitor screenshots

`utils.take_screenshot()` uses `PIL.ImageGrab.grab()` with no arguments, which captures the primary monitor only on some PIL versions. Verify behavior against the installed PIL version on target machines. Multi-monitor support is not a P1 requirement but should not silently capture the wrong screen.

### 9.4 Stop sequence not disabled

The upstream stop sequences (`"oa.stop"`, triple-Ctrl) are active. In background operation, a user could accidentally trigger them. For P1 this is acceptable; set `STOP_SEQUENCES = []` in a future release if needed.

---

## Implementation Locations

Every decision in this document maps to a concrete file location in one of two repos.

| Change | Repo | File | Type |
|---|---|---|---|
| Action-gated screenshots — replace fixed-FPS timer loop with `screenshot_trigger.wait(timeout=1.0)` | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `read_screen_events()` (line 793) |
| Action trigger — set `screenshot_trigger` after every action event is queued | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `trigger_action_event()` (line 646) |
| Thread `screenshot_trigger` through mouse/keyboard callbacks | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `on_move()`, `on_click()`, `on_scroll()`, `handle_key()` via `functools.partial` |
| Thread `screenshot_trigger` through reader thread launchers | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `read_keyboard_events()` and `read_mouse_events()` |
| Instantiate `screenshot_trigger` and wire all threads | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `record()` (line 1388) — create `threading.Event()`, pass to screen reader and action callbacks |
| Bounded queues — `maxsize=50` on `event_q` and all write queues | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `record()` (line 1444) — change `queue.Queue()` and `SynchronizedQueue()` constructor calls |
| Explicit PIL Image close after PNG write | openadapt-capture fork | `openadapt_capture/recorder.py` | Modify `write_screen_event()` (line 382) — add `image.close()` after `image.save()` |
| `SynchronizedQueue` maxsize support | openadapt-capture fork | `openadapt_capture/extensions/synchronized_queue.py` | Modify `SynchronizedQueue.__init__()` (line 74) — add `maxsize=0` parameter, forward to `super().__init__()` |
| Config overrides (all P1 flags from Section 4) | luminque-ops | `luminque/capture/__init__.py` | Modify existing `run()` stub — construct `RecordingConfig` with P1 values, call `config_override()` |
| Capture directory setup (`%APPDATA%\Luminque\recordings\<unix_timestamp>`) | luminque-ops | `luminque/capture/__init__.py` | Modify existing `run()` stub — resolve path, call `os.makedirs()`, pass `capture_dir` to `record()` |
| `record()` call with task description | luminque-ops | `luminque/capture/__init__.py` | Modify existing `run()` stub — call `record(task_description="luminque-background", capture_dir=capture_dir)` inside `config_override` context |
| Process RSS watchdog + liveness check + daily midnight restart | luminque-ops | `luminque/watchdog/__init__.py` | Modify existing `run()` stub — implement psutil-based loop, restart if dead (5 s backoff) or RSS > 500 MB or 00:00–00:05 window |
| UIPI guard on window data collection | openadapt-capture fork | `openadapt_capture/window/` (platform-specific active window module) | Modify `get_active_window_data()` — wrap in `try/except Exception`, return empty dict / None on failure |

---

## 10. Out of Scope for Phase 1

The following are explicitly deferred to later phases:

- **Video recording** — `RECORD_VIDEO = False`. No MP4/H.264 output.
- **Audio recording** — `RECORD_AUDIO = False`.
- **Browser event capture** — `RECORD_BROWSER_EVENTS = False`. No WebSocket bridge.
- **UI Automation / element state** — `RECORD_READ_ACTIVE_ELEMENT_STATE = False`.
- **Analysis pipeline** — Reading, processing, or aggregating `recording.db` files. Phase 1 produces raw capture only.
- **Privacy scrubbing** — PII in screenshots or key events is not filtered. openadapt-privacy integration is a future phase.
- **Storage rotation / eviction** — No automatic deletion of old recordings. Disk management is manual or deferred.
- **Remote upload / sync** — Recordings stay local in `%APPDATA%\Luminque\recordings\`.
- **Multi-user sessions** — Each Windows user profile gets its own recording directory. No aggregation at capture time.
- **Session segmentation** — One recording per process lifetime. No automatic splitting by application or business process context.
- **Replay** — No pynput-based replay of recorded sessions.
- **UI / tray icon** — Process runs headless. No user-facing controls in P1.

---

## Appendix: Summary of Code Changes Required

| File | Change | Purpose |
|---|---|---|
| `recorder.py` | Add `screenshot_trigger: threading.Event` parameter to `read_screen_events()` | Action-gated capture |
| `recorder.py` | Replace timer-loop in `read_screen_events()` with `screenshot_trigger.wait(timeout=1.0)` pattern | Eliminates idle frames |
| `recorder.py` | Add `screenshot_trigger` parameter to `trigger_action_event()`, call `.set()` after `event_q.put()` | Trigger on every action |
| `recorder.py` | Thread `screenshot_trigger` through `on_move`, `on_click`, `on_scroll`, `handle_key` via `partial()` | Connect mouse/keyboard to screen thread |
| `recorder.py` | Thread `screenshot_trigger` through `read_keyboard_events` and `read_mouse_events` | Same as above |
| `recorder.py` | Create `screenshot_trigger = threading.Event()` in `record()` and pass to relevant threads | Instantiation |
| `recorder.py` | Change `event_q = queue.Queue()` to `queue.Queue(maxsize=50)` | Back-pressure |
| `recorder.py` | Add `image.close()` after `image.save()` in `write_screen_event()` | PIL buffer release |
| `recorder.py` | Remove assertion `config.RECORD_VIDEO or config.RECORD_IMAGES` or ensure `RECORD_IMAGES=True` | Config compatibility |
| `extensions/synchronized_queue.py` | Add `maxsize=0` parameter to `SynchronizedQueue.__init__()`, forward to `super().__init__()` | Bounded write queues |
| `luminque_capture/__main__.py` (new) | Set `RecordingConfig` with P1 values, resolve capture dir, call `record()` | Luminque entry point |
