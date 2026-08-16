# luminque-capture Phase 2 — Memory Leak & CPU Fix Design Document

**Status:** Draft  
**Date:** 2026-05-07  
**Author:** Aaron (aaronnfw@gmail.com)

---

## 1. Overview

Phase 1 shipped a working capture pipeline. Live profiling of session `1778124128` revealed three root causes driving sustained memory growth and excessive CPU usage in the writer subprocesses. This document specifies the fixes.

**Observed symptoms (session 1778124128, ~45 min runtime):**

| Process | Role | RSS | CPU | Trend |
|---|---|---|---|---|
| 54506 (main) | `record()` + event routing | 421 MB | 8% | Growing (+) |
| 54549 (writer) | `screen_event_writer` | 107 MB | 80% | Growing (+) |
| 54550–54552 (writers) | action / window / perf writers | 33–35 MB | 55–58% | Stable |

The 500 MB watchdog threshold was within ~80 MB of being hit on the main process within a single afternoon session.

---

## 2. Root Cause 1 — Busy-Wait Loop in `write_events()`

### Location

`openadapt_capture/recorder.py` — `write_events()`, lines 511–514

### Problem

```python
# current
try:
    event = write_q.get_nowait()
except queue.Empty:
    continue   # ← immediately retries with no sleep
```

When the write queue is empty between action events, each writer process spins in a tight loop calling `get_nowait()` as fast as the CPU allows. `get_nowait()` is a syscall. With action-gated capture there are long idle periods between events.

**Measured impact:** `SYSBSD` (system call count) for each writer process exceeded **500–600 million** calls within ~45 minutes — all wasted CPU on empty queue checks. CPU sat at 55–80% per writer process even during idle periods.

### Fix

Replace `get_nowait()` with `get(timeout=0.1)`. The OS puts the process to sleep until an item arrives or the timeout expires, then wakes it:

```python
# fixed
try:
    event = write_q.get(timeout=0.1)
except queue.Empty:
    continue   # timeout expired — check terminate flag and sleep again
```

**Properties preserved:**
- Events are processed immediately on arrival (no 100ms added latency — the `get()` wakes as soon as an item is put)
- `terminate_processing` flag is still checked every ~100ms
- No events are dropped

**Expected outcome:** Writer process CPU drops from 55–80% to near 0% during idle periods.

### Implementation

| File | Change |
|---|---|
| `openadapt_capture/recorder.py` | In `write_events()`: change `write_q.get_nowait()` → `write_q.get(timeout=0.1)` |

---

## 3. Root Cause 2 — SQLAlchemy Identity Map Never Cleared

### Location

`openadapt_capture/recorder.py` — `write_events()` loop, `write_screen_event()`

### Problem

The SQLAlchemy session is created once at process startup and reused forever:

```python
session = get_session_for_path(db_path)   # once, at startup

while ...:
    event = write_q.get(...)
    write_fn(session, recording, event, perf_q)
    # ← no session.commit(), no session.expunge_all()
```

`write_fn` (e.g., `write_screen_event`) calls `crud.insert_screenshot()` which does `session.add(screenshot_obj)` and possibly `session.flush()`. The INSERT reaches SQLite on disk ✓. But SQLAlchemy's **identity map** — an in-memory cache keyed by primary key — retains a Python reference to every ORM object ever written, so the garbage collector cannot free them.

For `Screenshot` rows each carrying a 2–3 MB `png_data` blob, this means:

```
After N screenshots:  ~N × 2.5 MB held in session identity map
After 40 screenshots: ~100 MB  (matches 54549's observed 107 MB)
```

The data is in two places simultaneously:
1. **SQLite file on disk** ✓ (correct and desired)
2. **Python heap in the writer process** ✗ (the leak)

### Fix

Call `session.expunge_all()` after each write (or after each commit). This removes all tracked objects from the identity map, allowing Python to garbage-collect the `png_data` byte arrays:

```python
while ...:
    event = write_q.get(timeout=0.1)
    write_fn(session, recording, event, perf_q)
    session.expunge_all()   # release identity map references after each write
```

`session.expunge_all()` does not undo the write — the data is already in SQLite. It simply tells SQLAlchemy "I am done with these Python objects; you no longer need to track them."

**Trade-off:** Repeated queries for the same row will hit SQLite instead of the in-memory cache. The writer processes never re-query written rows, so there is no functional downside.

**Expected outcome:** Writer process RSS stays bounded regardless of session duration. `54549`'s 107 MB+ growth eliminated.

### Implementation

| File | Change |
|---|---|
| `openadapt_capture/recorder.py` | In `write_events()`: add `session.expunge_all()` after each `write_fn(...)` call |

---

## 4. Root Cause 3 — Raw PIL Images Passed Through Multiprocessing Queue

### Location

`openadapt_capture/recorder.py` — `read_screen_events()` → `event_q` → `process_events()` → `screen_write_q` → `write_screen_event()`

### Problem

The screenshot pipeline passes a raw PIL `Image` object across two queue boundaries:

```
read_screen_events          process_events          screen_write_q          write_screen_event
      │                          │                        │                        │
 PIL Image    ──event_q──▶  PIL Image   ──────────▶  PIL Image   ──────────▶  PNG conversion
 (~16 MB raw)               (~16 MB raw)            (~16 MB × up to 50)        happens here
```

PIL stores images as **raw uncompressed pixels** internally. For a Retina display (2560 × 1600 RGBA): `2560 × 1600 × 4 = ~16.4 MB` per screenshot. When passed through a `multiprocessing.Queue`, the PIL Image is **pickled** (serialised) — and the pickle of a PIL Image includes its raw pixel buffer, so the pickle is also ~16 MB.

With `screen_write_q` at `maxsize=50`, up to `50 × 16 MB = ~800 MB` of raw pixel data can sit in the queue pipe at peak backpressure. Even at typical load (a few items in the queue), each screenshot in transit occupies 3× its PNG size:

| Copy | Size | Location |
|---|---|---|
| Original PIL Image (pre-put) | ~16 MB | Main process heap |
| Pickled bytes in OS pipe | ~16 MB | Kernel buffer |
| Unpickled PIL Image (post-get) | ~16 MB | Writer process heap |

The writer process then compresses to PNG (~2–3 MB) as its first action — the 16 MB uncompressed copy was never needed.

### Fix

Convert the PIL Image to PNG bytes **in the screen reader thread**, immediately after taking the screenshot and before enqueueing. The queues then carry 2–3 MB compressed bytes instead of 16 MB raw pixels:

```
read_screen_events          process_events          screen_write_q          write_screen_event
      │                          │                        │                        │
 PIL → PNG bytes ──event_q──▶  PNG bytes ──────────▶  PNG bytes ──────────▶  write as-is
 (~2–3 MB)                     (~2–3 MB)              (~2–3 MB × up to 50)    (no conversion)
```

**Implementation in `read_screen_events()`:**

```python
screenshot = utils.take_screenshot()
if screenshot is None:
    continue

# discard blank frames (display sleep / lock transition)
if np.array(screenshot).mean() < 8.0:
    screenshot.close()
    continue

# compress to PNG immediately — pass bytes through queues, not raw pixels
with io.BytesIO() as buf:
    screenshot.save(buf, format="PNG")
    png_bytes = buf.getvalue()
screenshot.close()   # release native pixel buffer

event_q.put(Event(utils.get_timestamp(), "screen", png_bytes))
```

**`write_screen_event()` simplifies to:**

```python
def write_screen_event(db, recording, event, perf_q):
    assert event.type == "screen", event
    png_data = event.data   # already bytes — no conversion needed
    if config.RECORD_IMAGES:
        crud.insert_screenshot(db, recording, event.timestamp, {"png_data": png_data})
    perf_q.put((event.type, event.timestamp, utils.get_timestamp()))
```

**Expected outcome:** Peak queue memory drops from ~800 MB to ~150 MB (50 × 3 MB). Main process RSS growth eliminated.

**Scope note:** This change modifies the type of `event.data` for `"screen"` events from `PIL.Image` to `bytes`. The only consumer of `event.data` for screen events is `write_screen_event()` and optionally `write_video_event()` (disabled in Luminque via `RECORD_VIDEO=False`). No other code path is affected in the Luminque configuration.

### Implementation

| File | Change |
|---|---|
| `openadapt_capture/recorder.py` | In `read_screen_events()`: convert PIL → PNG bytes after capture, close PIL Image, put bytes in `event_q` |
| `openadapt_capture/recorder.py` | In `write_screen_event()`: remove PNG conversion (data is already bytes); remove `image.close()` (already closed upstream) |

---

## 5. Previously Shipped Fixes (Phase 1 post-launch patches)

These fixes were applied reactively during initial testing and are already in the fork. Documented here for completeness.

### 5.1 Blank screenshot detection

**Problem:** The click or keypress to wake a sleeping display fires a pynput event before the screen has finished lighting up. The 300ms post-trigger delay is not enough to wait for display wake from deep sleep. Resulting screenshots are entirely black (~5–6 KB PNG).

**Fix (already applied):** In `read_screen_events()`, after taking the screenshot, discard it if mean pixel brightness is below threshold:

```python
mean_brightness = np.array(screenshot).mean()
if mean_brightness < 8.0:
    logger.debug(f"Discarding blank screenshot (mean={mean_brightness:.1f})")
    screenshot.close()
    continue
```

**Note:** With the Root Cause 3 fix applied, this check moves to before the PNG conversion call, which is the correct position.

### 5.2 Post-trigger delay increase (150ms → 300ms)

**Problem:** At 150ms delay, some UI transitions (e.g., modal dialogs, dropdown menus appearing) had not finished rendering when the screenshot was taken.

**Fix (already applied):** Increased `time.sleep(0.15)` → `time.sleep(0.30)` in `read_screen_events()`.

### 5.3 Mouse move / scroll do not trigger screenshots

