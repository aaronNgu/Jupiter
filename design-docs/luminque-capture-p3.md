# luminque-capture-p3 — Native screenshot capture (openadapt-capture replacement)

Status: DRAFT
Supersedes: the openadapt-capture integration described in `luminque-capture-p1.md`
(action-gated screenshots) for the `--capture` mode. The memory-leak mitigations
in `luminque-capture-p2.md` become obsolete once this ships.

## 1. Motivation

Windows VM testing of the current `--capture` mode (openadapt-capture wrapper)
showed two problems:

1. **CPU ≥ 50%.** Profiling the fork identified the causes, in order:
   - `read_window_events` polls every 0.1 s; with `RECORD_WINDOW_DATA=true`
     each poll does a fresh `pywinauto Application(backend="uia").connect()`
     plus `get_element_properties()` — a **recursive walk of the entire UIA
     accessibility tree** of the active window via cross-process COM calls.
     For a browser/Office window one walk takes seconds at 100% of a core;
     the loop runs them back-to-back indefinitely.
   - Every mouse move (~125 Hz) is queued, pickled across a multiprocessing
     `SynchronizedQueue`, and inserted as a SQLAlchemy row — then *discarded
     by the sender* (`query_batch` filters `name != "move"`).
   - Full-resolution PIL PNG encode + full-frame `np.array(img).mean()` per
     screenshot.
2. **Screenshots are not meaningful.** The action-gated loop sleeps 300 ms
   *after* each input event before grabbing, so a click's screenshot shows the
   post-click world (menu closed, page mid-load) — never the screen the user
   acted on. Bursts of actions coalesce into one arbitrary mid-transition frame.

Product scope has narrowed to **screenshots only** for now (no action/window
event delivery), which removes the need for openadapt-capture's 5-thread +
4-process event pipeline entirely. A purpose-built capturer is ~300 lines,
single-process, and eliminates the pywinauto/UIA dependency.

## 2. Goals / non-goals

Goals:
- `--capture` produces meaningful screenshots at < 5% CPU sustained (target
  1–2%) on a 2-vCPU Windows VM.
- Implemented as a new package `luminque/captureV2/`, leaving the existing
  `luminque/capture/` wrapper in place during the transition. The only
  `luminque/main.py` change is re-pointing the `--capture` route; the sender's
  wire contract with the ingestion API (`/api/v1/sessions`, `.../events`,
  `.../media`) is unchanged.
- The capture DB remains schema-compatible with the sender's queries
  (`luminque/sender/db.py`), retention cap, and cleanup.
- Event capture can be added later without architectural change (§9).
- Remove `openadapt-capture` (and transitively pywinauto, sounddevice, loguru,
  av, whisper, etc.) from the PyInstaller bundle.

Non-goals (unchanged from Phase 1 scope):
- Action/window event *delivery* to the server (data model stays ready for it).
- Multi-monitor (primary monitor only), video, audio, browser events.
- PII scrubbing (Phase 2, sender-side).

## 3. Architecture

One process, started by `luminque.exe --capture` exactly as today. No
`multiprocessing`, no inter-process queues.

```
luminque.exe --capture
 ├─ main thread          capture loop (grab → dedupe → encode → SQLite write)
 ├─ pynput mouse thread  callback: update last_activity timestamp (no storage)
 ├─ pynput kbd thread    callback: update last_activity timestamp (no storage)
 └─ (that's it)
```

### 3.1 Capture policy: activity-gated sampling + change dedupe

Replaces both upstream FPS capture and the p1 action-gated trigger.

