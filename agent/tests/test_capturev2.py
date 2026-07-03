"""Unit tests for luminque.captureV2 — pure Python, no screen/input access."""

import sqlite3

import pytest
from PIL import Image

from luminque.captureV2 import loop as loop_mod
from luminque.captureV2 import schema
from luminque.captureV2.grabber import (
    brightness,
    dhash,
    downscale,
    encode_png,
    hamming,
    to_thumb,
)
from luminque.captureV2.loop import (
    BLANK,
    CAPTURED,
    GRAB_FAILED,
    IDLE,
    UNCHANGED,
    CaptureLoop,
)


def make_image(color, size=(320, 200)):
    return Image.new("RGB", size, color)


def checkerboard(size=(320, 200), block=20):
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            on = ((x // block) + (y // block)) % 2 == 0
            px[x, y] = (255, 255, 255) if on else (0, 0, 0)
    return img


class FakeActivity:
    def __init__(self, active=True, degraded=False):
        self.active = active
        self.degraded = degraded

    def active_within(self, seconds):
        return self.degraded or self.active


class FakeGrabber:
    """Yields a fixed sequence of frames (None = grab failure)."""

    def __init__(self, frames, monitor=(320, 200)):
        self.frames = list(frames)
        self.monitor = monitor

    def monitor_size(self):
        return self.monitor

    def grab(self):
        if not self.frames:
            return None
        return self.frames.pop(0)


@pytest.fixture
def conn(tmp_path):
    conn = schema.open_db(tmp_path / "recording.db")
    yield conn
    conn.close()


def make_loop(conn, grabber, activity=None, foreground_fn=lambda: None, **kwargs):
    return CaptureLoop(
        conn=conn,
        grabber=grabber,
        activity=activity or FakeActivity(),
        foreground_fn=foreground_fn,
        sleep=lambda _: None,
        **kwargs,
    )


class TestHashing:
    def test_identical_images_hash_equal(self):
        a, b = checkerboard(), checkerboard()
        assert hamming(dhash(to_thumb(a, 64)), dhash(to_thumb(b, 64))) == 0

    def test_different_images_hash_far_apart(self):
        a = to_thumb(checkerboard(), 64)
        b = to_thumb(checkerboard(block=50), 64)
        assert hamming(dhash(a), dhash(b)) > 4

    def test_brightness_black_vs_white(self):
        assert brightness(to_thumb(make_image((0, 0, 0)), 64)) < 8.0
        assert brightness(to_thumb(make_image((255, 255, 255)), 64)) > 200.0

    def test_downscale_caps_width_and_keeps_aspect(self):
        img = make_image((10, 10, 10), size=(2560, 1440))
        out = downscale(img, 1280)
        assert out.size == (1280, 720)

    def test_downscale_leaves_small_images_alone(self):
        img = make_image((10, 10, 10), size=(640, 480))
        assert downscale(img, 1280) is img

    def test_encode_png_roundtrip(self):
        img = checkerboard()
        data = encode_png(img, 1)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"


class TestDiskGuardSchema:
    def _insert(self, conn, ts_blobs):
        rec = schema.insert_recording(conn)
        for ts, blob in ts_blobs:
            schema.insert_screenshot(conn, rec, 0.0, ts, blob)

    def _live_ids(self, conn):
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM screenshot WHERE png_data IS NOT NULL ORDER BY id"
            )
        ]

    def test_total_blob_bytes_counts_only_live(self, conn):
        self._insert(conn, [(1.0, b"AAAA"), (2.0, b"BB")])
        assert schema.total_blob_bytes(conn) == 6
        schema.null_blobs_older_than(conn, cutoff_timestamp=1.5)
        assert schema.total_blob_bytes(conn) == 2  # 4-byte old row nulled

    def test_age_cap_nulls_old_keeps_recent(self, conn):
        self._insert(conn, [(900.0, b"old"), (995.0, b"new")])
        # now=1000, age cap 10 → cutoff 990; ts 900 nulled, ts 995 kept
        nulled = schema.null_blobs_older_than(conn, cutoff_timestamp=990.0)
        assert nulled == 1
        assert self._live_ids(conn) == [2]

    def test_size_cap_evicts_oldest_first(self, conn):
        self._insert(conn, [(float(i), b"X" * 8) for i in range(5)])  # 40 bytes
        # chunk_size=1 to evict precisely one-at-a-time, oldest first
        removed = schema.null_oldest_blobs_until_under(conn, max_bytes=10, chunk_size=1)
        assert removed == 4                       # 40→8, stops at <=10
        assert self._live_ids(conn) == [5]        # newest survives
        assert schema.total_blob_bytes(conn) <= 10

    def test_size_cap_noop_when_under(self, conn):
        self._insert(conn, [(1.0, b"small")])
        assert schema.null_oldest_blobs_until_under(conn, max_bytes=10**9) == 0
        assert self._live_ids(conn) == [1]

    def test_run_maintenance_enforces_both_caps(self, conn):
        loop = make_loop(
            conn,
            FakeGrabber([]),
            wall_clock=lambda: 1000.0,
            local_max_blob_age_seconds=10,
            local_max_blob_bytes=10**9,
        )
        schema.insert_recording(conn)
        schema.insert_screenshot(conn, 1, 0.0, 900.0, b"old")   # age 100 → evicted
        schema.insert_screenshot(conn, 1, 0.0, 998.0, b"new")   # age 2 → kept
        loop.run_maintenance()
        assert self._live_ids(conn) == [2]


