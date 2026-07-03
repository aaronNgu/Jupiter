"""The capture loop: activity-gated sampling with change dedupe.

Per design-docs/luminque-capture-p3.md §3.1. One thread, no queues:
grab → thumbnail → blank check → dhash dedupe → downscale → PNG → SQLite.

Frames are kept only while the user is active AND the screen changed since
the last kept frame, so the stored sequence is the series of distinct screen
states the user worked through. Idle periods produce nothing.
"""

import logging
import threading
import time

from luminque.captureV2 import constants, schema
from luminque.captureV2.foreground import get_foreground_window
from luminque.captureV2.grabber import (
    brightness,
    dhash,
    downscale,
    encode_png,
    hamming,
    to_thumb,
)

logger = logging.getLogger(__name__)

# tick() dispositions
IDLE = "idle"
GRAB_FAILED = "grab_failed"
BLANK = "blank"
UNCHANGED = "unchanged"
CAPTURED = "captured"

_LOG_EVERY_N_CAPTURES = 25
_ERROR_BACKOFF_SECONDS = 30.0
# Consecutive blank frames before assuming the grabber went stale (a black
# stream can mean stale monitor geometry, not just lock screen — see Grabber).
_BLANK_STREAK_RESET = 30


class CaptureLoop:
    def __init__(
        self,
        conn,
        grabber,
        activity,
        foreground_fn=get_foreground_window,
        sleep=time.sleep,
        wall_clock=time.time,
        active_interval_seconds: float = constants.ACTIVE_INTERVAL_SECONDS,
        idle_threshold_seconds: float = constants.IDLE_THRESHOLD_SECONDS,
        idle_poll_seconds: float = constants.IDLE_POLL_SECONDS,
        max_image_width: int = constants.MAX_IMAGE_WIDTH,
        thumb_width: int = constants.THUMB_WIDTH,
        dhash_distance_threshold: int = constants.DHASH_DISTANCE_THRESHOLD,
        blank_brightness_threshold: float = constants.BLANK_BRIGHTNESS_THRESHOLD,
        png_compress_level: int = constants.PNG_COMPRESS_LEVEL,
        maintenance_interval_seconds: float = constants.MAINTENANCE_INTERVAL_SECONDS,
        local_max_blob_bytes: int = constants.LOCAL_MAX_BLOB_BYTES,
        local_max_blob_age_seconds: float = constants.LOCAL_MAX_BLOB_AGE_SECONDS,
    ) -> None:
        self._conn = conn
        self._grabber = grabber
        self._activity = activity
        self._foreground_fn = foreground_fn
        self._sleep = sleep
        self._wall_clock = wall_clock

        self._active_interval = active_interval_seconds
        self._idle_threshold = idle_threshold_seconds
        self._idle_poll = idle_poll_seconds
        self._max_image_width = max_image_width
        self._thumb_width = thumb_width
        self._dhash_threshold = dhash_distance_threshold
        self._blank_threshold = blank_brightness_threshold
        self._png_compress_level = png_compress_level
        self._maintenance_interval = maintenance_interval_seconds
        self._local_max_blob_bytes = local_max_blob_bytes
        self._local_max_blob_age = local_max_blob_age_seconds

        self._stop = threading.Event()
        self._next_maintenance = 0.0  # 0 → run once on first iteration (clears any backlog after a restart)
        self._recording_id: int | None = None
        self._recording_timestamp: float | None = None
        self._last_kept_hash: int | None = None
        self._last_window_key: tuple | None = None
        self._captured_count = 0
        self._blank_streak = 0

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Loop until stop(). A failing tick must never kill the process:
        transient SQLite errors (sender holding the write lock longer than
        busy_timeout, disk full) are logged and retried after a backoff —
        crashing here would put the watchdog into a 5-minute restart loop.
        """
        while not self._stop.is_set():
            try:
                # Inside the try so a guard failure hits the same backoff as a
                # tick failure and never kills the long-running process.
                self._maybe_run_maintenance()
                disposition = self.tick()
            except Exception:
                logger.exception("Capture iteration failed — backing off")
                self._sleep(_ERROR_BACKOFF_SECONDS)
                continue
            if disposition == IDLE:
                self._sleep(self._idle_poll)
            else:
                self._sleep(self._active_interval)

    def _maybe_run_maintenance(self) -> None:
        now = self._wall_clock()
        if now >= self._next_maintenance:
            self.run_maintenance()
            self._next_maintenance = now + self._maintenance_interval

    def run_maintenance(self) -> None:
        """Bound local disk independently of the sender: null blobs older than
        the age cap, then evict oldest-first until under the size cap, then
        return freed pages to the OS. Only nulls png_data — the sender's
        `png_data != None` filter skips nulled rows, so this never fights the
        upload cursor. Can evict *unsent* blobs when the sender is failing:
        that is the intended tradeoff (bounding disk beats at-least-once
        delivery when the disk is what's at risk)."""
        now = self._wall_clock()
        aged = schema.null_blobs_older_than(self._conn, now - self._local_max_blob_age)
        evicted = schema.null_oldest_blobs_until_under(
            self._conn, self._local_max_blob_bytes
        )
        schema.incremental_vacuum(self._conn)
        if aged or evicted:
            logger.info(
                "Disk guard: nulled %d aged + %d oversize blobs (%d bytes remain)",
                aged,
                evicted,
                schema.total_blob_bytes(self._conn),
            )

    def _ensure_recording(self) -> None:
        if self._recording_id is not None:
            return
        size = self._grabber.monitor_size()
        width, height = size if size else (None, None)
        self._recording_timestamp = self._wall_clock()
        self._recording_id = schema.insert_recording(
            self._conn,
            timestamp=self._recording_timestamp,
            monitor_width=width,
            monitor_height=height,
            task_description=constants.TASK_DESCRIPTION,
            config={
                "capturer": "captureV2",
                "active_interval_seconds": self._active_interval,
                "idle_threshold_seconds": self._idle_threshold,
                "max_image_width": self._max_image_width,
                "dhash_distance_threshold": self._dhash_threshold,
            },
        )
        logger.info(
            "Recording %s started (monitor=%sx%s, degraded_activity=%s)",
            self._recording_id,
            width,
            height,
            self._activity.degraded,
        )

    def tick(self) -> str:
        """One iteration of the capture policy. Returns the disposition."""
        self._ensure_recording()

        if not self._activity.active_within(self._idle_threshold):
            return IDLE

        frame = self._grabber.grab()
        if frame is None:
            return GRAB_FAILED

        thumb = to_thumb(frame, self._thumb_width)
        if brightness(thumb) < self._blank_threshold:
            logger.debug("Discarding blank frame (lock screen / display sleep)")
            self._blank_streak += 1
            if self._blank_streak == _BLANK_STREAK_RESET:
                logger.info(
                    "%d consecutive blank frames — reinitializing grabber",
                    self._blank_streak,
                )
                self._blank_streak = 0
                reset = getattr(self._grabber, "reset", None)
                if reset:
                    reset()
            return BLANK
        self._blank_streak = 0

        frame_hash = dhash(thumb)
        if (
            self._last_kept_hash is not None
            and hamming(frame_hash, self._last_kept_hash) <= self._dhash_threshold
        ):
            return UNCHANGED

        image = downscale(frame, self._max_image_width)
        png = encode_png(image, self._png_compress_level)
        timestamp = self._wall_clock()
        self._maybe_insert_window_event(timestamp)
        screenshot_id = schema.insert_screenshot(
            self._conn,
            recording_id=self._recording_id,
            recording_timestamp=self._recording_timestamp,
            timestamp=timestamp,
            png_data=png,
        )
        self._last_kept_hash = frame_hash
        self._captured_count += 1
        logger.debug("Captured screenshot %s (%d bytes)", screenshot_id, len(png))
        if self._captured_count % _LOG_EVERY_N_CAPTURES == 0:
            logger.info("Captured %d screenshots so far", self._captured_count)
        return CAPTURED

    def _maybe_insert_window_event(self, timestamp: float) -> None:
        try:
            info = self._foreground_fn()
        except Exception:
            info = None
        if not info or not info.get("title"):
            return
        key = (info.get("title"), info.get("window_id"))
        if key == self._last_window_key:
            return
        schema.insert_window_event(
            self._conn,
            recording_id=self._recording_id,
            recording_timestamp=self._recording_timestamp,
            timestamp=timestamp,
            title=info.get("title"),
            left=info.get("left"),
            top=info.get("top"),
            width=info.get("width"),
            height=info.get("height"),
            window_id=info.get("window_id"),
        )
        self._last_window_key = key