```
loop:
    if now - last_activity > IDLE_THRESHOLD_SECONDS:
        sleep(IDLE_POLL_SECONDS); continue          # user idle → capture nothing
    frame = grab_primary_monitor()                   # mss, raw BGRA
    thumb = downscale(frame, THUMB_WIDTH)            # ~64px, for hash + brightness
    if mean_brightness(thumb) < BLANK_THRESHOLD:     # lock screen / display sleep
        sleep(ACTIVE_INTERVAL_SECONDS); continue
    h = dhash(thumb)
    if hamming(h, last_kept_hash) <= DHASH_DISTANCE_THRESHOLD:
        sleep(ACTIVE_INTERVAL_SECONDS); continue     # screen unchanged → skip
    img = downscale(frame, MAX_IMAGE_WIDTH)          # ~1280px
    png = encode_png(img, compress_level=PNG_COMPRESS_LEVEL)
    insert_screenshot(png); maybe_insert_window_event()
    last_kept_hash = h
    sleep(ACTIVE_INTERVAL_SECONDS)
```

Why this fixes "not meaningful": at ~1 fps while active, with frames kept only
when the screen *changed*, the kept set is exactly the sequence of distinct
screen states the user worked through — including both the pre-action state
(screen was stable before the click) and the post-action state (screen changed
after it). No event-timing heuristics needed. Idle periods produce nothing.

Worst-case storage rate: 1 frame/sec × ~150 KB (1280-wide PNG) ≈ 540 MB/hour
*if every frame differs*, but dedupe makes the realistic rate far lower
(typing/reading produce near-zero kept frames). The existing 24 h retention cap
and post-upload `png_data` nullification bound local disk as before.

### 3.2 Activity detection (pynput, storage-free)

`pynput.mouse.Listener` and `pynput.keyboard.Listener` with callbacks that do
exactly one thing: `self._last_activity = time.monotonic()`. No event objects,
no queues, no DB writes. This is the same low-level hook every screen recorder
uses; cost is negligible. The listeners also serve as the future event-capture
insertion point (§9).

Fallback: if pynput fails to start (hook injection blocked by security
software), log a warning and degrade to continuous sampling at
`ACTIVE_INTERVAL_SECONDS` — dedupe still suppresses unchanged frames.

### 3.3 Window title stamping (cheap, local-only)

Per kept frame, read the foreground window via **ctypes user32 calls only**
(`GetForegroundWindow`, `GetWindowTextW`, `GetWindowRect`,
`GetWindowThreadProcessId`) — microseconds, no COM, no pywinauto. Insert a
`window_event` row when (title, hwnd) changes. The sender currently only ships
window events referenced by action events, so these stay local — they exist to
make Phase-2 SOP analysis possible without a client redeploy. Wrapped in
`try/except` (UIPI: fails when an elevated window is focused).

### 3.4 Screenshot backend

`mss` (already the upstream backend). CAPTUREBLT must be disabled (cursor
flicker, slower BitBlt); in mss ≥ 10 the flag moved to `mss.windows.gdi`, so
patch whichever location exists. Primary monitor only (`sct.monitors[1]`).
One `mss.mss()` instance per process, created lazily on the capture thread,
and recreated on: any grab exception, a live `GetSystemMetrics` geometry
mismatch (checked at most once/minute — mss caches monitor rects, and a
resolution change like an RDP reconnect makes BitBlt return cropped/black
frames *without* raising), or a long streak of consecutive blank frames.

If VM testing shows GDI BitBlt is still too slow, the contained upgrade is
`dxcam` (DXGI desktop duplication) behind the same `grab()` seam — not in
scope unless measurements demand it.

### 3.5 Image processing

- Downscale with PIL `Image.resize(..., Image.BILINEAR)` to
  `MAX_IMAGE_WIDTH` (1280) preserving aspect ratio. This alone cuts encode
  time and payload size ~4× vs. full 1080p/4K frames.
- Encode PNG `compress_level=1`. PNG (not JPEG/WebP) keeps the
  `image/png` media contract with the ingestion API unchanged.
- dhash: 8×8 difference hash computed on the 64-px grayscale thumbnail with
  pure PIL/stdlib (no imagehash dependency needed; ~15 lines).
- Brightness check on the thumbnail, not the full frame (replaces the
  full-frame `np.array().mean()`); numpy dependency dropped.