class TestSchema:
    def test_creates_all_sender_tables(self, conn):
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"recording", "screenshot", "window_event", "action_event"} <= names

    def test_wal_mode(self, conn):
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_insert_ids_increment_across_reopen(self, tmp_path):
        path = tmp_path / "recording.db"
        conn = schema.open_db(path)
        rec = schema.insert_recording(conn, task_description="t")
        first = schema.insert_screenshot(conn, rec, 1.0, 1.0, b"png1")
        conn.close()

        conn = schema.open_db(path)  # restart appends, no migration
        rec2 = schema.insert_recording(conn, task_description="t")
        second = schema.insert_screenshot(conn, rec2, 2.0, 2.0, b"png2")
        assert rec2 > rec
        assert second > first
        conn.close()


class TestCaptureLoop:
    def test_idle_captures_nothing(self, conn):
        loop = make_loop(conn, FakeGrabber([checkerboard()]), FakeActivity(active=False))
        assert loop.tick() == IDLE
        assert conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0] == 0

    def test_active_changed_frame_is_captured(self, conn):
        loop = make_loop(conn, FakeGrabber([checkerboard()]))
        assert loop.tick() == CAPTURED
        row = conn.execute(
            "SELECT recording_id, png_data FROM screenshot"
        ).fetchone()
        assert row[0] is not None
        assert row[1][:8] == b"\x89PNG\r\n\x1a\n"

    def test_unchanged_frame_is_skipped(self, conn):
        loop = make_loop(conn, FakeGrabber([checkerboard(), checkerboard()]))
        assert loop.tick() == CAPTURED
        assert loop.tick() == UNCHANGED
        assert conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0] == 1

    def test_changed_frame_is_captured_again(self, conn):
        loop = make_loop(
            conn, FakeGrabber([checkerboard(), checkerboard(block=50)])
        )
        assert loop.tick() == CAPTURED
        assert loop.tick() == CAPTURED
        assert conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0] == 2

    def test_blank_frame_is_discarded(self, conn):
        loop = make_loop(conn, FakeGrabber([make_image((0, 0, 0))]))
        assert loop.tick() == BLANK
        assert conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0] == 0

    def test_grab_failure_is_tolerated(self, conn):
        loop = make_loop(conn, FakeGrabber([None, checkerboard()]))
        assert loop.tick() == GRAB_FAILED
        assert loop.tick() == CAPTURED

    def test_degraded_activity_still_captures(self, conn):
        loop = make_loop(
            conn,
            FakeGrabber([checkerboard()]),
            FakeActivity(active=False, degraded=True),
        )
        assert loop.tick() == CAPTURED

    def test_capture_downscales_to_max_width(self, conn):
        big = checkerboard(size=(2560, 1440))
        loop = make_loop(conn, FakeGrabber([big]), max_image_width=1280)
        assert loop.tick() == CAPTURED
        png = conn.execute("SELECT png_data FROM screenshot").fetchone()[0]
        import io

        assert Image.open(io.BytesIO(png)).size == (1280, 720)

    def test_window_event_inserted_on_title_change_only(self, conn):
        windows = [
            {"title": "Excel", "left": 0, "top": 0, "width": 800, "height": 600, "window_id": "1"},
            {"title": "Excel", "left": 0, "top": 0, "width": 800, "height": 600, "window_id": "1"},
            {"title": "Chrome", "left": 0, "top": 0, "width": 800, "height": 600, "window_id": "2"},
        ]
        frames = [checkerboard(), checkerboard(block=50), checkerboard(block=10)]
        loop = make_loop(
            conn, FakeGrabber(frames), foreground_fn=lambda: windows.pop(0)
        )
        assert loop.tick() == CAPTURED
        assert loop.tick() == CAPTURED
        assert loop.tick() == CAPTURED
        titles = [
            r[0] for r in conn.execute("SELECT title FROM window_event ORDER BY id")
        ]
        assert titles == ["Excel", "Chrome"]

    def test_foreground_failure_does_not_block_capture(self, conn):
        def boom():
            raise RuntimeError("UIPI")

        loop = make_loop(conn, FakeGrabber([checkerboard()]), foreground_fn=boom)
        assert loop.tick() == CAPTURED

    def test_recording_row_created_once(self, conn):
        loop = make_loop(
            conn, FakeGrabber([checkerboard(), checkerboard(block=50)])
        )
        loop.tick()
        loop.tick()
        rows = conn.execute(
            "SELECT task_description, monitor_width FROM recording"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "luminque-background"
        assert rows[0][1] == 320

    def test_run_forever_stops_on_stop(self, conn):
        loop = CaptureLoop(
            conn=conn,
            grabber=FakeGrabber([checkerboard()]),
            activity=FakeActivity(),
            foreground_fn=lambda: None,
            sleep=lambda _: loop.stop(),
        )
        loop.run_forever()  # returns instead of hanging
        assert conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0] == 1

    def test_run_forever_survives_tick_exception(self, conn):
        """A failing tick (e.g. DB locked past busy_timeout) must back off and
        continue, not kill the process — a crash here means a watchdog
        restart loop with up to 5-minute capture gaps."""

        class ExplodingGrabber:
            def __init__(self):
                self.calls = 0

            def monitor_size(self):
                return (320, 200)

            def grab(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("database is locked")
                return checkerboard()

        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                loop.stop()

        loop = CaptureLoop(
            conn=conn,
            grabber=ExplodingGrabber(),
            activity=FakeActivity(),
            foreground_fn=lambda: None,
            sleep=fake_sleep,
        )
        loop.run_forever()  # does not raise
        assert sleeps[0] == loop_mod._ERROR_BACKOFF_SECONDS
        # second iteration recovered and captured
        assert conn.execute("SELECT COUNT(*) FROM screenshot").fetchone()[0] == 1

    def test_maintenance_runs_on_interval_only(self, conn):
        clock = [1000.0]
        loop = make_loop(
            conn,
            FakeGrabber([]),
            wall_clock=lambda: clock[0],
            maintenance_interval_seconds=300,
        )
        ran = []
        loop.run_maintenance = lambda: ran.append(clock[0])

        loop._maybe_run_maintenance()           # next=0 → runs immediately on start
        assert ran == [1000.0]
        clock[0] = 1299.0
        loop._maybe_run_maintenance()           # before interval → no run
        assert ran == [1000.0]
        clock[0] = 1300.0
        loop._maybe_run_maintenance()           # interval elapsed → runs
        assert ran == [1000.0, 1300.0]

    def test_blank_streak_resets_grabber(self, conn):
        class ResettableGrabber(FakeGrabber):
            def __init__(self, frames):
                super().__init__(frames)
                self.resets = 0

            def reset(self):
                self.resets += 1

        black = make_image((0, 0, 0))
        grabber = ResettableGrabber([black] * loop_mod._BLANK_STREAK_RESET)
        loop = make_loop(conn, grabber)
        for _ in range(loop_mod._BLANK_STREAK_RESET):
            assert loop.tick() == BLANK
        assert grabber.resets == 1