**Problem:** Mouse move and scroll events were triggering `screenshot_trigger.set()`, flooding the trigger with events that don't meaningfully change screen content. This caused the bounded `event_q` to fill with redundant screenshots, creating back-pressure that slowed the entire pipeline.

**Fix (already applied):** `on_move()` and `on_scroll()` pass `screenshot_trigger=None` to `trigger_action_event()`. Only `on_click()` and `handle_key()` set the trigger.

---

## 6. Expected Memory Profile After All Fixes

| Process | Before | After |
|---|---|---|
| Main (54506) | 421 MB, growing | < 50 MB, stable |
| Screen writer (54549) | 107 MB, growing | < 20 MB, stable |
| Other writers (54550–52) | 33–35 MB, stable | < 10 MB, stable |
| **Total** | **~630 MB** | **< 100 MB** |

CPU for writer processes: 55–80% → near 0% during idle periods.

---

## 7. Implementation Table

All changes are in the `openadapt-capture` fork only. No changes to `luminque-ops`.

| # | Root Cause | File | Function | Change |
|---|---|---|---|---|
| 1 | Busy-wait | `recorder.py` | `write_events()` | `get_nowait()` → `get(timeout=0.1)` |
| 2 | Identity map | `recorder.py` | `write_events()` | Add `session.expunge_all()` after each `write_fn()` call |
| 3 | Raw PIL in queue | `recorder.py` | `read_screen_events()` | Convert PIL → PNG bytes immediately after capture; close PIL Image; enqueue bytes |
| 3 | Raw PIL in queue | `recorder.py` | `write_screen_event()` | Remove PNG conversion (data arrives as bytes); remove `image.close()` |

---

## 8. How to Monitor Memory During Development

### 8.1 Find the capture processes

```bash
pgrep -f "luminque --capture"
```

Expect 5–6 PIDs per running session: one `uv run` launcher, one main Python process, and 3–4 writer subprocesses.

Identify the process tree (PPID column shows parent → child relationships):

```bash
ps -f -p $(pgrep -f "luminque --capture" | tr '\n' ',')
```

The main Python process is the one whose PPID is the `uv run` launcher, not another Python process.

### 8.2 Watch RSS for all processes

`ps` reports RSS in **KB**. Divide by 1024 for MB.

```bash
# watch all capture processes, refresh every 5 seconds
while true; do
  clear
  echo "$(date +%T)"
  ps -o pid,rss,vsz,%cpu,command -p $(pgrep -f "luminque --capture" | tr '\n' ',')
  sleep 5
done
```

Key columns:

| Column | Unit | What to watch |
|---|---|---|
| `rss` | KB | Physical RAM in use — divide by 1024 for MB |
| `vsz` | KB | Virtual memory — usually large, not meaningful for leak detection |
| `%cpu` | % | Should be near 0% for writer processes when idle |

### 8.3 Interactive top for all capture processes

```bash
top -pid $(pgrep -f "luminque --capture" | xargs -I{} echo "-pid {}" | tr '\n' ' ' | xargs)
```

Or manually with known PIDs:

```bash
top -pid 54506 -pid 54549 -pid 54550 -pid 54551 -pid 54552
```

In top's output, the **MEM column is RSS**. A `+` suffix means the value grew since the last refresh. A `-` suffix on the CMPRS column means macOS is compressing that process's memory under pressure — a sign the system is running low.

### 8.4 Log RSS to a file (leak detection over time)

Run this alongside the capture process to log RSS every 30 seconds:

```bash
while true; do
  ts=$(date +%T)
  for pid in $(pgrep -f "luminque --capture"); do
    rss=$(ps -o rss= -p $pid 2>/dev/null)
    [ -n "$rss" ] && echo "$ts PID=$pid RSS=$((rss/1024))MB"
  done
  sleep 30
done | tee /tmp/luminque_mem.log
```

A healthy process holds steady RSS. A leaking process shows RSS climbing monotonically line-by-line. Example of a leak:

```
20:15:00 PID=54506 RSS=142MB
20:15:30 PID=54506 RSS=158MB
20:16:00 PID=54506 RSS=174MB   ← growing ~16MB/30s = problem
```

### 8.5 Confirming a fix worked

Before a fix: run the logger above for 5–10 minutes and observe RSS trend.  
After a fix: restart capture, run the logger again for the same duration. RSS should plateau.

Baseline targets after Phase 2 fixes are applied:

| Process | RSS target |
|---|---|
| Main process | < 50 MB, stable |
| Screen writer | < 20 MB, stable |
| Other writers | < 10 MB each, stable |

---

## 9. Out of Scope for Phase 2

- **Storage rotation** — DB files grow unbounded. Deferred to P3.
- **Multi-monitor screenshot support** — `PIL.ImageGrab.grab()` captures primary monitor only. Deferred.
- **Lock screen detection** — Screenshots of the macOS/Windows lock screen are not blank (they have content) but are not useful. Programmatic lock detection is platform-specific. Deferred.
- **PII scrubbing** — Deferred to sender P2 via openadapt-privacy.