## 4. Data layer — schema-compatible SQLite, stdlib `sqlite3`

Same DB location and append semantics as today:
`%APPDATA%\Luminque\recordings\recording.db`, one new `recording` row per
process start, IDs monotonically increasing across restarts.

The new module writes via **stdlib `sqlite3`** (no SQLAlchemy in the capture
path) using DDL that matches the openadapt column set exactly for the four
tables the sender touches. The sender continues to read through its existing
code path unmodified in step 1 (it still imports openadapt models), and through
its own models in step 4 (§7).

```sql
PRAGMA journal_mode=WAL;            -- matches sender expectation (db.py)
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS recording (
    id INTEGER PRIMARY KEY, timestamp NUMERIC(10,2),
    monitor_width INTEGER, monitor_height INTEGER,
    double_click_interval_seconds NUMERIC, double_click_distance_pixels NUMERIC,
    platform VARCHAR, task_description VARCHAR,
    video_start_time NUMERIC(10,2), config JSON,
    original_recording_id INTEGER REFERENCES recording(id)
);
CREATE TABLE IF NOT EXISTS screenshot (
    id INTEGER PRIMARY KEY, recording_timestamp NUMERIC(10,2),
    recording_id INTEGER REFERENCES recording(id), timestamp NUMERIC(10,2),
    png_data BLOB, png_diff_data BLOB, png_diff_mask_data BLOB
);
CREATE TABLE IF NOT EXISTS window_event (
    id INTEGER PRIMARY KEY, recording_timestamp NUMERIC(10,2),
    recording_id INTEGER REFERENCES recording(id), timestamp NUMERIC(10,2),
    state JSON, title VARCHAR, left INTEGER, top INTEGER,
    width INTEGER, height INTEGER, window_id VARCHAR
);
CREATE TABLE IF NOT EXISTS action_event ( ... full openadapt column set ... );
```

`action_event` is created (empty) because `sender/db.py::query_batch`
unconditionally queries it. Column definitions are copied verbatim from
`openadapt_capture/db/models.py` so an existing `recording.db` created by the
old capturer is reused as-is — `CREATE TABLE IF NOT EXISTS` means upgrade
requires no migration, and unsent rows from the old capturer are still drained
by the sender.

Timestamps remain **unix floats** (the sender's `_ts()` converts to ISO 8601).
Writes are single-row INSERTs at ≤ 1 Hz — no batching machinery needed; commit
per insert.

## 5. Module structure

New package at `luminque/captureV2/` (the legacy openadapt wrapper stays at
`luminque/capture/` untouched until step 6 removes it):

```
luminque/captureV2/
    __init__.py      run() — entry point: logging, single-instance guard,
                     construct + start CaptureLoop (same signature main.py expects)
    constants.py     all tunables (table below)
    schema.py        DDL above; open_db(path) -> sqlite3.Connection;
                     insert_recording / insert_screenshot / insert_window_event
    activity.py      ActivityMonitor — pynput listeners, .last_activity,
                     .start()/.stop(), degraded mode flag
    grabber.py       Grabber — mss wrapper; .grab() -> raw frame;
                     thumbnail(), downscale(), encode_png(), dhash(), hamming()
    foreground.py    get_foreground_window() -> {title, left, top, width,
                     height, hwnd} | None   (ctypes user32, try/except)
    loop.py          CaptureLoop — the §3.1 loop; injectable clock/grabber/
                     activity/db for tests; .run_forever(), .stop()
```

`run()` logs to `%APPDATA%\Luminque\logs\capture.log` via
`TimedRotatingFileHandler` (midnight rotation, `backupCount=14`, UTF-8) —
the legacy date-in-filename scheme only rolled because the watchdog restarted
capture at midnight, and nothing pruned the directory. The stdout handler is
added only when `sys.stdout` is not None (windowed PyInstaller builds have
none). The "Capture starting (pid=…)" log phrasing is kept for ops grep
compatibility. The macOS fork-start-method workaround and the `RECORD_*` env
vars are deleted with the openadapt wrapper.

### Constants (initial values; all in `constants.py`)

| Constant | Value | Rationale |
|---|---|---|
| `ACTIVE_INTERVAL_SECONDS` | 0.25 | ~4 fps while user is active |
| `IDLE_THRESHOLD_SECONDS` | 5.0 | no input for 5 s → stop sampling |
| `IDLE_POLL_SECONDS` | 0.5 | wake latency out of idle |
| `MAX_IMAGE_WIDTH` | 1280 | encode cost / payload vs. legibility |
| `THUMB_WIDTH` | 64 | hash + brightness input |
| `DHASH_DISTANCE_THRESHOLD` | 2 | bits differing to count as "changed" (lower = keeps subtler changes) |
| `BLANK_BRIGHTNESS_THRESHOLD` | 8.0 | matches upstream lock-screen discard |
| `PNG_COMPRESS_LEVEL` | 1 | speed over ratio; gzip-on-wire not used for media |
| `MAINTENANCE_INTERVAL_SECONDS` | 300 | disk guard cadence |
| `LOCAL_MAX_BLOB_BYTES` | 2 GiB | hard disk bound (size) |
| `LOCAL_MAX_BLOB_AGE_SECONDS` | 8 h | age bound; backstops sender `RETENTION_SECONDS` (6 h) |

### Capture-side disk guard

The sender nulls `png_data` two ways (on HTTP 201, and a retention cap), but
both live in the sender — and the sender `return 1`s at the credential check
(`sender.py`) before reaching retention. So a machine that is unenrolled, has
broken keyring creds, has no `--send` scheduled task, or whose exe was
quarantined by Defender **never nulls blobs**: at 4 fps that's ~0.5–4 GB/hour
of active use until `C:` (where `%APPDATA%` lives) fills. The watchdog only
watches RSS/liveness, so it is blind to this.

The guard runs on the always-on capture thread, every
`MAINTENANCE_INTERVAL_SECONDS`, inside `run_forever`'s try (so its errors hit
the same backoff and never kill the loop):

1. `null_blobs_older_than(now - LOCAL_MAX_BLOB_AGE_SECONDS)` — age bound.
2. `null_oldest_blobs_until_under(LOCAL_MAX_BLOB_BYTES)` — size bound, evicts
   oldest-first (ascending id, matching the sender's send order).
3. `incremental_vacuum()` — return freed pages to the OS.

Both null operations are **chunked + committed per batch** (same write-lock
reasoning as the sender retention cap) so they never hold the lock past
capture's `busy_timeout`.

Design notes:
- **Eviction (nulling) is the hard bound; vacuum is best-effort.**
  `auto_vacuum=INCREMENTAL` only takes effect on a *fresh* DB, so on existing
  field DBs the file won't shrink — but nulled pages are reused by new
  inserts, so the file *plateaus* rather than growing unbounded. The bound
  holds either way; vacuum only returns slack to the OS on fresh installs.
- **Bounds blob bytes, not file size** — verify via
  `SELECT SUM(LENGTH(png_data)) ... WHERE png_data IS NOT NULL`, not file
  size (SQLite doesn't shrink the file without VACUUM).
- **Only nulls `png_data`** — the sender's `png_data != None` filter skips
  nulled rows, so the guard never fights the upload cursor.
- **Can evict unsent blobs** when the sender is failing/behind. Intended
  tradeoff: bounding disk beats at-least-once delivery when the disk is what's
  at risk. Ordering `LOCAL_MAX_BLOB_AGE_SECONDS (8h) >= sender
  RETENTION_SECONDS (6h) >= send latency` keeps the guard from touching unsent
  data in normal operation: the sender nulls at 6h and the capture guard only
  backstops at 8h, so in steady state it evicts only already-sent (already-
  nulled) rows. The size cap remains the worst-case protection when the sender
  is not running at all.
- Caps (2 GiB / 8 h capture, 6 h sender) are placeholders — tune to the pilot
  disk budget.

## 6. Lifecycle integration (unchanged components)

- **main.py** — one-line change: the `--capture` branch imports
  `luminque.captureV2.run` instead of `luminque.capture.run`. Everything else
  (mode strings, Task Scheduler commands, watchdog cmdline matching) is
  unchanged. `multiprocessing.freeze_support()` stays (harmless; still correct
  if anything ever spawns). Rollback during the transition is reverting this
  one import.
- **watchdog** — no change. Finds capture by `"--capture" in cmdline`; restart
  and daily-midnight-restart logic untouched. The 500 MB RSS threshold becomes
  generous headroom (expected RSS: tens of MB).
- **onboarding / stop / Task Scheduler** — no change; same exe, same flags.
- **Single-instance guard** — keep behavior equivalent to today (watchdog may
  fire while capture runs). Named Windows mutex `Local\LuminqueCapture` via
  ctypes (`WinDLL(..., use_last_error=True)` + `ctypes.get_last_error()` —
  the naive `windll.kernel32.GetLastError()` pattern can be clobbered by
  ctypes' own intervening calls); exit 0 if held. `Local\` not `Global\`:
  non-elevated processes lack SeCreateGlobalPrivilege. Known limitation: on
  multi-session hosts (RDS, fast user switching) each session runs its own
  capture against the same per-user DB — WAL-safe, just redundant.
- **Shutdown** — Task Scheduler / `--stop` terminate the process; WAL mode
  makes mid-write kills safe (at most the in-flight frame is lost). No
  graceful-shutdown protocol needed, matching today.
- **Error containment** — a failing `tick()` (sender holding the SQLite write
  lock past `busy_timeout`, disk full) is logged and retried after a 30 s
  backoff, never allowed to kill the process: a crash here would put the
  watchdog into a 5-minute restart loop. Complementarily, the sender's
  retention cap nullifies blobs in bounded chunks (500 rows/transaction) so
  an offline-for-a-day backlog can't hold the write lock for seconds.

## 7. Sender changes

**Step 1 (ship the capturer): none.** `query_batch` already uses an
independent `last_sent_screenshot_id` cursor and tolerates zero action events;
`sender.py` already derives `started_at` from the first screenshot when there
are no action events; media upload, `cleanup_sent_screenshots`, and the
retention cap operate purely on the `screenshot` table.

**Step 4 (drop the openadapt dependency):** replace the two openadapt imports
in `sender/db.py` (`get_session_for_path`, models) with a luminque-internal
data layer. Options: (a) plain `sqlite3` row dicts — requires touching
`payload.py` accessor style (`getattr` → key access), or (b) a ~60-line
`luminque/sender/models.py` with SQLAlchemy models mirroring the three tables,
keeping `payload.py`/`sender.py` untouched. **Choose (b)** — smaller diff,
`payload.py`'s `getattr`-based serializers keep working, SQLAlchemy stays a
dependency anyway only if (b); if dropping SQLAlchemy from the bundle matters
for size, revisit with (a). After this step, remove `openadapt-capture` from
`pyproject.toml` and the PyInstaller spec's hidden imports; add direct deps
`mss`, `pynput`, `pillow`.

## 8. Testing

Unit (pure-Python, CI-runnable on macOS/Linux):
- `dhash`/`hamming`: identical, slightly-noisy, and different images.
- `CaptureLoop` with injected fake clock/grabber/activity/db:
  - idle → zero captures; activity resumes sampling within `IDLE_POLL_SECONDS`.
  - unchanged screen → exactly one kept frame.
  - changed screen → new row with monotonically increasing id.
  - blank frame discarded.
  - pynput-unavailable degraded mode still captures.
- `schema.py`: fresh DB has all four tables; re-open of an existing
  openadapt-created DB (fixture) inserts without error and continues IDs.

Integration (the contract test — most important):
- New capturer writes a DB → run the *unmodified* sender stack against it
  (`open_capture_db` → `query_batch` → `build_events_request` +
  `screenshot_filename` → mocked transport): screenshots serialize, cursor
  advances, `cleanup_sent_screenshots` nullifies blobs, retention cap runs.
  Extend `tests/test_sender.py` fixtures to generate the DB via `schema.py`
  instead of openadapt models (proving compat in both directions).

Manual (Windows VM, acceptance criteria):
- Sustained CPU < 5% during active use (Task Manager, 10-min mixed browsing +
  typing session); ~0% idle.
- Kept frames visually correspond to distinct screen states (the
  "meaningful" check), including pre/post states around clicks.
- Lock screen, display sleep, elevated-window focus (UIPI), logout/login via
  Task Scheduler, watchdog kill/restart, `--stop`.
- End-to-end: capture → `--send` against a local ingestion-api → media +
  screenshot events arrive, blobs nullified locally.

## 9. Future: event capture (designed-for, not built)

When action events come back into scope:
1. `activity.py` callbacks additionally append `(name, ts, x, y, key…)` tuples
   to an in-memory list; `loop.py` flushes it to `action_event` rows on its
   1 s tick (`screenshot_id` = last kept frame → the pre-action screen;
   `window_event_id` = current window row). Mouse moves never stored.
2. Remove the `name != "move"` reliance and bump `MAX_BATCH_EVENTS` review.
3. Click/typing merging (openadapt `processing.py` equivalent) runs
   **server-side** in sop-discovery on raw events — never on the client.
4. If element-level context is ever needed: a single UIA `ElementFromPoint`
   per click (not a tree walk), budgeted and `try/except`-wrapped.

## 10. Implementation steps

| # | Step | Touches | Est. |
|---|---|---|---|
| 1 | `captureV2/schema.py` + unit tests + sender contract test (DB written by new DDL, read by current sender) | new code, `tests/` | 0.5 d |
| 2 | `captureV2/grabber.py`, `foreground.py`, `activity.py` + unit tests | new code | 0.5 d |
| 3 | `captureV2/loop.py`, `constants.py`, `__init__.py::run()`; re-point `--capture` in `main.py` to `luminque.captureV2.run` | `luminque/captureV2/`, `luminque/main.py` | 0.5 d |
| 4 | Sender de-coupling: `sender/models.py`, rewrite `sender/db.py` internals; drop `openadapt-capture` from `pyproject.toml`; add `mss`/`pynput`/`pillow`; update PyInstaller spec | `luminque/sender/db.py`, packaging | 0.5–1 d |
| 5 | Windows VM validation per §8 manual checklist; tune constants | — | 0.5–1 d |
| 6 | Cleanup: delete legacy `luminque/capture/`; docs — update CLAUDE.md (capture section, related-repos table), mark p1 §3 superseded | `luminque/capture/`, docs | 0.25 d |

Steps 1–3 are shippable without step 4 (exe still bundles openadapt but never
imports it at capture time; sender still reads via openadapt models). The
legacy `luminque/capture/` package is deleted only in step 6, after VM
validation passes — until then `main.py` can be reverted to it in one line.
Total: ~3–4 days including VM time.

## 11. Risks

- **pynput hooks blocked** by endpoint security → degraded continuous-sampling
  mode (§3.2) keeps capture functional; log loudly for ops visibility.
- **mss too slow in VMs without GPU** → measured in step 5; `dxcam` seam ready.
- **Dedupe too aggressive** (e.g., misses small-but-important changes like a
  status field) → `DHASH_DISTANCE_THRESHOLD` is a constant; worst case set to
  0 (keep every changed-at-all frame). Tune against real SOP footage in VM.
- **Storage spikes** from visually-busy apps (video playing in browser) →
  retention cap already bounds disk; if needed add a per-hour kept-frame cap
  later.
- **Old capturer's unsent data** — drained automatically (same DB, same
  cursors); no migration.
